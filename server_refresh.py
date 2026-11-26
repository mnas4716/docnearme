#!/usr/bin/env python3
"""
Server-side refresh for the Railway deployment.

Only the HotDoc availability changes between refreshes, so this loads the static
pre-built base (master_base.json), re-reads HotDoc availability, re-attaches it, and
rebuilds master_map.html in place.

Two sources of availability:
  * GPs: HotDoc "find a doctor" pages for the busiest GP suburbs (rendered with
    Playwright's bundled Chromium; PW_CHANNEL unset), matched to GPs by clinic name.
  * Specialists: every specialist that hotdoc_links.json ties to a HotDoc practitioner.
    Their clinic's page data names each practitioner's earliest open appointment.
    Clinics are re-read in rotation (a slice per refresh, the stalest first), so the
    request rate stays low; the last known answer for every clinic is kept in
    hotdoc_spec_cache.json.
"""
from __future__ import annotations
import collections, csv, datetime, json, math, os, re, time, urllib.parse, urllib.request, urllib.error
from pathlib import Path
import specialist_map as S
import hotdoc_map as H
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
BASE = HERE / "master_base.json"
GP = HERE / "gp_prospects.csv"
OUT = HERE / "master_map.html"
BASE_URL = "https://www.hotdoc.com.au"
N_SEEDS = int(os.environ.get("AVAIL_SEEDS", "40"))
LINKS = HERE / "hotdoc_links.json"              # record profile url -> {"clinic": path, "slug": practitioner slug}
SPEC_CACHE = HERE / "hotdoc_spec_cache.json"    # clinic path -> {"at": epoch, "name", "addr", "tz", "docs": {slug: iso}}
SPEC_ROTATE = max(1, int(os.environ.get("SPEC_ROTATE", "6")))     # every clinic is re-read once per this many refreshes
SPEC_PAUSE = float(os.environ.get("SPEC_PAUSE", "1.2"))           # seconds between requests
SPEC_MAX_AGE_H = float(os.environ.get("SPEC_MAX_AGE_H", "30"))    # answers older than this are not shown
HD_API = BASE_URL + "/api/patient/pages?path="
HD_HEADERS = {"Accept": "application/au.com.hotdoc.v6", "Context": "from=direct;",
              "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/130 Safari/537.36"}

STOP = {"medical", "centre", "center", "practice", "clinic", "doctors", "doctor",
        "family", "the", "and", "health", "group", "surgery", "gp", "care", "co",
        "medicalcentre", "medicentre", "healthcare", "general"}


def norm_clinic(name: str) -> frozenset:
    toks = re.findall(r"[a-z0-9]+", (name or "").lower())
    return frozenset(t for t in toks if t not in STOP and len(t) > 1)


def gp_rows() -> list[dict]:
    return [r for r in csv.DictReader(open(GP, encoding="utf-8"))
            if not (r.get("Specialty") or "").strip()]


def seeds(rows, n):
    cnt = collections.Counter((r.get("Suburb", "").strip(), r.get("Postcode", "").strip())
                              for r in rows if r.get("Postcode"))
    out = []
    for (sub, pc), _ in cnt.most_common(n):
        slug = re.sub(r"[^a-z0-9]+", "-", sub.lower()).strip("-")
        out.append((sub, pc, f"{slug}-{pc}"))
    return out


def collect(seed_list):
    today = H.dt.date.today()
    clinics = {}
    ch = os.environ.get("PW_CHANNEL")           # unset on Railway -> bundled Chromium
    with sync_playwright() as p:
        lk = {"headless": True}
        if ch:
            lk["channel"] = ch
        b = p.chromium.launch(**lk)
        pg = b.new_page(viewport={"width": 1400, "height": 2200})
        pg.set_default_timeout(45000)
        for i, (sub, pc, slug) in enumerate(seed_list, 1):
            try:
                pg.goto(f"{BASE_URL}/find/doctor/NSW/{slug}", wait_until="domcontentloaded")
                pg.wait_for_function("() => /Next Available|No available|km away/.test(document.body.innerText)")
                pg.wait_for_timeout(900)
                cards = pg.evaluate(H._EXTRACT_CARDS_JS)
            except Exception as e:
                print(f"  avail [{i}/{len(seed_list)}] {slug}: skip ({e.__class__.__name__})", flush=True)
                continue
            for c in cards:
                if c["href"] in clinics:
                    continue
                rec = H.parse_card(c["text"], today)
                clinics[c["href"]] = {"name": rec["name"], "address": rec["address"],
                                      "next_date_label": rec["next_date_label"],
                                      "slots": rec["slots"][:6], "days_away": rec["days_away"],
                                      "href": c["href"], "seed_pc": pc}
            time.sleep(1.0)
        b.close()
    return list(clinics.values())


def _pc(addr):
    m = re.search(r"\b(\d{4})\b", addr or "")
    return m.group(1) if m else ""


def match(prospects, clinics):
    from urllib.parse import quote
    by_pc = collections.defaultdict(list)
    for c in clinics:
        by_pc[_pc(c["address"]) or c.get("seed_pc", "")].append((norm_clinic(c["name"]), c))
    out = {}
    for r in prospects:
        pc = (r.get("Postcode") or "").strip()
        pnorm = norm_clinic(r.get("Practice", ""))
        if not pnorm:
            continue
        subtoks = set(re.findall(r"[a-z0-9]+", (r.get("Suburb") or "").lower()))
        dp = pnorm - subtoks
        best, best_ov = None, 0
        for ctoks, c in by_pc.get(pc, []):
            dc = ctoks - subtoks
            ov = len(dp & dc) if (dp and dc) else (1 if (not dp and not dc and pnorm == ctoks) else 0)
            if ov > best_ov:
                best, best_ov = c, ov
        if best and best_ov >= 1:
            q = quote(f"{best['name']} {best['address'] or (r.get('Suburb','') + ' NSW')}")
            out[r["Profile URL"]] = {"label": best["next_date_label"], "slots": best["slots"],
                                     "days_away": best["days_away"], "clinic": best["name"],
                                     "href": best["href"],
                                     "maps": f"https://www.google.com/maps/search/?api=1&query={q}"}
    return out


# ---------------------------------------------------------------- specialists: HotDoc practitioner calendars ------
class Throttled(Exception):
    pass


_SPEC_MEM = None        # clinic path -> last answer; kept in memory between refreshes (the file is a best-effort copy)


def fetch_clinic(path: str):
    """One clinic's page data -> {"name","addr","tz","docs": {slug: earliest ISO or None}}; None if the page is gone."""
    url = HD_API + urllib.parse.quote(urllib.parse.quote(path, safe=""), safe="")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=HD_HEADERS), timeout=30) as resp:
            d = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (403, 429, 503):
            raise Throttled(str(e.code))
        return None
    c = d.get("clinic") or {}
    if not c:
        return None
    sub = d.get("suburb") or {}
    sub = (sub[0] if sub else {}) if isinstance(sub, list) else sub
    addr = ", ".join(x for x in (c.get("street_address"), f"{sub.get('name', '')} {sub.get('state', '')} {sub.get('postcode', '')}".strip()) if x)
    docs = {}
    for x in d.get("doctors") or []:
        ok = x.get("has_bookings_support") and not x.get("call_to_book") and x.get("visible_on_hot_doc", True)
        docs[x.get("slug")] = x.get("earliest_available") if ok else None
    return {"name": c.get("name"), "addr": addr, "tz": c.get("timezone") or "Australia/Sydney", "docs": docs}


def _local(iso: str, tz: str):
    """UTC ISO time -> aware datetime in the clinic's own timezone."""
    t = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    try:
        from zoneinfo import ZoneInfo
        return t.astimezone(ZoneInfo(tz))
    except Exception:                       # no timezone data: eastern standard time is the closest single answer
        return t.astimezone(datetime.timezone(datetime.timedelta(hours=10)))


def spec_av(entry: dict, slug: str, now=None):
    """The availability block for one practitioner from a cached clinic entry, or None."""
    iso = (entry.get("docs") or {}).get(slug)
    if not iso:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        t = _local(iso, entry.get("tz") or "Australia/Sydney")
    except Exception:
        return None
    if t < now - datetime.timedelta(minutes=5):
        return None                          # that appointment time has passed and has not been re-read yet
    days = (t.date() - now.astimezone(t.tzinfo).date()).days
    label = "Today" if days == 0 else "Tomorrow" if days == 1 else f"{t.strftime('%a')}, {t.day} {t.strftime('%b')}"
    hour = t.hour % 12 or 12
    return {"label": label, "slots": [f"{hour}:{t.minute:02d}{'am' if t.hour < 12 else 'pm'}"], "days_away": days,
            "clinic": entry.get("name") or ""}


def refresh_specialists() -> dict:
    """Re-read the stalest slice of linked clinics, then return {record url: availability} from everything known."""
    from urllib.parse import quote
    links = json.loads(LINKS.read_text()) if LINKS.exists() else {}
    if not links:
        return {}
    global _SPEC_MEM
    if _SPEC_MEM is None:                    # first refresh after a start: begin from the answers shipped with the app
        _SPEC_MEM = json.loads(SPEC_CACHE.read_text()) if SPEC_CACHE.exists() else {}
    cache = _SPEC_MEM
    paths = sorted({v["clinic"] for v in links.values()})
    paths.sort(key=lambda p: (cache.get(p) or {}).get("at", 0))            # stalest (or never read) first
    batch = paths[:max(1, math.ceil(len(paths) / SPEC_ROTATE))]
    read = 0
    for p in batch:
        try:
            got = fetch_clinic(p)
        except Throttled as e:
            print(f"  specialists: HotDoc answered {e}; keeping earlier answers", flush=True)
            break
        except Exception as e:
            print(f"  specialists: skip {p} ({e.__class__.__name__})", flush=True)
            continue
        cache[p] = dict(got or {"docs": {}}, at=time.time())
        read += 1
        time.sleep(SPEC_PAUSE)
    try:
        SPEC_CACHE.write_text(json.dumps(cache))
    except Exception:
        pass
    out, cutoff = {}, time.time() - SPEC_MAX_AGE_H * 3600
    for u, v in links.items():
        entry = cache.get(v["clinic"])
        if not entry or entry.get("at", 0) < cutoff:
            continue
        a = spec_av(entry, v["slug"])
        if a:
            a["href"] = f"{BASE_URL}{v['clinic']}/{v['slug']}"
            a["maps"] = "https://www.google.com/maps/search/?api=1&query=" + quote(f"{entry.get('name', '')} {entry.get('addr', '')}".strip())
            out[u] = a
    print(f"  specialists: {len(paths)} linked clinics, {read} re-read now, {len(out)} practitioners with an open appointment", flush=True)
    return out


def refresh() -> dict:
    t = time.time()
    records = json.loads(BASE.read_text())
    rows = gp_rows()
    clinics = collect(seeds(rows, N_SEEDS))
    avail = match(rows, clinics)
    try:
        spec = refresh_specialists()
    except Exception as e:                   # never let the specialist pass break the GP refresh
        print(f"  specialists: failed ({e.__class__.__name__}: {e})", flush=True)
        spec = {}
    for u, a in spec.items():
        avail.setdefault(u, a)
    n = 0
    for r in records:
        r.pop("av", None)
        a = avail.get(r["u"])
        if a:
            r["av"] = a; n += 1
    S.OUT = OUT
    S.DATA_OUT = HERE / "_merged.json"
    S.build(records)
    info = {"clinics": len(clinics), "matched": n, "specialists": len(spec), "mins": round((time.time() - t) / 60, 1)}
    print(f"[refresh] {info}", flush=True)
    return info


if __name__ == "__main__":
    refresh()
