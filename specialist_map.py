#!/usr/bin/env python3
"""
Specialist prospect mapper
==========================

Merges the Curbside specialist CSVs (the national Healthshare-derived set plus
the state expansion batches), geocodes each specialist by postcode/suburb using
an offline Australian postcode table, and writes an interactive map
(specialist_map.html) with a clustered pin per specialist, coloured by prospect
tier and filterable by state, specialty, tier and whether an email is on file.

Run:  /Users/moe/hotdoc-map/.venv/bin/python specialist_map.py
"""
from __future__ import annotations
import csv, glob, json, os, random, re, sys, collections
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tiering

random.seed(7)

GS = "/Users/moe/Documents/Genius scans - specialists"
POSTCODES = "/Users/moe/specialist-map/data/au_postcodes.csv"
OUT = Path("/Users/moe/specialist-map/specialist_map.html")
DATA_OUT = Path("/Users/moe/specialist-map/data/specialists_merged.json")

# ---- 1. gather all specialist CSVs (main file + expansion batches) ----------
def source_files() -> list[str]:
    files = [os.path.join(GS, "specialists_all.csv")]
    for d in ["gap-fill", "nsw-act-expansion", "sa-wa-tas-nt-expansion", "vic-qld-expansion"]:
        files += sorted(glob.glob(os.path.join(GS, d, "specialists_new*.csv")))
    files += glob.glob(os.path.join(GS, "nsw-act-new-only", "specialists_new_only.csv"))
    return [f for f in files if os.path.exists(f)]

STATE_BY_PC = {"0": "NT", "2": "NSW/ACT", "3": "VIC", "4": "QLD",
               "5": "SA", "6": "WA", "7": "TAS"}
def state_of(pc: str) -> str:
    pc = (pc or "").strip()
    return STATE_BY_PC.get(pc[:1], "?") if pc[:1].isdigit() else "?"

def merge_specialists() -> list[dict]:
    seen: dict[str, dict] = {}
    for f in source_files():
        for r in csv.DictReader(open(f, encoding="utf-8")):
            key = (r.get("Profile URL") or "").strip() or \
                  (r.get("Name", "") + "|" + r.get("Practice", "")).strip()
            if key and key not in seen:
                r["__src"] = "Healthshare"
                seen[key] = r
    return list(seen.values())


# ---- expansion sources (Ramsay, Healthscope, Doctify) + cross-source dedup --
EXPANSION_GLOB = "/Users/moe/specialist-map/expansion/data/*_specialists.csv"
PHYS_KW = re.compile(r"cardiol|gastro|hepatol|endocrin|respiratory|sleep medicine|"
                     r"rheumat|nephrol|neurolog|geriatric|immunol|allerg|"
                     r"haematol|hematol|oncolog|infectious|general medicine|"
                     r"internal medicine|palliat|rehabilitation|renal|"
                     r"thoracic medicine|clinical pharmacol", re.I)

def norm_name(n: str) -> str:
    n = re.sub(r"\b(a/?prof|assoc\.?\s*prof(essor)?|prof(essor)?|dr|mr|mrs|ms|miss|mx)\b",
               " ", n or "", flags=re.I)
    n = re.sub(r"\([^)]*\)", " ", n)          # drop nicknames
    return re.sub(r"[^a-z]", "", n.lower())

def load_expansion(base: list[dict]):
    """Return (net_new_rows, report). Net-new = not already in base by (name, state)."""
    import glob
    base_keys = {(norm_name(r.get("Name", "")), state_of(r.get("Postcode", ""))) for r in base}
    files = sorted(glob.glob(EXPANSION_GLOB))
    collected, net_new = [], []
    overlap = 0
    by_src = collections.Counter()
    phys_new = collections.Counter()
    for f in files:
        src = Path(f).stem.replace("_specialists", "").title()
        for r in csv.DictReader(open(f, encoding="utf-8")):
            r["__src"] = src
            collected.append(r)
            k = (norm_name(r.get("Name", "")), state_of(r.get("Postcode", "")))
            if k in base_keys:
                overlap += 1
            else:
                base_keys.add(k)            # also de-dupe within/among expansion sources
                tiering.tier_record(r)      # score/tier on the IMMEX scheme
                net_new.append(r)
                by_src[src] += 1
                if PHYS_KW.search(r.get("Specialty", "") or ""):
                    phys_new[src] += 1
    report = {"files": [Path(f).name for f in files], "collected": len(collected),
              "overlap": overlap, "net_new": len(net_new),
              "net_new_by_source": dict(by_src), "phys_new_by_source": dict(phys_new),
              "net_new_tiers": dict(sorted(collections.Counter(r.get("Tier") for r in net_new).items()))}
    return net_new, report

# ---- 2. offline geocoding by postcode (+ suburb match) ----------------------
def load_postcode_geo():
    by_pc_loc: dict[tuple, tuple] = {}   # (postcode, LOCALITY) -> (lat,lng)
    by_pc: dict[str, tuple] = {}         # postcode -> first valid (lat,lng)
    for r in csv.DictReader(open(POSTCODES, encoding="utf-8")):
        pc = (r.get("postcode") or "").strip()
        loc = (r.get("locality") or "").strip().upper()
        try:
            lat = float(r.get("lat") or 0); lng = float(r.get("long") or 0)
        except ValueError:
            continue
        if not pc or abs(lat) < 0.01 or abs(lng) < 0.01:
            continue
        by_pc_loc.setdefault((pc, loc), (lat, lng))
        by_pc.setdefault(pc, (lat, lng))
    return by_pc_loc, by_pc

def geocode(rows, by_pc_loc, by_pc):
    out, missed = [], 0
    for r in rows:
        pc = (r.get("Postcode") or "").strip()
        su = (r.get("Suburb") or "").strip().upper()
        # exact coords (e.g. Doctify) win over postcode-centroid
        exlat, exlng = (r.get("Lat") or "").strip(), (r.get("Lng") or "").strip()
        if exlat and exlng:
            try:
                lat, lng = float(exlat), float(exlng)
            except ValueError:
                exlat = ""
        if not exlat:
            geo = by_pc_loc.get((pc, su)) or by_pc.get(pc)
            if not geo:
                missed += 1
                continue
            lat, lng = geo
            # no random scatter: it used to push people into water and parks. Practitioners who share a
            # position are shown as one numbered group that opens a list (see the page template).
        sub = (r.get("Subspecialty / interest") or "").strip()
        if len(sub) > 140:
            sub = sub[:138] + "…"
        out.append({
            "n": r.get("Name", "").strip(),
            "s": (r.get("Specialty") or "").strip(),
            "sub": sub,
            "pr": (r.get("Practice") or "").strip(),
            "su": (r.get("Suburb") or "").strip(),
            "pc": pc,
            "st": state_of(pc),
            "ph": (r.get("Phone") or "").strip(),
            "em": (r.get("Published email") or "").strip(),
            "w": (r.get("Website") or "").strip(),
            "u": (r.get("Profile URL") or "").strip(),
            "t": (r.get("Tier") or "").strip() or "?",
            "rg": (r.get("Region") or "").strip(),
            "src": r.get("__src") or "Healthshare",
            "lat": round(lat, 5), "lng": round(lng, 5),
            "loc": "address" if exlat else "suburb",      # how precise the position is
        })
    return out, missed

# ---- record category (kind): gp / spec / diag (imaging & pathology) / allied -------------------------------------
# Imaging & pathology are grouped apart from consulting specialists so they can be hidden with allied health under
# the DOCTORS_ONLY switch. A record is "diag" only when EVERY slashed component is imaging/pathology, so a
# "Cardiologist / Nuclear Medicine Physician" stays a specialist and an O&G ultrasound sub-spec is not miscounted.
IMGPATH = re.compile(r"radiolog|nuclear medicine|patholog|\bimaging\b|sonograph", re.I)
def _imgpath_component(c: str) -> bool:
    c = c.strip().lower()
    if not c:
        return False
    if "speech" in c and "patholog" in c:          # speech pathology is allied health, not medical pathology
        return False
    return bool(IMGPATH.search(c))
# GP and GP sub-types (GP Registrar, GP Obstetrician/Anaesthetist, General Practice (GP), Family Medicine) are GPs,
# not specialists -- but "General Physician"/"General Medicine" (consultant physicians) must stay specialists.
GP_RE = re.compile(r"(?i)^(gp|general\s*practi|family\s*(medicine|physician|doctor|practi))")
# Vague/special-interest labels that are GPs on general registration, verified against the AHPRA register
# (2026-11-25: e.g. Dr David Banney, "Skin Cancer Doctor", holds general registration -- not a dermatology
# specialist). The DESCRIPTOR (s) is always kept as-is for display; only the kind (filter bucket) becomes gp.
GPISH = re.compile(r"(?i)skin\s*cancer|travel\s*(doctor|medicine)|cosmetic\s*(doctor|physician|medicine)")
def kind_of(r: dict) -> str:
    if r.get("k"):                                 # allied records are tagged upstream (k="allied", pg=profession)
        return r["k"]
    s = (r.get("s") or "").strip()
    if (s == "General Practitioner" or GP_RE.match(s) or GPISH.search(s)
            or s.lower() in ("medical practitioner", "other medical practitioner")):
        return "gp"
    return "spec"   # individual imaging/pathology doctors are specialists; only the preset-k CENTRES (diag_base.json) stay "diag" -- separates radiologist/pathologist doctors from imaging centres (user 2026-11-25)


_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")   # inline emails hide in scraped free-text (sub/ph/w)
def _scrub_email(s):
    return re.sub(r"\s+", " ", _EMAIL_RE.sub(" ", s)).strip() if (s and "@" in s) else s


def build(records):
    import hashlib
    # merge allied health, kept in a separate file so master_base.json is never rewritten (data split, 2026-11)
    allied_path = OUT.parent / "allied_base.json"
    if allied_path.exists():
        records = records + json.loads(allied_path.read_text())
    diag_path = OUT.parent / "diag_base.json"       # imaging & pathology sites (geolocated), user 2026-11-25
    if diag_path.exists() and os.environ.get("SKIP_DIAG", "").strip().lower() not in ("1", "on", "true", "yes"):
        records = records + json.loads(diag_path.read_text())   # SKIP_DIAG=on pulls imaging until fork-5 geocoding is final (user 2026-11-25)
    fac_path = OUT.parent / "facilities_base.json"  # pharmacies & hospitals, open Geoscience Australia CC BY 4.0, user 2026-11-25
    if fac_path.exists():
        records = records + json.loads(fac_path.read_text())
    dental_path = OUT.parent / "dental_base.json"   # dentists (Overture CDLA / OSM ODbL / HotDoc), geolocated; st normalized at source (pc + coords-fallback) -> use as-is
    if dental_path.exists():
        records = records + json.loads(dental_path.read_text())
    for r in records:                              # category tag on every record (doctors derived, allied preset)
        r["k"] = kind_of(r)
        if r["k"] == "allied":                     # split allied -> physio / psych / other by specialty string (marker redesign)
            _s = r.get("s") or ""
            r["k"] = "physio" if re.search(r"physiotherap", _s, re.I) else "psych" if re.search(r"psycholog", _s, re.I) else "allied"
    rv_path = OUT.parent / "review_scores.json"    # review scores (doctors), keyed by profile URL; attach only a real aggregate (user 2026-11-25)
    rvs = json.loads(rv_path.read_text()) if rv_path.exists() else {}
    for r in records:
        if r.get("k") in ("gp", "spec"):
            v = rvs.get((r.get("u") or "").rstrip("/"))
            if v and v.get("score") is not None:
                r["rv"] = v
    if os.environ.get("DOCTORS_ONLY", "").strip().lower() in ("1", "on", "true", "yes"):
        records = [r for r in records if r.get("k") in ("gp", "spec")]   # hide allied + imaging/pathology
    specialties = collections.Counter(r["s"] for r in records if r["s"])
    states = collections.Counter(r["st"] for r in records)
    tiers = collections.Counter(r["t"] for r in records)
    sources = collections.Counter(r["src"] for r in records)
    kinds = collections.Counter(r["k"] for r in records)
    profs = collections.Counter(r.get("pg") for r in records if r.get("k") == "allied")
    DATA_OUT.write_text(json.dumps(records))
    meta = {
        "count": len(records),
        "specialties": specialties.most_common(),
        "states": dict(states),
        "tiers": dict(sorted(tiers.items())),
        "sources": dict(sources),
        "hasAvail": sum(1 for r in records if r.get("av")),
        "typeCounts": {"GP": kinds.get("gp", 0), "Specialist": kinds.get("spec", 0)},
        "kinds": dict(kinds),
        "profs": dict(profs),
    }
    meta_json = json.dumps(meta, separators=(",", ":"))
    # ---- overview + detail-on-demand re-architecture (fork-7) -------------------------------------------------
    # 1) OVERVIEW (blocks first paint, ~601 KB gz): one row per record [lat,lng,kindCode,id,score]; score=rv.score
    #    or null. NO name/addr/email/phone. kindCode: gp0 spec1 diag2 allied3 pharmacy4 hospital5. Carries META too
    #    (small, no PII) so the chrome (chips/legend/datalist) can render before the index arrives.
    KC = {"gp": 0, "spec": 1, "diag": 2, "allied": 3, "pharmacy": 4, "hospital": 5, "physio": 6, "psych": 7, "dental": 8}
    ov = [[r["lat"], r["lng"], KC.get(r.get("k"), 1), i, (r.get("rv") or {}).get("score")]
          for i, r in enumerate(records)]
    ov_json = json.dumps({"ov": ov, "meta": meta}, separators=(",", ":"))
    # 2) INDEX (deferred, non-blocking): the fields the list rows + search engine read, MINUS em (and minus
    #    popup-only/internal fields w,u,addr,src,rg,loc,rv.src and the lat/lng/k the overview already carries).
    #    Same id order as the overview. Tune the kept-field set to taste (drop "t" if you drop the tier UI, etc.).
    IXF = ("n", "s", "sub", "pr", "su", "pc", "st", "ph", "t", "av", "pg")
    def _ix(r):
        o = {k: r[k] for k in IXF if r.get(k) not in (None, "")}
        for _k, _v in list(o.items()):                 # scrub inline emails from served free-text (sub, ph, ...)
            if isinstance(_v, str) and "@" in _v:
                o[_k] = _scrub_email(_v)
        v = r.get("rv")
        if v and v.get("score") is not None:
            o["rv"] = {"score": v.get("score"), "n": v.get("n"), "conf": v.get("conf")}   # summary only; breakdown via /r
        return o
    ix_json = json.dumps([_ix(r) for r in records], separators=(",", ":"))

    DATA_DIR = OUT.parent / "data_files"
    DATA_DIR.mkdir(exist_ok=True)
    def _emit(prefix, body, var, ready):           # same md5-hash + keep-latest-2-prune pattern, per family
        hh = hashlib.md5(body.encode()).hexdigest()[:16]
        (DATA_DIR / f"{prefix}-{hh}.js").write_text(
            "window." + var + "=" + body + ";if(window." + ready + ")window." + ready + "(window." + var + ");")
        keep = sorted(DATA_DIR.glob(f"{prefix}-*.js"), key=lambda f: f.stat().st_mtime, reverse=True)[:2]
        for f in DATA_DIR.glob(f"{prefix}-*.js"):
            if f not in keep:
                try: f.unlink()
                except OSError: pass
        return hh
    ov_h = _emit("map-ov", ov_json, "__DNM_OV", "__dnmOv")
    ix_h = _emit("map-ix", ix_json, "__DNM_IX", "__dnmIx")
    # retire the old single-file build output, but keep the newest one through the shell's ~5-min CDN cache window
    big = sorted((f for f in DATA_DIR.glob("map-*.js") if not re.match(r"map-(ov|ix)-", f.name)),
                 key=lambda f: f.stat().st_mtime, reverse=True)
    for f in big[1:]:
        try: f.unlink()
        except OSError: pass
    html = HTML.replace("__OVHASH__", ov_h).replace("__IXHASH__", ix_h)
    OUT.write_text(html)
    print(f"Wrote {OUT}  ({len(records)} mapped; overview map-ov-{ov_h}.js {len(ov_json)//1024} KB, "
          f"index map-ix-{ix_h}.js {len(ix_json)//1024} KB, shell {len(html)//1024} KB)")
    print("States :", dict(states))
    print("Tiers  :", dict(sorted(tiers.items())))


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>docnearme — Specialist prospect map</title>
<meta name="theme-color" content="#2563eb">
<link rel="icon" href="data:image/svg+xml,%3Csvg%20xmlns=%22http://www.w3.org/2000/svg%22%20viewBox=%220%200%2032%2032%22%3E%3Cdefs%3E%3ClinearGradient%20id=%22dnm%22%20x1=%220%22%20y1=%220%22%20x2=%221%22%20y2=%221%22%3E%3Cstop%20offset=%220%22%20stop-color=%22%232563eb%22/%3E%3Cstop%20offset=%221%22%20stop-color=%22%2306b6d4%22/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect%20width=%2232%22%20height=%2232%22%20rx=%229%22%20fill=%22url%28%23dnm%29%22/%3E%3Cpath%20d=%22M16%206.2c-4.5%200-8%203.4-8%207.8%200%205.5%206.300%2010.800%207.300%2011.600.4.300.980.300%201.400%200%201-.8%207.300-6.100%207.300-11.600%200-4.400-3.500-7.800-8-7.800z%22%20fill=%22%23fff%22/%3E%3Cpath%20d=%22M14.600%2010.200h2.800v2.600H20v2.800h-2.600v2.600h-2.800v-2.600H12v-2.800h2.600z%22%20fill=%22%232563eb%22/%3E%3C/svg%3E">
<script>try{var __t=localStorage.getItem('dnm-theme');if(__t==='dark'||__t==='light')document.documentElement.setAttribute('data-theme',__t);}catch(e){}</script>
<link rel="preconnect" href="https://tiles.openfreemap.org" crossorigin>
<link rel="dns-prefetch" href="https://basemaps.cartocdn.com">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl.min.js"></script>
<script defer src="/data/map-ov-__OVHASH__.js"></script>
<script defer src="/data/map-ix-__IXHASH__.js"></script>
<style>
  :root{
    color-scheme:light;--bg:#f4f6f8;--panel:#ffffff;--panel2:#f3f5f8;--ink:#121922;--muted:#566473;--faint:#8793a0;--line:#e3e7ec;
    --accent:#2563eb;--on-accent:#ffffff;--accent-soft:rgba(37,99,235,.11);--ok:#15803d;--ok-soft:rgba(21,163,74,.12);
    --star:#b45309;--star-soft:rgba(245,158,11,.15);--mark:rgba(250,204,21,.5);
    --shadow:0 8px 28px rgba(16,24,40,.16);
    --tA:#15a34a;--tB:#2f6fed;--tC:#f0a92b;--tD:#9aa7b4;
  }
  @media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
    color-scheme:dark;--bg:#0b1015;--panel:#121920;--panel2:#19222b;--ink:#eaf0f5;--muted:#a3b1bf;--faint:#73828f;--line:#232e39;
    --accent:#7aa7ff;--on-accent:#0b1015;--accent-soft:rgba(122,167,255,.16);--ok:#4ade80;--ok-soft:rgba(74,222,128,.13);
    --star:#fbbf24;--star-soft:rgba(251,191,36,.14);--mark:rgba(250,204,21,.32);
    --shadow:0 8px 28px rgba(0,0,0,.55);}}
  :root[data-theme="dark"]{
    color-scheme:dark;--bg:#0b1015;--panel:#121920;--panel2:#19222b;--ink:#eaf0f5;--muted:#a3b1bf;--faint:#73828f;--line:#232e39;
    --accent:#7aa7ff;--on-accent:#0b1015;--accent-soft:rgba(122,167,255,.16);--ok:#4ade80;--ok-soft:rgba(74,222,128,.13);
    --star:#fbbf24;--star-soft:rgba(251,191,36,.14);--mark:rgba(250,204,21,.32);
    --shadow:0 8px 28px rgba(0,0,0,.55);}
  *{box-sizing:border-box}
  [hidden]{display:none!important}
  html,body{height:100%;margin:0}
  body{font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif;color:var(--ink);background:var(--bg);-webkit-font-smoothing:antialiased}
  button,input,select{font:inherit;color:inherit}
  #wrap{display:flex;height:100vh;height:100dvh}
  #side{width:420px;flex:0 0 420px;background:var(--panel);border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0;position:relative;z-index:2}
  #head{padding:15px 18px 9px}
  .brand{display:flex;align-items:center;gap:9px}
  .brand .grow{flex:1}
  #head h1{display:flex;align-items:center;gap:8px;font-size:19px;font-weight:750;letter-spacing:-.03em;margin:0;white-space:nowrap}
  #head h1 .mark{width:28px;height:28px;flex:none;border-radius:8px;box-shadow:0 2px 8px rgba(37,99,235,.35)}
  #head h1 .wm b{font-weight:750;background:linear-gradient(90deg,#2563eb,#06b6d4);-webkit-background-clip:text;background-clip:text;color:transparent}
  #tagline{min-width:0;font-size:10.5px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:var(--muted);background:var(--panel2);border:1px solid var(--line);border-radius:999px;padding:3px 8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  #sub{margin:5px 0 0;color:var(--muted);font-size:12px}
  .icon-btn{flex:none;display:grid;place-items:center;width:30px;height:30px;border:1px solid var(--line);background:var(--panel);color:var(--muted);border-radius:9px;cursor:pointer;font-size:15px;line-height:1;padding:0}
  .icon-btn:hover{color:var(--ink);border-color:var(--faint)}
  .icon-btn:focus-visible,.chip:focus-visible,#moreBtn:focus-visible,#sortby:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

  #searchbox{position:relative;margin:2px 18px 11px}
  #searchbox svg{position:absolute;left:13px;top:50%;transform:translateY(-50%);width:17px;height:17px;color:var(--faint);pointer-events:none}
  #q{width:100%;height:44px;padding:0 42px 0 39px;font-size:15px;background:var(--panel2);border:1.5px solid transparent;border-radius:12px;outline:none;-webkit-appearance:none;appearance:none}
  #q::placeholder{color:var(--faint)}
  #q:placeholder-shown{text-overflow:ellipsis}
  #q::-webkit-search-cancel-button{display:none}
  #q:focus{border-color:var(--accent);background:var(--panel);box-shadow:0 0 0 4px var(--accent-soft)}
  #qhint{position:absolute;right:12px;top:50%;transform:translateY(-50%);font:600 11px/1 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--faint);border:1px solid var(--line);border-radius:6px;padding:4px 7px;background:var(--panel);pointer-events:none}
  #qclear{position:absolute;right:8px;top:50%;transform:translateY(-50%);border:0;background:transparent;font-size:20px;width:28px;height:28px}

  #didyou{margin:-4px 18px 9px;font-size:12px;line-height:1.4;color:var(--muted)}
  #didyou b{color:var(--ink)}
  #didyou a{color:var(--accent);font-weight:600;cursor:pointer;white-space:nowrap}
  #didyou a:hover{text-decoration:underline}
  #quick{padding:0 18px 2px;display:grid;gap:7px}
  .frow{display:flex;align-items:flex-start;gap:8px}
  .frow>.lab{flex:0 0 36px;font-size:10.5px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:var(--muted);padding-top:7px}
  #more .frow>.lab{flex-basis:49px}
  .chips{display:flex;flex-wrap:wrap;gap:6px;min-width:0}
  .chip{display:inline-flex;align-items:center;font-size:12px;font-weight:550;line-height:1;border:1px solid var(--line);background:var(--panel);color:var(--muted);border-radius:999px;padding:6px 8px;cursor:pointer;user-select:none;white-space:nowrap;transition:background .12s,color .12s,border-color .12s}
  .chip:hover{border-color:var(--faint);color:var(--ink)}
  .chip .n{font-weight:400;opacity:.85;margin-left:4px;font-variant-numeric:tabular-nums}
  .chip .dot{width:8px;height:8px;border-radius:50%;margin-right:5px;flex:none}
  .chip.on{background:var(--ink);color:var(--panel);border-color:var(--ink)}
  .chip.on .dot{background:currentColor!important;opacity:.9}
  .chip.tA.on{background:var(--tA);border-color:var(--tA);color:#fff}
  .chip.tB.on{background:var(--tB);border-color:var(--tB);color:#fff}
  .chip.tC.on{background:var(--tC);border-color:var(--tC);color:#2b1d00}
  .chip.tD.on{background:var(--tD);border-color:var(--tD);color:#10161d}

  #moreBtn{display:flex;align-items:center;gap:8px;width:calc(100% - 36px);margin:9px 18px 0;padding:9px 0;border:0;border-top:1px solid var(--line);background:transparent;color:var(--muted);font-size:12px;font-weight:600;cursor:pointer;text-align:left}
  #moreBtn:hover{color:var(--ink)}
  #moreBtn .grow{flex:1}
  #moreBtn .lbl-m{display:none}
  #moreN{display:inline-grid;place-items:center;min-width:18px;height:18px;padding:0 5px;border-radius:9px;background:var(--accent);color:var(--on-accent);font-size:11px;font-weight:700}
  #moreBtn .chev{transition:transform .15s;font-size:11px}
  #moreBtn[aria-expanded="true"] .chev{transform:rotate(180deg)}
  #more{padding:0 18px 11px;display:grid;gap:8px;max-height:38vh;overflow:auto}
  #specwrap{position:relative;flex:1;min-width:0}
  #spec{width:100%;height:34px;padding:0 30px 0 11px;font-size:13px;background:var(--panel2);border:1.5px solid transparent;border-radius:9px;outline:none}
  #spec::placeholder{color:var(--faint)}
  #spec:focus{border-color:var(--accent);background:var(--panel)}
  #specclear{position:absolute;right:3px;top:50%;transform:translateY(-50%);border:0;background:transparent;font-size:18px;width:26px;height:26px}

  #bar{display:flex;align-items:center;gap:7px;padding:8px 12px 8px 18px;border-top:1px solid var(--line);border-bottom:1px solid var(--line);background:var(--panel2)}
  #count{flex:1;min-width:0;font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  #count b{color:var(--ink);font-size:14.5px;font-weight:700;font-variant-numeric:tabular-nums}
  #bar .chip{padding:5px 9px;font-size:11.5px}
  #inviewBtn.on{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
  #sortby{flex:none;width:100px;height:28px;font-size:12px;padding:0 6px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink);cursor:pointer}
  #reset{width:28px;height:28px;border-radius:8px;font-size:14px}
  #locpill{display:flex;align-items:center;gap:8px;padding:7px 12px 7px 18px;font-size:12px;color:var(--ink);background:var(--accent-soft);border-bottom:1px solid var(--line)}
  #locpill span{flex:1;min-width:0}
  #locpill button{border:0;background:transparent;color:var(--muted);font-size:18px;line-height:1;cursor:pointer;padding:0 6px}

  #list{flex:1;min-height:0;overflow-y:auto;overscroll-behavior:contain;scrollbar-width:thin;outline:none}
  .row{position:relative;padding:11px 16px 11px 24px;border-bottom:1px solid var(--line);cursor:pointer}
  .row .tbar{position:absolute;left:10px;top:13px;bottom:13px;width:4px;border-radius:3px}
  .row:hover,.row.kb{background:var(--panel2)}
  .row.sel{background:var(--accent-soft)}
  .row.sel .nm{color:var(--accent)}
  .row.kb{box-shadow:inset 3px 0 0 var(--accent)}
  .r1{display:flex;align-items:center;gap:8px}
  .nm{flex:1;min-width:0;font-weight:650;font-size:14px;letter-spacing:-.005em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .tb{flex:none;display:grid;place-items:center;width:19px;height:19px;border-radius:6px;font-size:10.5px;font-weight:800;color:#fff}
  .tb.tA{background:var(--tA)}.tb.tB{background:var(--tB)}.tb.tC{background:var(--tC);color:#2b1d00}.tb.tD{background:var(--tD);color:#10161d}
  .r2{margin-top:1px;color:var(--muted);font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .r2 .sp{color:var(--ink);opacity:.86}
  .r3{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:5px;font-size:11.5px;color:var(--muted)}
  .r3 .loc{margin-right:2px}
  .b{display:inline-flex;align-items:center;gap:3px;border-radius:6px;padding:2px 6px;font-size:11px;font-weight:600;line-height:1.35;white-space:nowrap}
  .bav{background:var(--ok-soft);color:var(--ok)}
  .bstar{background:var(--star-soft);color:var(--star)}
  .bstar i{font-style:normal;font-weight:400;opacity:.8}
  .bmail{background:var(--panel2);color:var(--muted)}
  .bnr{background:var(--panel2);color:var(--faint)}
  .row:hover .bmail,.row.kb .bmail{background:var(--panel)}
  mark{background:var(--mark);color:var(--ink);border-radius:3px;padding:0 1px}
  .empty{padding:40px 26px;text-align:center;color:var(--muted);font-size:13px}
  .empty b{display:block;color:var(--ink);font-size:15px;margin-bottom:4px}
  .endnote{padding:14px 18px 22px;text-align:center;color:var(--faint);font-size:11.5px}

  #mapwrap{flex:1;min-width:0;position:relative;background:var(--bg)}
  #map{position:absolute;inset:0}
  #mapmsg{position:absolute;inset:0;z-index:1;display:grid;place-items:center;padding:30px;text-align:center;color:var(--muted);background:var(--bg);overflow:auto}
  #mapmsg .pop{text-align:left;max-width:340px;color:var(--ink);background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 18px;box-shadow:var(--shadow)}
  #toast{position:absolute;left:50%;top:14px;transform:translateX(-50%);z-index:4;display:flex;align-items:center;gap:8px;max-width:calc(100% - 120px);padding:7px 7px 7px 14px;border-radius:999px;background:var(--panel);color:var(--ink);border:1px solid var(--line);box-shadow:var(--shadow);font-size:12.5px;white-space:nowrap}
  #toast span{min-width:0;overflow:hidden;text-overflow:ellipsis}
  #toast button{flex:none;border:0;border-radius:999px;padding:6px 11px;font-size:12px;font-weight:600;cursor:pointer;background:var(--accent);color:var(--on-accent)}
  #toast button.x{background:transparent;color:var(--faint);font-size:17px;line-height:1;padding:2px 7px}
  #foot{display:flex;align-items:center;gap:13px;padding:7px 18px;border-top:1px solid var(--line);font-size:11.5px;color:var(--faint)}
  #foot a{color:var(--muted);text-decoration:none;font-weight:500}
  #foot a:hover{color:var(--ink);text-decoration:underline}
  #foot .grow{flex:1}
  .bdist{background:var(--accent-soft);color:var(--accent)}
  #map .maplibregl-ctrl-group{border-radius:10px;overflow:hidden;box-shadow:0 2px 10px rgba(16,24,40,.18)}
  #map .maplibregl-popup{z-index:3}
  #map .maplibregl-popup-content{background:var(--panel);color:var(--ink);padding:14px 16px 13px;border-radius:14px;border:1px solid var(--line);box-shadow:var(--shadow);font:inherit;font-size:13px;line-height:1.45}
  #map .maplibregl-popup-close-button{right:5px;top:5px;width:26px;height:26px;border-radius:8px;font-size:19px;line-height:1;color:var(--faint);background:transparent}
  #map .maplibregl-popup-close-button:hover{color:var(--ink);background:var(--panel2)}
  #map .maplibregl-popup-anchor-bottom .maplibregl-popup-tip,#map .maplibregl-popup-anchor-bottom-left .maplibregl-popup-tip,#map .maplibregl-popup-anchor-bottom-right .maplibregl-popup-tip{border-top-color:var(--panel)}
  #map .maplibregl-popup-anchor-top .maplibregl-popup-tip,#map .maplibregl-popup-anchor-top-left .maplibregl-popup-tip,#map .maplibregl-popup-anchor-top-right .maplibregl-popup-tip{border-bottom-color:var(--panel)}
  #map .maplibregl-popup-anchor-left .maplibregl-popup-tip{border-right-color:var(--panel)}
  #map .maplibregl-popup-anchor-right .maplibregl-popup-tip{border-left-color:var(--panel)}
  .pop .ptags{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:7px;padding-right:24px}
  .pop .tag{display:inline-block;font-size:10px;font-weight:700;letter-spacing:.03em;text-transform:uppercase;color:#fff;border-radius:5px;padding:2px 7px}
  .pop .tag.src{background:#64748b}
  .pop .tag.tC{color:#2b1d00}.pop .tag.tD{color:#10161d}
  .pop .pn{font-size:15.5px;font-weight:700;letter-spacing:-.01em;line-height:1.25}
  .pop .mo{color:var(--muted);font-size:12.5px;margin-top:3px}
  .pop .k{font-size:12.5px;margin-top:3px}
  .pop .approx{display:inline-block;margin-top:5px;font-size:11.5px;color:var(--muted);background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:2px 7px}
  .pop a{color:var(--accent);text-decoration:none;font-weight:600;cursor:pointer}
  .pop a:hover{text-decoration:underline}
  .pop .avail{margin:10px 0 2px;padding:8px 10px;background:var(--ok-soft);border-radius:10px;font-size:12px;color:var(--ok)}
  .pop .avail .slots{display:flex;flex-wrap:wrap;gap:4px;margin:5px 0}
  .pop .avail .slot{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:1px 6px;font-size:11px;color:var(--ok)}
  .pop .gmaps{margin-top:8px;font-size:12px;color:var(--star)}
  .pop .rv{margin-top:9px}
  .pop .rvhow,.pop .rvsrc{margin-top:6px;font-size:12px}
  .pop .rvhow>summary,.pop .rvsrc>summary{color:var(--accent);font-weight:600;cursor:pointer;list-style:none}
  .pop .rvhow>summary::-webkit-details-marker,.pop .rvsrc>summary::-webkit-details-marker{display:none}
  .pop .rvmeth{margin-top:5px;color:var(--muted);font-size:11.5px;line-height:1.5}
  .pop .rvrow{margin-top:5px;color:var(--ink);font-size:12px}
  .pop .rvrow a{white-space:nowrap}
  .pop .links{margin-top:10px;padding-top:9px;border-top:1px solid var(--line);font-size:12.5px}
  .pop .gback{display:inline-block;margin-bottom:7px;font-size:12px}
  .grp .gh{font-size:13.5px;font-weight:700;margin-bottom:2px;padding-right:24px}
  .grp .gsub{font-size:11.5px;color:var(--muted);margin-bottom:8px}
  .grp .gl{max-height:clamp(96px,calc(50vh - 170px),270px);overflow:auto;margin:0 -8px;scrollbar-width:thin}
  .grp .gi{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:8px;color:var(--ink);font-weight:500}
  .grp .gi:hover{background:var(--panel2);text-decoration:none}
  .grp .gdot{flex:none;width:8px;height:8px;border-radius:50%}
  .grp .gn{flex:1;min-width:0;font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .grp .gs{flex:none;max-width:42%;font-size:11.5px;font-weight:400;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .grp .gall{display:block;margin-top:9px;padding-top:9px;border-top:1px solid var(--line);font-size:12.5px}

  @media (max-width:760px){
    #wrap{flex-direction:column}
    #side{width:100%;flex:0 0 60%;border-right:0;border-bottom:1px solid var(--line)}
    #head{padding:10px 14px 6px}#sub{display:none}
    #searchbox{margin:2px 14px 8px}#didyou{margin:-2px 14px 7px}#quick{padding:0 14px 2px}#qhint{display:none}
    #moreBtn{width:calc(100% - 28px);margin:7px 14px 0}#more{padding:0 14px 9px}
    #q{font-size:16px}
    #toast{max-width:calc(100% - 70px);top:10px}
    #foot{padding:6px 14px}
    #side{flex-basis:58%}
    #side:not(.more-open) #quick{display:none}
    #moreBtn{border-top:0;margin-top:0;padding-top:2px}
    #moreBtn .lbl-d{display:none}#moreBtn .lbl-m{display:inline}
    .frow>.lab{display:none}
    #map .maplibregl-popup-content{max-height:calc(42vh - 70px);max-height:calc(42dvh - 70px);overflow:auto}
    #side.more-open{flex-basis:80%}
    #list{min-height:96px}
    #spec{font-size:16px}#sortby{font-size:16px;width:124px}
    #locpill button{min-width:30px;min-height:30px}
    #count .cl{display:none}
  }

  /* ---- floating layout: the map fills the screen, search + results float over it as one card ---- */
  #wrap{display:block;position:relative;height:100vh;height:100dvh;overflow:hidden}
  #mapwrap{position:absolute;inset:0}
  #side{position:absolute;z-index:5;top:14px;left:14px;width:400px;max-width:calc(100vw - 28px);flex:none;max-height:calc(100vh - 28px);max-height:calc(100dvh - 28px);
        border:1px solid var(--line);border-radius:22px;overflow:hidden;box-shadow:0 10px 40px rgba(16,24,40,.22),0 2px 6px rgba(16,24,40,.08);
        background:var(--panel);background:color-mix(in srgb,var(--panel) 88%,transparent);-webkit-backdrop-filter:blur(20px) saturate(1.5);backdrop-filter:blur(20px) saturate(1.5)}
  #head{padding:13px 16px 8px}
  #searchbox{display:flex;align-items:center;gap:8px;margin:0 14px 12px}
  #searchbox>svg{left:14px}
  #q{flex:1;min-width:0;height:46px;border-radius:999px;padding:0 40px 0 40px;background:var(--panel2);border-color:var(--line)}
  #qhint{right:66px}#qclear{right:60px}
  #moreBtn{position:relative;flex:none;display:grid;place-items:center;width:46px;height:46px;margin:0;padding:0;border:1.5px solid var(--line);border-radius:50%;background:var(--panel2);color:var(--muted);cursor:pointer;transition:background .12s,color .12s,border-color .12s}
  #moreBtn svg{position:static;transform:none;width:19px;height:19px;color:inherit}
  #moreBtn:hover{color:var(--ink);border-color:var(--faint)}
  #moreBtn[aria-expanded="true"]{background:var(--ink);color:var(--panel);border-color:var(--ink)}
  #moreN{position:absolute;top:-4px;right:-4px;border:2px solid var(--panel)}
  #didyou{margin:-5px 20px 10px}
  #filters{flex:none;display:none;padding:2px 0 4px;max-height:46vh;overflow:auto;scrollbar-width:thin;border-top:1px solid var(--line)}
  #side.more-open #filters{display:block}
  #quick{padding:12px 16px 2px}
  #more{padding:8px 16px 4px;max-height:none;overflow:visible}
  #more[hidden]{display:grid!important}
  .frow>.lab,#more .frow>.lab{flex-basis:49px}
  #sub{margin:8px 16px 8px;font-size:11.5px;color:var(--faint)}
  #bar{padding:8px 10px 8px 18px;background:transparent;border-bottom:0}
  #head,#searchbox,#bar,#locpill,#didyou{flex:none}
  #count{flex:1 0 auto;overflow:visible}
  #sortby{flex:0 1 96px;min-width:60px;width:auto}
  #bar .chip{flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis}
  #listBtn{flex:none;display:inline-flex;align-items:center;gap:6px;height:30px;padding:0 12px;border:0;border-radius:999px;background:var(--accent);color:var(--on-accent);font-size:12px;font-weight:650;cursor:pointer;white-space:nowrap}
  #listBtn:hover{filter:brightness(1.08)}
  #listBtn:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  #listBtn .chev{font-size:9px;transition:transform .15s}
  #side:not(.mini) #listBtn{background:var(--panel2);color:var(--muted);border:1px solid var(--line)}
  #side:not(.mini) #listBtn .chev{transform:rotate(180deg)}
  #side:not(.mini) #bar{border-bottom:1px solid var(--line)}
  #side.mini #list,#side.mini #locpill,#side.mini #inviewBtn,#side.mini #sortby,#side.mini #reset{display:none!important}
  #list{flex:0 1 auto;min-height:0}
  .row:last-child{border-bottom:0}
  #foot{position:absolute;z-index:4;left:50%;bottom:8px;transform:translateX(-50%);gap:12px;padding:4px 12px;border:1px solid var(--line);border-radius:999px;font-size:11px;white-space:nowrap;
        background:var(--panel);background:color-mix(in srgb,var(--panel) 82%,transparent);-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px)}
  #toast{left:calc(50% + 207px);max-width:calc(100% - 560px)}
  @media (max-width:1100px){#toast{left:auto;right:60px;transform:none;top:auto;bottom:44px;max-width:calc(100% - 80px)}}
  @media (prefers-reduced-motion:no-preference){#side{transition:box-shadow .2s}#list{animation:dnmIn .18s ease-out}}
  @keyframes dnmIn{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:none}}
  @media (max-width:760px){
    #wrap{flex-direction:initial}
    #side{top:8px;left:8px;right:8px;width:auto;max-width:none;flex-basis:auto;border:1px solid var(--line);border-radius:20px;max-height:calc(100dvh - 16px)}
    #side:not(.mini){max-height:min(64dvh,calc(100dvh - 16px))}
    #side.more-open:not(.mini){max-height:calc(100dvh - 16px)}
    #head{padding:10px 14px 6px}#head h1{font-size:17px}
    #searchbox{margin:0 10px 10px}
    #quick{padding:10px 12px 2px}#more{padding:8px 12px 4px}#sub{display:block;margin:8px 12px}
    #side:not(.more-open) #quick{display:grid}
    #moreBtn{width:46px;margin:0;padding:0;border:1.5px solid var(--line)}
    #filters{max-height:38dvh}
    #list{min-height:0}
    #bar{padding:7px 8px 7px 14px}
    #foot{bottom:6px;left:8px;transform:none;padding:3px 10px}
    #map .maplibregl-ctrl-bottom-right{bottom:28px}
    #toast{left:8px;right:auto;bottom:68px;top:auto;transform:none;max-width:calc(100% - 70px)}
    #map .maplibregl-ctrl-top-right{top:auto;bottom:62px}
    #map .maplibregl-popup-content{max-height:calc(46dvh - 70px)}
  }
  #areaBtn{position:absolute;z-index:4;top:16px;left:calc(50% + 207px);transform:translateX(-50%);height:36px;padding:0 16px;border:1px solid var(--line);border-radius:999px;background:var(--panel);color:var(--accent);font-size:13px;font-weight:650;cursor:pointer;box-shadow:var(--shadow);white-space:nowrap}
  #areaBtn:hover{background:var(--panel2)}
  #sub{display:none!important}
  #loading{position:absolute;z-index:2;left:50%;top:50%;transform:translate(-50%,-50%);display:flex;align-items:center;gap:10px;padding:10px 16px;border-radius:999px;background:var(--panel);border:1px solid var(--line);box-shadow:var(--shadow);color:var(--muted);font-size:13px;font-weight:600}
  #loading .spin{width:16px;height:16px;border-radius:50%;border:2.5px solid var(--line);border-top-color:var(--accent);animation:dnmSpin .8s linear infinite}
  @keyframes dnmSpin{to{transform:rotate(360deg)}}
  #legend{position:absolute;z-index:3;left:14px;bottom:10px;display:flex;gap:12px;padding:5px 12px;border:1px solid var(--line);border-radius:999px;font-size:11px;color:var(--muted);white-space:nowrap;
          background:var(--panel);background:color-mix(in srgb,var(--panel) 82%,transparent);-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px)}
  #legend span{display:inline-flex;align-items:center;gap:5px}
  #legend i{width:9px;height:9px;border-radius:50%;display:inline-block}
  #legend i.bk{background:transparent;border:2.5px solid #16a34a}
  @media (max-width:760px){#legend{left:8px;bottom:36px;gap:9px;padding:4px 10px}}
  @media (max-width:480px){#legend{display:none}}   /* narrow phones: pins now carry type glyphs + the filter chips show the full key, so the minimal legend is dropped to free the crowded bottom bar (it was colliding with the OSM attribution + info button) */
  @media (max-width:1100px){#areaBtn{left:auto;right:60px;transform:none}}
  @media (max-width:760px){#areaBtn{top:auto;bottom:74px;left:50%;right:auto;transform:translateX(-50%)}}

  /* ===================== design pass (2026-11-27) — presentation only, no data or behaviour change ===================== */
  body{font-feature-settings:"cv11","ss01";text-rendering:optimizeLegibility}
  /* card */
  #side{border-radius:24px;border-color:color-mix(in srgb,var(--line) 75%,transparent);
        box-shadow:0 18px 50px rgba(15,23,42,.18),0 3px 10px rgba(15,23,42,.07);
        background:color-mix(in srgb,var(--panel) 93%,transparent);-webkit-backdrop-filter:blur(24px) saturate(1.6);backdrop-filter:blur(24px) saturate(1.6)}
  #head{padding:14px 16px 10px}
  #head h1{font-size:19px;letter-spacing:-.035em}
  #head h1 .mark{width:30px;height:30px;border-radius:9px}
  #tagline{background:transparent;border:0;padding:0 0 0 2px;font-size:11px;font-weight:600;letter-spacing:.02em;text-transform:none;color:var(--faint)}
  .icon-btn{border-radius:50%;width:34px;height:34px;border-color:transparent;background:transparent;transition:background .12s,color .12s}
  .icon-btn:hover{background:var(--panel2);border-color:transparent;color:var(--ink)}
  /* search */
  #searchbox{margin:0 14px 12px;gap:8px}
  #q{height:48px;font-size:15px;border:1.5px solid transparent;background:var(--panel2);transition:border-color .12s,box-shadow .12s,background .12s}
  #q:hover{border-color:var(--line)}
  #q:focus{border-color:var(--accent);background:var(--panel);box-shadow:0 0 0 4px var(--accent-soft)}
  #qhint{right:68px;border-radius:7px}#qclear{right:62px}
  #moreBtn{width:48px;height:48px;border:1.5px solid transparent;background:var(--panel2)}
  #moreBtn:hover{border-color:var(--line)}
  #moreBtn[aria-expanded="true"]{background:var(--accent);color:var(--on-accent);border-color:var(--accent)}
  /* filters */
  #filters{max-height:52vh;padding:0}
  #quick{padding:14px 16px 4px;gap:14px}
  #more{padding:12px 16px 14px;gap:14px}
  .frow{flex-direction:column;gap:7px}
  .frow>.lab,#more .frow>.lab{display:block;flex:none;padding:0;font-size:10.5px;font-weight:700;letter-spacing:.08em;color:var(--faint)}
  .chips{gap:6px}
  .chip{height:30px;padding:0 11px;font-size:12.5px;font-weight:550;background:transparent;color:var(--ink);border:1px solid var(--line)}
  .chip:hover{background:var(--panel2);border-color:var(--line);color:var(--ink)}
  .chip .n{color:var(--faint);opacity:1;font-size:11.5px;margin-left:5px}
  .chip .dot{width:9px;height:9px;margin-right:6px}
  .chip.on{background:var(--accent-soft);border-color:var(--accent);color:var(--ink);font-weight:650}
  .chip.on .n{color:var(--muted)}
  #availChip.on .dot{background:#22c55e!important;opacity:1}
  .chip.tc.on{background:var(--accent-soft);background:color-mix(in srgb,var(--c) 14%,var(--panel));border-color:var(--c);border-color:color-mix(in srgb,var(--c) 75%,transparent)}
  .chip.tc.on .dot{background:var(--c)!important;opacity:1}
  .chip.tc:not(.on) .dot{opacity:.55}
  .chip.allied-master{border-style:dashed}
  #spec{height:38px;border-radius:12px;padding:0 32px 0 12px}
  /* results bar */
  #bar{gap:8px;padding:10px 12px 10px 18px}
  #count{font-size:12.5px}
  #count b{font-size:16px;letter-spacing:-.015em}
  #listBtn{height:32px;padding:0 14px;font-size:12.5px}
  #side:not(.mini) #bar{flex-wrap:wrap;row-gap:8px}
  #side:not(.mini) #count{order:1;flex:1 1 calc(100% - 96px)}
  #side:not(.mini) #listBtn{order:2}
  #side:not(.mini) #inviewBtn{order:3;flex:none;overflow:visible;height:32px;padding:0 12px;font-size:12px}
  #side:not(.mini) #sortby{order:4;flex:1 1 auto;width:auto;min-width:0;height:32px;border-radius:999px;padding:0 10px;background:transparent}
  #side:not(.mini) #reset{order:5;width:32px;height:32px;border:1px solid var(--line)}
  /* result rows: type avatar (same colour + glyph as the map pin) */
  .av{flex:none;display:grid;place-items:center;width:34px;height:34px;border-radius:50%;box-shadow:0 1px 3px rgba(15,23,42,.2)}
  .av img{display:block;width:26px;height:26px}
  .av.lg{width:42px;height:42px}.av.lg img{width:32px;height:32px}
  .row{padding:12px 16px 12px 62px;transition:background .1s}
  .row>.av{position:absolute;left:16px;top:13px}
  .nm{font-size:14.5px;letter-spacing:-.01em}
  .r2{margin-top:1px}
  .r3{margin-top:6px;gap:5px}
  .b{border-radius:999px;padding:2px 8px}
  .row.kb{box-shadow:inset 3px 0 0 var(--accent)}
  .empty{padding:44px 28px}
  /* popup card */
  #map .maplibregl-popup-content{padding:16px 16px 14px;border-radius:18px;min-width:272px;box-shadow:0 18px 48px rgba(15,23,42,.22),0 2px 8px rgba(15,23,42,.08)}
  #map .maplibregl-popup-close-button{right:8px;top:8px;width:28px;height:28px;border-radius:50%}
  .pop .phead{display:flex;align-items:center;gap:11px;padding-right:26px;margin-bottom:10px}
  .pop .pht{min-width:0}
  .pop .pn{font-size:16px;letter-spacing:-.015em}
  .pop .ps{color:var(--muted);font-size:12.5px;margin-top:1px}
  .pop .mo{margin-top:0;line-height:1.5}
  .pop .mo b{color:var(--ink);font-weight:600}
  .pop .acts{display:flex;flex-wrap:wrap;gap:6px;margin-top:11px}
  .pop .act{display:inline-flex;align-items:center;gap:6px;height:32px;padding:0 12px;border-radius:999px;border:1px solid var(--line);background:var(--panel);color:var(--ink);font-size:12.5px;font-weight:600;white-space:nowrap;transition:background .12s}
  .pop .act:hover{background:var(--panel2);text-decoration:none}
  .pop .act svg{flex:none;width:14px;height:14px;color:var(--accent)}
  .pop .avail{margin:12px 0 2px;padding:10px 12px;border-radius:12px}
  .pop .rv{margin-top:11px}
  .pop .links{margin-top:11px;padding-top:9px;color:var(--faint);font-size:11.5px}
  .pop .links a{font-weight:600}
  .grp .gi{border-radius:10px}
  /* map chrome: controls, attribution, legend follow the theme */
  #map .maplibregl-ctrl-group{border-radius:14px;background:var(--panel);border:1px solid var(--line);box-shadow:0 4px 16px rgba(15,23,42,.14)}
  #map .maplibregl-ctrl-group button{width:36px;height:36px;background-color:transparent}
  #map .maplibregl-ctrl-group button+button{border-top:1px solid var(--line)}
  #map .maplibregl-ctrl-group button:hover{background-color:var(--panel2)}
  #map .maplibregl-ctrl-attrib{font-size:10.5px;color:var(--muted);background:var(--panel);background:color-mix(in srgb,var(--panel) 82%,transparent);-webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px)}
  #map .maplibregl-ctrl-attrib a{color:var(--muted)}
  @media (prefers-color-scheme:dark){:root:not([data-theme="light"]) #map .maplibregl-ctrl-icon,:root:not([data-theme="light"]) #map .maplibregl-ctrl-attrib-button{filter:invert(1) brightness(1.6)}}
  :root[data-theme="dark"] #map .maplibregl-ctrl-icon,:root[data-theme="dark"] #map .maplibregl-ctrl-attrib-button{filter:invert(1) brightness(1.6)}
  #legend{flex-wrap:wrap;gap:5px 14px;max-width:min(560px,calc(100vw - 470px));padding:7px 13px;border-radius:16px;font-size:11.5px;box-shadow:0 4px 16px rgba(15,23,42,.1)}
  #areaBtn{height:38px;padding:0 18px;border-color:transparent;box-shadow:0 8px 24px rgba(15,23,42,.18)}
  #foot{box-shadow:0 4px 16px rgba(15,23,42,.08)}
  #loading{padding:11px 18px;box-shadow:0 8px 24px rgba(15,23,42,.16)}
  @media (max-width:760px){
    #side{border-radius:22px}
    #q{font-size:16px}#spec,#sortby{font-size:16px}
    #filters{max-height:44dvh}
    #legend{max-width:calc(100vw - 16px)}
    .row{padding-right:14px}
  }
  /* --- round 2: quick category pills, collapsible allied group, mobile bottom sheet --- */
  #topcard,#sheet{display:contents}
  #grab,#sheetfoot{display:none}
  #cats{flex:none;display:flex;gap:6px;overflow-x:auto;padding:0 14px 12px;scrollbar-width:none;-webkit-overflow-scrolling:touch;
        -webkit-mask-image:linear-gradient(90deg,#000 calc(100% - 26px),transparent);mask-image:linear-gradient(90deg,#000 calc(100% - 26px),transparent)}
  #cats::-webkit-scrollbar{display:none}
  #cats:empty,#side.more-open #cats{display:none}
  .cat{flex:none;display:inline-flex;align-items:center;height:30px;padding:0 11px;border-radius:999px;border:1px solid var(--line);background:transparent;color:var(--ink);font-size:12.5px;font-weight:550;line-height:1;cursor:pointer;white-space:nowrap;user-select:none;transition:background .12s,border-color .12s}
  .cat:hover{background:var(--panel2)}
  .cat:focus-visible,.chipx:focus-visible,#grab:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  .cat .dot{width:9px;height:9px;border-radius:50%;margin-right:6px;flex:none;opacity:.55}
  .cat.on{font-weight:650;background:var(--accent-soft);background:color-mix(in srgb,var(--c) 14%,var(--panel));border-color:var(--c);border-color:color-mix(in srgb,var(--c) 75%,transparent)}
  .cat.on .dot{opacity:1}
  .cat.more{color:var(--muted);margin-right:18px}
  .chipx{display:inline-flex;align-items:center;gap:5px;height:30px;padding:0 10px;border:0;border-radius:999px;background:transparent;color:var(--accent);font-size:12.5px;font-weight:600;cursor:pointer;white-space:nowrap}
  .chipx:hover{background:var(--accent-soft)}
  .chipx .chev{font-size:10px;transition:transform .15s}
  #types.allied-open .chipx .chev{transform:rotate(180deg)}
  #types:not(.allied-open) .chip.ap{display:none}
  #specwrap{flex:none;width:100%}
  @media (max-width:760px){
    #side{display:contents}
    #topcard{display:flex;flex-direction:column;position:absolute;z-index:5;top:8px;left:8px;right:8px;max-height:calc(100dvh - 150px);overflow:hidden;
             border:1px solid color-mix(in srgb,var(--line) 75%,transparent);border-radius:22px;box-shadow:0 12px 36px rgba(15,23,42,.18),0 2px 6px rgba(15,23,42,.07);
             background:var(--panel);background:color-mix(in srgb,var(--panel) 93%,transparent);-webkit-backdrop-filter:blur(24px) saturate(1.6);backdrop-filter:blur(24px) saturate(1.6)}
    #head{padding:10px 14px 8px}#tagline{display:none}
    #searchbox{margin:0 10px 10px}
    #cats{padding:0 10px 10px}
    #filters{max-height:46dvh}
    #sheet{display:flex;flex-direction:column;position:absolute;z-index:6;left:0;right:0;bottom:0;max-height:64dvh;
           border:1px solid var(--line);border-bottom:0;border-radius:22px 22px 0 0;background:var(--panel);
           box-shadow:0 -10px 36px rgba(15,23,42,.16);padding-bottom:env(safe-area-inset-bottom)}
    #grab{display:block;flex:none;position:relative;height:16px;cursor:grab;touch-action:none}
    #grab::before{content:"";position:absolute;left:50%;top:7px;width:40px;height:4px;margin-left:-20px;border-radius:2px;background:var(--faint);opacity:.45}
    #sheet #bar{border-top:0;padding:2px 10px 10px 16px}
    #side:not(.mini) #sheetfoot{display:flex;flex:none;justify-content:center;gap:18px;padding:9px 12px;border-top:1px solid var(--line);font-size:11.5px}
    #sheetfoot a{color:var(--muted);text-decoration:none;font-weight:500}
    #foot,#legend{display:none!important}
    #map .maplibregl-ctrl-bottom-right{bottom:70px}
    #map .maplibregl-ctrl-top-right{top:auto;bottom:104px}
    #areaBtn{bottom:84px}
    #toast{bottom:128px}
    #map .maplibregl-popup-content{max-height:calc(52dvh - 110px)}
  }
  /* --- round 3: motion, depth and feedback (all decorative; honours prefers-reduced-motion below) --- */
  :root{--ez:cubic-bezier(.2,.8,.2,1);--grad:linear-gradient(135deg,#2563eb,#0891b2)}
  .chip,.cat,.chipx,#listBtn,#moreBtn,.icon-btn,.pop .act{transition:background .14s var(--ez),border-color .14s var(--ez),color .14s var(--ez),transform .1s var(--ez),box-shadow .16s var(--ez)}
  .chip:active,.cat:active,.chipx:active,#listBtn:active,#moreBtn:active,.icon-btn:active,.pop .act:active{transform:scale(.95)}
  #q{caret-color:var(--accent)}
  /* brand gradient on the primary actions */
  #head h1 .mark{box-shadow:0 4px 14px rgba(37,99,235,.38)}
  #listBtn{background:var(--grad);color:#fff;box-shadow:0 4px 14px rgba(37,99,235,.3)}
  #listBtn:hover{filter:none;box-shadow:0 6px 18px rgba(37,99,235,.42)}
  #side:not(.mini) #listBtn{box-shadow:none}
  #moreBtn[aria-expanded="true"]{background:var(--grad);border-color:transparent;color:#fff;box-shadow:0 4px 14px rgba(37,99,235,.3)}
  #areaBtn,#areaBtn:hover{background:var(--grad);color:#fff}
  #areaBtn:hover{box-shadow:0 10px 26px rgba(37,99,235,.42)}
  /* avatars: a touch of depth */
  .av{background:var(--c);background:linear-gradient(145deg,color-mix(in srgb,var(--c) 76%,#fff),var(--c) 62%);box-shadow:0 1px 3px rgba(15,23,42,.2),inset 0 0 0 1px rgba(255,255,255,.14)}
  .av.sm{width:24px;height:24px}.av.sm img{width:18px;height:18px}
  .row>.av{transition:transform .16s var(--ez)}
  .row:hover>.av,.row.kb>.av{transform:scale(1.08)}
  .row{content-visibility:auto;contain-intrinsic-size:auto 86px}
  .row:active{background:var(--accent-soft)}
  .row.sel{box-shadow:inset 3px 0 0 var(--accent)}
  #list::-webkit-scrollbar{width:9px}
  #list::-webkit-scrollbar-thumb{background:var(--line);border-radius:9px;border:2.5px solid transparent;background-clip:content-box}
  /* entrances */
  @keyframes dnmDrop{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:none}}
  @keyframes dnmRise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
  @keyframes dnmFade{from{opacity:0}to{opacity:1}}
  @keyframes dnmPop{from{opacity:0;scale:.94}to{opacity:1;scale:1}}
  @keyframes dnmSheet{from{transform:translateY(46%)}to{transform:none}}
  @keyframes dnmShim{to{background-position:-100% 0}}
  @keyframes dnmSlide{from{transform:translateX(-110%)}to{transform:translateX(280%)}}
  @media (prefers-reduced-motion:no-preference){
    #side.more-open #filters{animation:dnmDrop .2s var(--ez)}
    #side:not(.mini) #list{animation:dnmRise .22s var(--ez)}
    #map .maplibregl-popup{animation:dnmPop .17s var(--ez);transform-origin:50% 100%}
    #areaBtn:not([hidden]),#toast:not([hidden]),#legend{animation:dnmFade .25s ease-out}
    #types.allied-open .chip.ap{animation:dnmDrop .18s var(--ez) both}
    @media (max-width:760px){#side:not(.mini) #sheet{animation:dnmSheet .26s var(--ez)}#side:not(.mini) #list{animation:none}}
  }
  /* while the search index is still arriving: a slim progress shimmer across the top of the card */
  #head{position:relative}
  body:not(.hyd) #head::before{content:"";position:absolute;left:0;top:0;width:36%;height:3px;border-radius:3px;background:linear-gradient(90deg,transparent,#2563eb,#06b6d4,transparent);animation:dnmSlide 1s linear infinite}
  /* loading skeletons (list + popup) */
  .skr i,.skr b,.skr span,.skp i,.skp b,.skp span{display:block;border-radius:8px;background:linear-gradient(90deg,var(--panel2) 25%,var(--line) 37%,var(--panel2) 63%);background-size:200% 100%;background-position:100% 0;animation:dnmShim 1.1s linear infinite}
  .skr{position:relative;height:80px;padding:15px 16px 0 62px;border-bottom:1px solid var(--line)}
  .skr i{position:absolute;left:16px;top:14px;width:34px;height:34px;border-radius:50%}
  .skr b{width:46%;height:13px}
  .skr span{width:74%;height:10px;margin-top:10px}
  .skp{min-width:236px}
  .skp .skh{display:flex;align-items:center;gap:11px;margin-bottom:13px}
  .skp .skh i{flex:none;width:42px;height:42px;border-radius:50%}
  .skp .skh div{flex:1}
  .skp .skh b{width:72%;height:14px}
  .skp .skh span{width:46%;height:10px;margin-top:8px}
  .skp>span{width:90%;height:10px;margin-top:8px}
  .skp>span.s2{width:58%}
  /* empty state */
  .empty b::before{content:"";display:block;width:46px;height:46px;margin:0 auto 12px;border-radius:50%;background:var(--panel2) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%238793a0' stroke-width='2' stroke-linecap='round'%3E%3Ccircle cx='11' cy='11' r='7'/%3E%3Cpath d='M20 20l-3.6-3.6M8.5 11h5'/%3E%3C/svg%3E") center/22px no-repeat}
  /* dark theme: lift the type colours so dots and active pills keep their contrast on the dark card */
  @media (prefers-color-scheme:dark){
    :root:not([data-theme="light"]) .cat .dot,:root:not([data-theme="light"]) .chip.tc .dot{filter:brightness(1.6) saturate(1.15)}
    :root:not([data-theme="light"]) .cat.on,:root:not([data-theme="light"]) .chip.tc.on{background:color-mix(in srgb,var(--c) 32%,var(--panel));border-color:color-mix(in srgb,var(--c) 55%,#fff)}
  }
  :root[data-theme="dark"] .cat .dot,:root[data-theme="dark"] .chip.tc .dot{filter:brightness(1.6) saturate(1.15)}
  :root[data-theme="dark"] .cat.on,:root[data-theme="dark"] .chip.tc.on{background:color-mix(in srgb,var(--c) 32%,var(--panel));border-color:color-mix(in srgb,var(--c) 55%,#fff)}
  @media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
</style>
</head>
<body>
<div id="wrap">
  <aside id="side" class="mini" aria-label="Search and results">
    <div id="topcard">
    <header id="head">
      <div class="brand"><h1><svg class="mark" viewBox="0 0 32 32" aria-hidden="true"><defs><linearGradient id="dnm" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#2563eb"/><stop offset="1" stop-color="#06b6d4"/></linearGradient></defs><rect width="32" height="32" rx="9" fill="url(#dnm)"/><path d="M16 6.2c-4.5 0-8 3.4-8 7.8 0 5.5 6.300 10.800 7.300 11.600.4.300.980.300 1.400 0 1-.8 7.300-6.100 7.300-11.600 0-4.400-3.500-7.800-8-7.800z" fill="#fff"/><path d="M14.600 10.200h2.800v2.600H20v2.800h-2.600v2.600h-2.800v-2.600H12v-2.800h2.600z" fill="#2563eb"/></svg><span class="wm">doc<b>near</b>me</span></h1><span id="tagline">Specialist prospects</span><span class="grow"></span>
        <button type="button" class="icon-btn" id="themeBtn" title="Switch light / dark" aria-label="Switch between light and dark theme"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M2.5 12h2M19.5 12h2M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/></svg></button></div>
    </header>
    <div id="searchbox">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.6-3.6"/></svg>
      <input type="search" id="q" placeholder="e.g. cardiologist, Chatswood, Dr Smith" autocomplete="off" autocapitalize="off" spellcheck="false" aria-label="Search by name, practice, suburb, postcode, specialty or phone">
      <kbd id="qhint">/</kbd>
      <button type="button" class="icon-btn" id="qclear" hidden aria-label="Clear search">×</button>
      <button type="button" id="moreBtn" aria-expanded="false" aria-controls="filters" title="Filters" aria-label="Filters"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M4 7h16M7 12h10M10 17h4"/></svg><span id="moreN" hidden></span></button>
    </div>
    <div id="cats" role="group" aria-label="Quick categories"></div>
    <div id="didyou" hidden aria-live="polite"></div>
    <div id="filters">
    <div id="quick">
      <div class="frow" id="typeRow" hidden><span class="lab">Type</span><div class="chips" id="types"></div></div>
      <div class="frow"><span class="lab" title="A = top priority → D">Tier</span><div class="chips" id="tiers"></div></div>
      <div class="frow"><span class="lab">Show</span><div class="chips" id="flags">
        <button type="button" class="chip" id="availChip" hidden aria-pressed="false" title="Bookable on HotDoc"><span class="dot" style="background:#22c55e"></span>Bookable</button>
        <button type="button" class="chip" id="ratedChip" hidden aria-pressed="false" title="Has a review score">★ Rated</button>

      </div></div>
    </div>
    <div id="more" hidden>
      <div class="frow"><span class="lab">Specialty</span><div id="specwrap">
        <input type="text" id="spec" list="speclist" placeholder="All specialties — type any part, e.g. cardio" autocomplete="off" spellcheck="false" aria-label="Filter by specialty">
        <datalist id="speclist"></datalist>
        <button type="button" class="icon-btn" id="specclear" hidden aria-label="Clear specialty">×</button></div></div>
      <div class="frow"><span class="lab">State</span><div class="chips" id="states"></div></div>
    </div>
    <p id="sub"></p>
    </div>
    </div>
    <div id="sheet">
    <div id="grab" role="button" tabindex="-1" aria-label="Show or hide the results list"></div>
    <div id="bar">
      <div id="count" aria-live="polite"></div>
      <button type="button" class="chip" id="inviewBtn" aria-pressed="false" title="Only list practitioners inside the current map view">In map view</button>
      <select id="sortby" aria-label="Sort results">
        <option value="tier">Best tier</option>
        <option value="avail">Soonest</option>
        <option value="rating">Top rated</option>
        <option value="name">Name A–Z</option>
      </select>
      <button type="button" class="icon-btn" id="reset" title="Clear all filters" aria-label="Clear all filters">↺</button>
      <button type="button" id="listBtn" aria-expanded="false" aria-controls="list"><span class="lb-t">Show list</span><span class="chev">▼</span></button>
    </div>
    <div id="locpill" hidden><span id="loctext"></span><button type="button" id="locclear" aria-label="Clear location filter">×</button></div>
    <div id="list" role="listbox" aria-label="Matching practitioners" tabindex="-1"></div>
    <nav id="sheetfoot" aria-label="Site links"><a href="/about">About</a><a href="/privacy">Privacy</a><a href="/terms">Terms</a><a href="/contact">Contact</a></nav>
    </div>
  </aside>
  <div id="mapwrap"><div id="map"></div><div id="loading" role="status"><span class="spin"></span>Loading the map…</div><div id="mapmsg" hidden></div>
    <div id="legend" aria-label="Map key"><span><i style="background:#1d4ed8"></i>GP</span><span><i style="background:#0891b2"></i>Specialist</span><span><i class="bk"></i>Bookable online</span></div>
    <button type="button" id="areaBtn" hidden>Search this area</button>
    <footer id="foot" hidden><a href="/about">About</a><a href="/privacy">Privacy</a><a href="/terms">Terms</a><a href="/contact">Contact</a></footer>
    <div id="toast" hidden role="status"><span id="toastText"></span><button type="button" id="toastAct" hidden></button><button type="button" class="x" id="toastX" aria-label="Dismiss">×</button></div></div>
</div>
<script>
let DATA=[], META, OV=[], __booted=false, __hydrated=false, __hydrateFn=null;
const KIND=['gp','spec','diag','allied','pharmacy','hospital','physio','psych','dental'];// overview kindCode -> kind
// Phase 1 (overview ~601 KB): META + compact rows. Boots the MAP + chrome immediately.
function __dnmOv(d){
  if(__booted)return;
  META=d.meta;
  OV=d.ov.map(a=>({__i:a[3],lat:a[0],lng:a[1],k:KIND[a[2]]||'spec',rv:a[4]==null?undefined:{score:a[4]}}));
  __booted=true;__boot();
  if(window.__DNM_IX)__dnmIx(window.__DNM_IX);                                   // index already here? hydrate now
}
// Phase 2 (index, deferred): full list/search fields per id, MINUS em. Hydrates DATA, enables search + filters.
function __dnmIx(ix){
  if(!__booted||__hydrated)return;                                              // need OV first (coords/kind per row)
  __hydrated=true;
  DATA=ix.map((r,i)=>{r.__i=i;const o=OV[i]||{};r.lat=o.lat;r.lng=o.lng;r.k=o.k;if(!r.rv&&o.rv)r.rv=o.rv;return r;});
  if(__hydrateFn)__hydrateFn();
}
window.__dnmOv=__dnmOv;window.__dnmIx=__dnmIx;
function __boot(){
const TCOL={A:'#15a34a',B:'#2f6fed',C:'#f0a92b',D:'#9aa7b4','?':'#9aa7b4'};
function tcol(t){return TCOL[t]||'#9aa7b4';}
const TB={A:0,B:1,C:2,D:3,'?':4};
const $=id=>document.getElementById(id);
const ESC={'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'};
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>ESC[c]);}
function num(n){return Number(n||0).toLocaleString();}
function safeUrl(u){u=String(u==null?'':u).trim();return /^(https?:|mailto:|tel:)/i.test(u)?esc(u):'#';}

// >>> SEARCH ENGINE (pure functions over DATA/META; the same text is run by the offline test harness)
// Every word typed must match. A word matches at the START of a word in the record, so "uro" finds Urologist but not
// Neurologist. On top of that:
//  - specialty words are reduced to a stem (cardiology = cardiologist = cardiologists)
//  - US and Australian spellings are interchangeable (pediatric = paediatric)
//  - everyday words and abbreviations map to the specialty (eye, heart, skin, kids, gp, ent ...)
//  - filler words are ignored ("gp near me", "a cardiologist in sydney"), titles too ("dr smith")
//  - a word that finds (almost) nothing but is a near-miss of a real word is treated as a typo and shown as such
function norm(s){
  s=String(s==null?'':s);
  if(s.normalize)s=s.normalize('NFKC');
  s=s.toLowerCase();
  if(s.normalize)s=s.normalize('NFD').replace(/[̀-ͯ]/g,'');
  return s.replace(/[^a-z0-9]+/g,' ').trim();
}
function mkset(s){const o=Object.create(null);s.split(' ').forEach(w=>{if(w)o[w]=1;});return o;}
function has(o,k){return Object.prototype.hasOwnProperty.call(o,k);}
const VARIANTS=[['paed','ped'],['gynaec','gynec'],['haem','hem'],['anaesth','anesth'],['oesoph','esoph'],['oestr','estr'],
  ['tumour','tumor'],['centre','center'],['foet','fet'],['coeliac','celiac'],['leukaem','leukem'],['orthopaed','orthoped'],
  ['anaem','anem'],['caesar','cesar'],['apnoea','apnea'],['oedem','edem'],['behaviour','behavior'],['diarrhoea','diarrhea'],
  ['dyspnoea','dyspnea'],['labour','labor'],['counsell','counsel']];
const MEDSTEMS=8;                 // the first rules are medical word families; the rest are generic endings
const STEMS=[[/^(.{3,})(surgeons|surgeon|surgery|surgical)$/,'$1surg'],
  [/(ologists|ologist|ologis|ologi|ological|ologic|ologies|ology)$/,'olog'],
  [/(iatricians|iatrician|iatricia|iatrici|iatritions|iatrition|iatritian|iatrists|iatrist|iatris|iatrics|iatric|iatry|iatri)$/,'iatr'],
  [/(etricians|etrician|etricia|etrici|etritions|etrition|etritian|etrics|etric)$/,'etr'],
  [/(ometrists|ometrist|ometry)$/,'ometr'],
  [/(therapists|therapist|therapies|therapy|therapeutic)$/,'therap'],
  [/(graphers|grapher|graphy)$/,'graph'],
  [/(dentistry|dentists|dentist)$/,'dentist'],
  [/(ists|ist)$/,''],[/ics$/,'ic'],[/ies$/,'y'],[/([^s])s$/,'$1']];
let STEMGEN=false;                // set by stem(): true when the stem came from a generic ending
function stem(w,medOnly){
  STEMGEN=false;
  if(w.length<5||/\d/.test(w))return '';
  if(/^ana?esth/.test(w))return 'anaesth';
  if(w.length<6)return '';
  const n=medOnly?MEDSTEMS:STEMS.length;
  for(let i=0;i<n;i++){const re=STEMS[i][0];
    if(re.test(w)){const s=w.replace(re,STEMS[i][1]);if(s.length>=4&&s!==w){STEMGEN=i>=MEDSTEMS;return s;}return '';}}
  return '';
}
// everyday word -> what to look for. A trailing $ means "this exact word" (not "words starting with").
const SYN=(function(){
  const o=Object.create(null),put=(keys,vals)=>keys.split(' ').forEach(k=>{o[k]=vals;});
  put('gp gps',['general practitioner','general practice']);
  put('doctor doctors',['general practitioner']);
  put('ent ear ears nose throat hearing deaf audiologist audiologists audiology',['otolaryngolog','ear nose']);
  put('eye eyes vision eyesight glasses optician opticians optical',['ophthalmolog','optometr']);
  put('heart',['cardiolog','cardiothoracic','cardiac']);put('cardiac',['cardiolog','cardiothoracic']);
  put('skin derm',['dermatolog']);
  put('kid kids child children childrens baby babies babys infant infants newborn newborns toddler toddlers',['paediatr','neonat']);
  put('paeds peds',['paediatr']);put('teen teens teenager teenagers',['paediatr','adolescent']);put('neonatal nicu',['neonat']);
  put('bone bones joint joints',['orthopaed','rheumatolog']);put('ortho orthopod orthopods',['orthopaed']);
  put('obgyn obgyns',['obstetr','gynaecolog']);put('obs',['obstetr']);put('gyn gyno gynae gynie gyni',['gynaecolog']);
  put('women womens woman womans',['gynaecolog','obstetr']);
  put('womenshealth',['gynaecolog','obstetr','women s health','womens health']);
  put('men mens',['urolog','androlog','sexual health']);
  put('menshealth',['urolog','androlog','sexual health','men s health','mens health']);
  put('pregnancy pregnant',['obstetr']);put('fertility',['infertility','ivf']);put('mfm',['maternal fetal','maternal foetal']);
  put('kidney kidneys renal',['nephrolog']);
  put('lung lungs chest',['respiratory','thoracic','pulmon']);put('breathing pulmonary pulmonologist pulmonologists pulmonology',['respiratory','pulmon']);
  put('brain',['neurolog','neurosurg']);put('nerve nerves',['neurolog']);
  put('blood',['haematolog']);put('cancer tumour tumor chemo chemotherapy',['oncolog']);put('radiotherapy radiotherapist',['radiation oncolog']);
  put('hormone hormones hormon hormonal diabetes thyroid endocrine',['endocrinolog']);
  put('gut bowel',['gastroenterolog','colorectal']);put('stomach digestive',['gastroenterolog']);
  put('gi',['gastroenterolog','gastrointestinal']);put('gastrointestinal',['gi$']);
  put('liver',['hepatolog','gastroenterolog']);put('colon rectal rectum',['colorectal']);
  put('allergy allergies',['allerg','immunolog']);put('arthritis',['rheumatolog']);
  put('mental',['psychiatr','psycholog']);put('shrink',['psychiatr']);put('physio',['physiotherap']);
  put('urinary bladder prostate',['urolog']);put('elderly aged',['geriatr']);
  put('teeth tooth dentist dentists dentistry',['dentist','dental surg','oral and maxillofacial']);
  put('foot feet',['podiatr']);put('spine back',['spine','spinal']);
  put('weight obesity',['bariatr']);put('weightloss',['bariatr','weight loss','obesity']);
  put('anaesthesia anesthesia',['anaesth']);
  put('surgery surgeries surgical surgeons',['surgeon']);put('gensurg',['general surg']);
  put('genmed',['physician general','general physician','general medicine','internal medicine']);
  put('icu',['intensive care']);put('ed er',['emergency']);put('sti stis std stds',['sexual health']);
  put('mri ct ctscan scan scans imaging',['radiolog','nuclear medicine']);put('xray xrays',['radiolog','x ray']);
  put('infection infections',['infectio']);put('id',['infectious']);
  put('piles',['haemorrhoid']);put('boob boobs',['breast']);put('reconstructive',['plastic']);
  put('healthcare',['health care']);put('mount',['mt$']);put('mt',['mount']);put('saint',['st$']);
  put('professor',['prof']);put('aging',['ageing']);put('ageing',['aging']);put('sports',['sport']);put('victoria',['vic$']);
  return o;
})();
const WHOLE=mkset('ent gp gps gi ed er id icu mfm sti std mri ct men mens obs nsw act vic qld sa wa nt tas');   // short codes: exact word only
const STOP=mkset('and or the in at of for with near nearby nearest closest around me find show who is are need want looking i');
const STOP2=mkset('a an my to on best good top');                 // dropped only in front of a specialty word ("a cardiologist")
const SOFT=mkset('doctor doctors doc docs dr clinic clinics physician physicians consultant consultants medicine care health management');
const TITLE=mkset('dr doctor mr mrs ms miss'),TMOD=mkset('a assoc associate clin clinical adj adjunct conjoint'),TPROF=mkset('prof professor aprof');
const GENERIC=mkset('medical medicine general practice practitioner specialist health care');
const ALIAS={anesthesiologist:'anaesthetist',anaesthesiologist:'anaesthetist',gastrologist:'gastroenterologist',
  opthalmologist:'ophthalmologist',opthamologist:'ophthalmologist',optomologist:'ophthalmologist',physciatrist:'psychiatrist'};
const PHRASES=[[/\b(general practitioners?|general practice|family (doctors?|medicine|physicians?|practice|practitioners?)|primary care|local doctors?|general doctors?)\b/g,'gp'],
  [/\b(general|gen) (surgery|surgeons?|surg)\b/g,'gensurg'],
  [/\b(general|internal|gen) (medicine|med|physicians?)\b|\binternists?\b/g,'genmed'],
  [/\b(o g|o and g|ob gyn|obs and gynae|obstetrics (and )?gyna?ecology)\b/g,'obgyn'],
  [/\bear nose (and )?throat\b/g,'ent'],
  [/\b(eye|ophthalmic) (surgeons?|surgery)\b/g,'ophthalmologist'],
  [/\bx rays?\b/g,'xray'],[/\b(ct|cat) scans?\b/g,'ctscan'],[/\bintensive care unit\b/g,'icu'],
  [/\bweight loss\b/g,'weightloss'],[/\bnose job\b/g,'rhinoplasty'],[/\bboob job\b/g,'breast augmentation'],[/\btummy tuck\b/g,'abdominoplasty'],
  [/\bradiation therapy\b/g,'radiotherapy'],[/\b(womens|women|woman|womans) health\b/g,'womenshealth'],[/\b(mens|men) health\b/g,'menshealth'],
  [/\bmental health\b/g,'mental'],[/\bsports? medicine\b/g,'sports'],
  [/\bnew south wales\b/g,'nsw'],[/\baustralian capital territory\b/g,'act'],[/\bnorthern territory\b/g,'nt'],
  [/\bsouth australia\b/g,'sa'],[/\bwestern australia\b/g,'wa'],[/\bqueensland\b/g,'qld'],[/\btasmania\b/g,'tas']];
// combining forms: lets "thoracic" find Cardiothoracic and "surgeon" find Neurosurgeon without letting "urology" find Neurology
const COMBO=['otorhino','gastro','cardio','neuro','psycho','physio','electro','immuno','haemato','hemato','dermato','hepato','nephro','maxillo','cranio','musculo','thoraco','broncho','laryngo','rhino','angio','radio','micro','histo','cyto','lymph','colo','endo','oto','uro'];
function extras(text){
  const ws=text.split(' '),out=[];
  for(let i=0;i<ws.length;i++){const w=ws[i];if(w.length<9)continue;
    for(let c=0;c<COMBO.length;c++){const p=COMBO[c];
      if(w.length-p.length>=5&&w.lastIndexOf(p,0)===0){out.push(w.slice(p.length));break;}}}
  return out.join(' ');
}
const PART=mkset('van von de der den del della di da dos du la le el al st abdul abu ter ten mc mac o d');
function joins(nm){               // van der Merwe -> vandermerwe, De Silva -> desilva, Mc Donald -> mcdonald
  const ws=nm.split(' '),out=[];
  for(let i=0;i<ws.length-1;i++){if(!has(PART,ws[i]))continue;
    let j=i,s='';while(j<ws.length-1&&has(PART,ws[j])){s+=ws[j];j++;}
    s+=ws[j];if(s.length>=5)out.push(s);}
  return out;
}
function stateOf(r){              // the data groups NSW and ACT together; tell them apart by postcode, for searching only
  const st=norm(r.st);
  if(st==='nsw act'){const p=+String(r.pc==null?'':r.pc).replace(/\D/g,'');return ((p>=2600&&p<=2618)||(p>=2900&&p<=2920)||(p>=200&&p<=299))?'act':'nsw';}
  return st;
}
function indexRecord(r){
  const raw=String(r.n==null?'':r.n),nm=norm(raw),nj=norm(raw.replace(/['’`\-]/g,''));   // O'Brien -> obrien, Al-Khawaja -> alkhawaja
  let extra=nj!==nm?' '+nj:'';const js=joins(nm);if(js.length)extra+=' '+js.join(' ');
  const pr=norm(r.pr),prj=norm(String(r.pr==null?'':r.pr).replace(/['’`]/g,'')),prx=prj!==pr?' '+prj:'';
  const st=stateOf(r),pc=String(r.pc==null?'':r.pc).replace(/\D/g,'');
  r.__n=' '+nm+extra+' ';
  r.__pr=' '+pr+prx+' ';
  r.__h=' '+norm([r.n,r.pr,r.su,r.pc,r.s,r.sub,st,r.ph].join(' '))+extra+prx+' ';
  r.__c=' '+norm([r.n,r.s,r.su,r.pc,st].join(' '))+extra+' ';
  r.__x=' '+extras(norm([r.s,r.sub,r.pr].join(' ')))+' ';
  r.__s=String(r.s||'').toLowerCase();
  r.__sn=' '+norm(r.s)+' ';
  r.__sx=' '+extras(norm(r.s))+' ';
  let p=String(r.ph||'').replace(/\D/g,'');if(/^61\d{9}$/.test(p))p='0'+p.slice(2);
  r.__p=' '+p+(pc?' pc'+('0000'+pc).slice(-4):'')+(/^general pract/i.test(String(r.s||''))?'':' nongp')+' ';   // phone digits + exact-match tags
}
function hit(set,h,x,p){
  for(let i=0;i<set.length;i++){const n=set[i];
    if(n.d){if(p&&p.indexOf(n.s)>=0)return true;continue;}
    if(h.indexOf(n.k)>=0)return true;
    if(n.x&&x.indexOf(n.k)>=0)return true;}
  return false;
}
function matchRec(c,r){           // c = compile() result
  let miss=false;
  for(let i=0;i<c.sets.length;i++)if(!hit(c.sets[i],r.__h,r.__x,r.__p)){if(!c.soft[i])return false;miss=true;}
  if(!miss)return true;
  // a filler word ("doctor", "clinic", "medicine") is missing: fine as long as the real words describe the specialty itself
  for(let i=0;i<c.sets.length;i++)if(!c.soft[i]&&!hit(c.sets[i],r.__sn,r.__sx,''))return false;
  return true;
}
let SPECV=null,SPECW=null,FULLV=null,NAMEW=null,PCS=null;
const SUG=new Map(),PAIR=new Map();
function nameWords(){
  if(!NAMEW){NAMEW=new Set();for(let i=0;i<DATA.length;i++){const ws=DATA[i].__n.split(' ');for(let k=0;k<ws.length;k++)if(ws[k])NAMEW.add(ws[k]);}}
  return NAMEW;
}
function postcodes(){
  if(!PCS){PCS=new Set();for(let i=0;i<DATA.length;i++){const m=/ pc(\d{4}) /.exec(DATA[i].__p);if(m)PCS.add(m[1]);}}
  return PCS;
}
function expand(tok){
  const out=[],seen=Object.create(null);
  const add=(s,x,g,whole)=>{s=norm(s);if(!s||seen[s])return;seen[s]=1;out.push({s:s,k:' '+s+(whole?' ':''),x:!!x&&!whole,g:!!g});};
  if(/^\d{4}$/.test(tok)&&postcodes().has(tok))return [{s:' pc'+tok+' ',d:1}];              // a real postcode means that postcode
  if(/^\d{6,}$/.test(tok)){let d=tok;if(/^610\d{9}$/.test(d))d=d.slice(2);else if(/^61\d{9}$/.test(d))d='0'+d.slice(2);out.push({s:d,d:1});}
  if(tok==='specialist'||tok==='specialists')out.push({s:' nongp ',d:1});                        // "specialist" = anyone who is not a GP
  const whole=has(WHOLE,tok),combo=COMBO.indexOf(tok)>=0,medOnly=nameWords().has(tok);
  const forms=[tok];
  for(let i=0;i<VARIANTS.length;i++){const au=VARIANTS[i][0],us=VARIANTS[i][1];
    if(tok.indexOf(au)>=0)forms.push(tok.replace(au,us));
    else if(tok.indexOf(us)>=0)forms.push(tok.replace(us,au));
    else if(tok.length>=5&&us.lastIndexOf(tok,0)===0&&au.lastIndexOf(tok,0)!==0)forms.push(au);}
  const mm=/^(mc|mac)([a-z]{3,})$/.exec(tok);if(mm)forms.push((mm[1]==='mc'?'mac':'mc')+mm[2]);
  for(let i=0;i<forms.length;i++){const f=forms[i],lng=f.length>=5&&!combo;
    add(f,lng,false,whole&&i===0);
    const st=stem(f,medOnly);if(st)add(st,lng,STEMGEN);}
  if(tok.length>=4&&/^(opht|opth|ofth?a|opt[ao]l?m[aeiou]?l)/.test(tok))add('ophthalm',true);   // every way people start to spell ophthalmology
  if(has(SYN,tok)){const syn=SYN[tok];
    for(let i=0;i<syn.length;i++){const s=syn[i];if(s.slice(-1)==='$')add(s.slice(0,-1),false,false,true);else add(s,true);}}
  return out;
}
function osa(a,b,max){            // edit distance (insert / delete / substitute / swap neighbours), cut off above max
  const la=a.length,lb=b.length;if(Math.abs(la-lb)>max)return max+1;
  let p2=null,p1=new Array(lb+1),cur;
  for(let j=0;j<=lb;j++)p1[j]=j;
  for(let i=1;i<=la;i++){cur=new Array(lb+1);cur[0]=i;let best=i;const ca=a.charCodeAt(i-1);
    for(let j=1;j<=lb;j++){const cb=b.charCodeAt(j-1);
      let v=Math.min(p1[j]+1,cur[j-1]+1,p1[j-1]+(ca===cb?0:1));
      if(i>1&&j>1&&ca===b.charCodeAt(j-2)&&a.charCodeAt(i-2)===cb)v=Math.min(v,p2[j-2]+1);
      cur[j]=v;if(v<best)best=v;}
    if(best>max)return max+1;
    p2=p1;p1=cur;}
  return p1[lb];
}
function specVocab(){             // words of the specialty names, plus their US spellings and well-known wrong forms, as correction targets
  if(!SPECV){const m=new Map();
    META.specialties.forEach(x=>{norm(x[0]).split(' ').forEach(w=>{if(w.length>=4&&!/\d/.test(w))m.set(w,(m.get(w)||0)+(+x[1]||0));});});
    SPECV=[];m.forEach((c,w)=>SPECV.push({w:w,st:stem(w)||w,c:c,to:w,al:0}));
    const n=SPECV.length;
    for(let i=0;i<n;i++){const e=SPECV[i];
      for(let v=0;v<VARIANTS.length;v++){const au=VARIANTS[v][0];
        if(e.w.indexOf(au)>=0){const u=e.w.replace(au,VARIANTS[v][1]);if(!m.has(u))SPECV.push({w:u,st:stem(u)||u,c:e.c,to:e.w,al:1});break;}}}
    Object.keys(ALIAS).forEach(k=>{const to=ALIAS[k];if(m.has(to)&&!m.has(k))SPECV.push({w:k,st:stem(k)||k,c:m.get(to),to:to,al:2});});
    SPECW=new Set();SPECV.forEach(e=>{e.cs=stem(e.to)||e.to;SPECW.add(e.w);SPECW.add(e.st);});}
  return SPECV;
}
function isSpecWord(t){specVocab();return has(SYN,t)||SPECW.has(t)||SPECW.has(stem(t)||t);}
function fullVocab(){
  if(!FULLV){const m=new Map();
    for(let i=0;i<DATA.length;i++){const ws=DATA[i].__h.split(' ');for(let k=0;k<ws.length;k++){const w=ws[k];if(w.length>=4&&!/\d/.test(w))m.set(w,(m.get(w)||0)+1);}}
    FULLV=Array.from(m.entries());}
  return FULLV;
}
function specFuzzy(t,strict){     // nearest specialty word. strict = the word as typed already finds something, so be much more careful
  const V=specVocab(),L=t.length,ts=stem(t)||t,max=strict?(L>=10?2:1):(L>=15?3:L>=6?2:1),cands=[];
  for(let i=0;i<V.length;i++){const e=V[i];
    if(e.w===t||e.st===ts){if(e.al===2)return {d:1,word:e.to,show:e.to,count:e.c,pre:false};return null;}
    if(L>=5&&e.w.length>L&&e.w.lastIndexOf(t,0)===0){if(e.al===2){cands.push({d:0,e:e,pre:''});continue;}return null;}   // a real word still being typed
    if(strict&&has(GENERIC,e.to))continue;
    let d=osa(t,e.w,max);
    if(d>max&&e.st!==e.w&&ts.length>=6&&e.st.length>=6){const ms=Math.min(max,ts.length>=8?2:1),ds=osa(ts,e.st,ms);if(ds<=ms)d=ds;}
    const same=t.charCodeAt(0)===e.w.charCodeAt(0);
    let ok=d<=max;
    if(ok&&d>=2&&!same)ok=false;
    if(ok&&!strict&&d===2&&L<8&&(e.w.length<6||(Math.abs(e.w.length-L)>1&&e.w.length<8)))ok=false;
    if(ok&&strict&&L<8&&(L<6||!same||e.w.length<6))ok=false;
    if(ok){cands.push({d:d,e:e,pre:''});continue;}
    if(L>=5&&e.w.length>L+1){        // a typo in the first letters of a longer word that is still being typed
      if(strict){if(L>=6)for(let P=L;P<=L+1;P++){const pre=e.w.slice(0,P);if(pre.slice(-2)===t.slice(-2)&&osa(t,pre,1)<=1){cands.push({d:1,e:e,pre:pre});break;}}}
      else for(let P=L-1;P<=L+1;P++){const pre=e.w.slice(0,P);if(osa(t,pre,1)<=1){cands.push({d:1,e:e,pre:pre});break;}}
    }
  }
  if(!cands.length)return null;
  cands.sort((x,y)=>x.d-y.d||(x.pre?1:0)-(y.pre?1:0)||y.e.c-x.e.c);
  const b=cands[0],wd=c=>c.e.al===2?c.e.to:(c.pre||c.e.w),res={d:b.d,word:wd(b),show:b.e.to,count:b.e.c,pre:!!b.pre};
  if(!b.pre)for(let i=1;i<cands.length;i++){const c=cands[i];if(c.d!==b.d||c.pre)break;if(c.e.cs!==b.e.cs){res.alt={word:wd(c),show:c.e.to};break;}}
  return res;
}
function fullFuzzy(t,hits){       // nearest word anywhere in the data (names, suburbs, practices). hits>0: the typed word exists but is rare
  const V=fullVocab(),NW=nameWords(),L=t.length,max=hits?1:(L>=6?2:1);let best=null;
  for(let i=0;i<V.length;i++){const w=V[i][0],c=V[i][1];
    if(Math.abs(w.length-L)>max)continue;
    const same=w.charCodeAt(0)===t.charCodeAt(0),m=(same||w.charCodeAt(1)===t.charCodeAt(1))?max:1;
    const d=osa(t,w,m);
    if(d<1||d>m)continue;
    if(c<(NW.has(w)?(L<=5?5:1):(L<=5?30:3)))continue;            // a rare surname is a fair guess; a rare ordinary word is not
    if(d===2&&(!same||c<3||(L<8&&c<30)||(w.charCodeAt(1)!==t.charCodeAt(1)&&c<150)))continue;   // two slips: must start the same and be a common word
    if(hits&&(L<7||c<Math.max(15*hits,30)))continue;
    if(!best||d<best.d||(d===best.d&&c>best.count))best={d:d,word:w,show:w,count:c,pre:false};}
  return best;
}
function countHits(set,cap){
  let c=0;for(let i=0;i<DATA.length;i++){const r=DATA[i];if(hit(set,r.__h,r.__x,r.__p)){if(++c>=cap)break;}}
  return c;
}
function suggest(t,set){
  if(SUG.has(t))return SUG.get(t);
  let res=null;
  if(t.length>=4&&!/\d/.test(t)&&/[aeiouy]/.test(t)&&!has(STOP,t)){
    const hits=countHits(set,60),lit=set.filter(n=>!n.g),hl=lit.length<set.length?countHits(lit,60):hits;
    if(hl===0){                                     // the word as typed matches nothing
      const c=specFuzzy(t,false);if(c&&c.count>=3)res=c;
      if(!res){const f=fullFuzzy(t,0);if(f&&(hits===0||f.d===1)&&!set.some(n=>n.s===f.word))res=f;}
      if(res&&hits>0)res.dropStem=true;
    }else if(hits<40&&!nameWords().has(t)){         // a rare real word, or a typo that also exists in the data
      const c=specFuzzy(t,true);if(c&&c.count>=15*hits)res=c;
      if(!res&&hits<=8)res=fullFuzzy(t,hits);
    }
  }
  SUG.set(t,res);return res;
}
function anyAll(c){for(let i=0;i<DATA.length;i++)if(matchRec(c,DATA[i]))return true;return false;}
function addFix(t,set,fx,fixes){
  let to=fx.show;const more=expand(fx.word);
  if(fx.alt){to+=' and '+fx.alt.show;const m2=expand(fx.alt.word);for(let k=0;k<m2.length;k++)more.push(m2[k]);}
  if(fx.dropStem)for(let k=set.length-1;k>=0;k--)if(set[k].g)set.splice(k,1);
  for(let k=0;k<more.length;k++)set.push(more[k]);
  if(!fixes.some(f=>f.from===t))fixes.push({from:t,to:to});
}
function prep(q){                 // raw text -> {words, phones}
  let s=String(q==null?'':q);if(s.normalize)s=s.normalize('NFKC');
  const phones=[];
  s=s.replace(/\+?\d[\d\s().\-]{5,}\d/g,m=>{
    let d=m.replace(/\D/g,'');if(d.length<8)return m;
    const groups=m.trim().split(/\s+/);
    if(groups.length>1&&groups.every(g=>/^\d{4}$/.test(g)&&postcodes().has(g)))return m;        // several postcodes, not a phone number
    d=d.replace(/^0011/,'');if(/^610\d{9}$/.test(d))d=d.slice(2);else if(/^61\d{9}$/.test(d))d='0'+d.slice(2);
    phones.push(d);return ' ';});
  s=s.replace(/\bo\s*(?:&|\+)\s*g\b/gi,' obgyn ').replace(/\b([a-z])\.(?=[a-z]\b)/gi,'$1')
     .replace(/([a-z])['’`]s\b/gi,'$1').replace(/([a-z])['’`]([a-z])/gi,'$1$2');
  let t=norm(s);
  for(let i=0;i<PHRASES.length;i++)t=t.replace(PHRASES[i][0],PHRASES[i][1]);
  let words=t?t.split(' ').filter(Boolean):[];
  words=words.filter((w,i)=>words.indexOf(w)===i).slice(0,12);
  return {words:words,phones:phones};
}
function compile(q,exact){       // -> {toks, sets:[needles per word], soft:[bool], fixes:[{from,to}]}
  const pp=prep(q);let toks=pp.words;
  // leading titles: "dr smith", "a/prof smith", "associate professor smith"
  let prof=false;
  while(toks.length>1){const t=toks[0];
    if(has(TITLE,t)){if(!toks.slice(1).some(x=>!has(STOP,x)&&!has(STOP2,x)))break;toks=toks.slice(1);continue;}   // "doctor near me" keeps its only real word
    if(has(TMOD,t)){let j=0;while(j<toks.length-1&&has(TMOD,toks[j]))j++;
      if(has(TPROF,toks[j])||has(TITLE,toks[j])){toks=toks.slice(j);continue;}
      break;}
    if(has(TPROF,t)&&!prof){prof=true;toks=toks.slice(1);continue;}
    break;}
  // filler words
  if(toks.some(t=>!has(STOP,t)))toks=toks.filter(t=>!has(STOP,t));
  toks=toks.filter((t,i)=>!(has(STOP2,t)&&i<toks.length-1&&isSpecWord(toks[i+1])&&!has(SOFT,toks[i+1])&&!has(GENERIC,toks[i+1])));
  if(prof)toks=['prof'].concat(toks.filter(t=>t!=='prof'));
  const anyHard=toks.some(t=>!has(SOFT,t));
  const sets=[],soft=[],fixes=[],fixed=[],joined=[];
  for(let i=0;i<toks.length;i++){sets.push(expand(toks[i]));soft.push(anyHard&&has(SOFT,toks[i]));}
  // two words that are really one ("mc donald", "my health", "sun doctors")
  for(let i=0;i<toks.length-1;i++){const a=toks[i],b=toks[i+1];
    if(!/^[a-z]+$/.test(a)||!/^[a-z]+$/.test(b)||a.length+b.length<5||(prof&&i===0))continue;
    const key=a+b;let js=PAIR.get(key);
    if(js===undefined){js=expand(key).filter(n=>!n.d);if(!countHits(js,1))js=null;PAIR.set(key,js);}
    if(js){for(let k=0;k<js.length;k++){sets[i].push(js[k]);sets[i+1].push(js[k]);}joined[i]=joined[i+1]=1;}}
  if(!exact)for(let i=0;i<toks.length;i++){if(soft[i]||joined[i]||(prof&&i===0))continue;
    const fx=suggest(toks[i],sets[i]);if(fx){addFix(toks[i],sets[i],fx,fixes);fixed[i]=1;}}
  for(let i=0;i<pp.phones.length;i++){toks.push(pp.phones[i]);sets.push([{s:pp.phones[i],d:1}]);soft.push(false);}
  const c={toks:toks,sets:sets,soft:soft,fixes:fixes};
  if(toks.length>1&&!anyAll(c)){
    if(prof){toks.shift();sets.shift();soft.shift();}                  // "professor smith" when the record just says Dr
    else if(!exact){               // every word exists, but never together: the rarest one is probably misspelt
      const order=[];
      for(let i=0;i<toks.length;i++){const t=toks[i];if(fixed[i]||soft[i]||t.length<4||/\d/.test(t))continue;const h=countHits(sets[i],40);if(h<40)order.push([h,i]);}
      order.sort((x,y)=>x[0]-y[0]);
      for(let o=0;o<order.length;o++){const i=order[o][1],t=toks[i],tries=[fullFuzzy(t,0),specFuzzy(t,false)];let done=false;
        for(let k=0;k<tries.length&&!done;k++){const fx=tries[k];if(!fx)continue;
          const keep=sets[i];sets[i]=keep.concat(expand(fx.word));
          const ok=anyAll(c);sets[i]=keep;
          if(ok){addFix(t,keep,fx,fixes);done=true;}}
        if(done)break;}
    }
  }
  return c;
}
// <<< SEARCH ENGINE
// ---- one-time search index: built at hydrate (see __hydrateFn), since names arrive with the deferred index ----
const SPECSET=new Set(META.specialties.map(x=>String(x[0]).toLowerCase()));   // META is in the overview -> available now
const HAS_RATED=OV.some(r=>r.rv&&r.rv.score!=null);                           // overview carries score

// ---- filter state ----
const F={q:'',qraw:'',toks:[],cq:null,sets:[],specSets:[],fixes:[],hln:[],exact:false,spec:'',specExact:false,states:new Set(),tiers:new Set(),types:new Set(),sources:new Set(),
         emailOnly:false,availOnly:false,ratedOnly:false,loc:null,sort:'rating',inview:false,inviewAuto:false};
// "In map view" switched on automatically by near-me steps aside while a search is typed, so a search always looks everywhere
function inviewOn(){return F.inview&&!(F.inviewAuto&&(F.toks.length>0||!!F.spec));}
// a record's filter category: GP / Specialist (consulting doctors) / Imaging (imaging & pathology) / allied:<profession>
const PROF_LABEL={physio:'Physios',psychology:'Psychologists',ot:'OTs',podiatry:'Podiatrists',dietetics:'Dietitians',speech:'Speech pathologists',chiro:'Chiropractors',osteo:'Osteopaths',exercise_physiology:'Exercise physiologists',audiology:'Audiologists',optometry:'Optometrists',dentistry:'Dentists'};
const PROF_ORDER=['physio','psychology','ot','podiatry','dietetics','speech','chiro','osteo','exercise_physiology','audiology','optometry','dentistry'];
function catOf(r){const k=r.k||(r.s==='General Practitioner'?'gp':'spec');return k==='gp'?'GP':k==='spec'?'Specialist':k==='diag'?'Imaging':k==='pharmacy'?'Pharmacy':k==='hospital'?'Hospital':k==='physio'?'Physio':k==='psych'?'Psych':k==='dental'?'Dental':'allied:'+(r.pg||'other');}
function match(r){
  if(F.loc&&!F.loc.has(r.__i))return false;
  if(!(F.toks.length||F.spec)&&!F.types.has(catOf(r)))return false;   // category gate applies while browsing; a search looks across every category
  if(F.tiers.size&&!F.tiers.has(r.t))return false;
  if(F.states.size&&!F.states.has(r.st))return false;

  if(F.availOnly&&!r.av)return false;
  if(F.ratedOnly&&!(r.rv&&r.rv.score!=null))return false;
  if(F.spec){if(F.specExact){if(r.__s!==F.spec)return false;}else for(let k=0;k<F.specSets.length;k++)if(!hit(F.specSets[k],r.__sn,r.__sx,r.__p))return false;}
  if(F.cq&&F.cq.sets.length&&!matchRec(F.cq,r))return false;
  return true;
}
function availKey(r){return r.av?(r.av.days_away==null?50:r.av.days_away):99;}
function tierKey(r){const v=TB[r.t];return v==null?4:v;}
// ---- review score: ranking + colour (replaces the old tier / per-record Google-rating basis) ----
const RV_PRIOR=3.5;                          // shrinkage mean: few reviews pull toward this
function rvKey(r){                           // weighted score for "Top rated"; unrated -> -1 (sort last)
  const v=r.rv;if(!v||v.score==null)return -1;
  const c=Math.max(0,Math.min(1,+v.conf||0));
  return c*(+v.score)+(1-c)*RV_PRIOR;
}
function rvcol(r){                           // colour by score; neutral grey when unrated
  const v=r.rv&&r.rv.score!=null?+r.rv.score:null;
  if(v==null)return '#9aa7b4';
  if(v>=4.5)return '#15a34a';
  if(v>=4.0)return '#5aa23c';
  if(v>=3.0)return '#f0a92b';
  return '#e0564f';
}
// ---- type avatar: the same colour + glyph as the map pin, reused in the list and the popup (presentation only) ----
function pkOf(r){const k=r.k||(r.s==='General Practitioner'?'gp':'spec');return k==='spec'?'sp':(k==='gp'||k==='diag'||k==='pharmacy'||k==='hospital'||k==='physio'||k==='psych'||k==='dental')?k:'allied';}
const __ico={};
function icoURL(k){if(k in __ico)return __ico[k];try{const c=document.createElement('canvas');c.width=c.height=56;const x=c.getContext('2d');x.scale(2,2);glyph(x,14,k);return __ico[k]=c.toDataURL();}catch(e){return __ico[k]='';}}
function avHTML(r,cls){const k=pkOf(r),u=icoURL(k);return `<span class="av${cls?' '+cls:''}" style="--c:${PINCOL[k]}" aria-hidden="true">${u?`<img src="${u}" alt="">`:''}</span>`;}
const ICO={
  phone:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2z"/></svg>',
  dir:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 11l18-8-8 18-2-8-8-2z"/></svg>',
  web:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.8 3 2.8 15 0 18M12 3c-2.8 3-2.8 15 0 18"/></svg>'
};
function relevance(r){          // 0 whole word(s) of the name · 1 start of a name · 2 practice name · 3 specialty / suburb / postcode / state · 4 mentioned elsewhere
  const S=F.sets,soft=F.cq?F.cq.soft:[];let name=true,whole=true,core=true,prac=S.length>=2;
  for(let k=0;k<S.length;k++){if(soft[k])continue;const s=S[k];
    if(name){
      if(!hit(s,r.__n,' ',''))name=false;
      else if(whole){let w=false;for(let i=0;i<s.length;i++){const n=s[i];if(!n.d&&r.__n.indexOf(n.k.charAt(n.k.length-1)===' '?n.k:n.k+' ')>=0){w=true;break;}}if(!w)whole=false;}}
    if(prac&&!hit(s,r.__pr,' ',''))prac=false;
    if(core&&!hit(s,r.__c,r.__sx,r.__p))core=false;
    if(!name&&!prac&&!core)return 4;}
  return name?(whole?0:(F.specQ?3.5:1)):prac?2:core?3:4;
}
function sortRows(a){
  const s=F.sort,q=F.sets.length>0;
  if(q)for(let k=0;k<a.length;k++)a[k].__m=relevance(a[k]);
  a.sort((x,y)=>{
    if(q&&x.__m!==y.__m)return x.__m-y.__m;               // best kind of match first while searching
    if(s==='name')return x.__nr-y.__nr;
    if(s==='near')return ((x.__d||0)-(y.__d||0))||(tierKey(x)-tierKey(y))||(x.__nr-y.__nr);
    if(s==='avail')return (availKey(x)-availKey(y))||(tierKey(x)-tierKey(y))||(x.__nr-y.__nr);
    if(s==='rating')return (rvKey(y)-rvKey(x))||(x.__nr-y.__nr);
    return (tierKey(x)-tierKey(y))||(availKey(x)-availKey(y))||(x.__nr-y.__nr);
  });
  return a;
}

// ---- highlighting ----
function hl(text){
  text=text==null?'':String(text);
  const N=F.hln;if(!N.length||!text)return esc(text);
  const low=text.toLowerCase();if(low.length!==text.length)return esc(text);
  const rs=[];
  for(let k=0;k<N.length;k++){const s=N[k].s;let p=0;
    while((p=low.indexOf(s,p))>=0){const c=p?low.charCodeAt(p-1):32,wordStart=!((c>=97&&c<=122)||(c>=48&&c<=57));
      if(wordStart||(N[k].x&&s.length>=5))rs.push([p,p+s.length]);p+=s.length;}}
  if(!rs.length)return esc(text);
  rs.sort((a,b)=>a[0]-b[0]||b[1]-a[1]);
  let out='',pos=0;
  for(let k=0;k<rs.length;k++){const s=Math.max(rs[k][0],pos),e=rs[k][1];if(e<=pos)continue;
    out+=esc(text.slice(pos,s))+'<mark>'+esc(text.slice(s,e))+'</mark>';pos=e;}
  return out+esc(text.slice(pos));
}

// ---- review-score renderer (generic: any record with rv; "No reviews yet" for gp/spec/diag; nothing for allied/pharmacy/hospital) ----
const RV_METHOD=`<b>How the review score works.</b> The score is a 0–5 figure combining the ratings we hold for this practitioner from the sources shown. Each source is weighted by its number of reviews and lightly adjusted toward the overall average (so a few reviews can't outweigh hundreds). <b>Google ratings are the practitioner's clinic rating</b> — Google rates practices, not individual doctors. “No reviews yet” means we hold no ratings. We show ratings and counts only, and link to each source; we don't republish review text. Sources: Google (clinic), Healthshare (practitioner); more added over time.`;
function rvShows(r){const k=r.k||(r.s==='General Practitioner'?'gp':'spec');return k==='gp'||k==='spec'||k==='diag';}
function rvGmaps(r){
  const addr=[r.pr,r.addr,r.su,r.pc,(r.st&&r.st!=='?')?r.st:'','Australia'].filter(Boolean).join(' ');
  return 'https://www.google.com/maps/search/?api=1&query='+encodeURIComponent((r.n+' '+addr).replace(/\s+/g,' ').trim());
}
function rvBadge(r){
  if(r.rv&&r.rv.score!=null)return `<span class="b bstar" title="Review score (0–5)">★ ${esc(r.rv.score)}${r.rv.n?` <i>(${num(r.rv.n)})</i>`:''}</span>`;
  return rvShows(r)?`<span class="b bnr" title="We hold no ratings for this practitioner yet">No reviews yet</span>`:'';
}
function rvSrcRows(r){
  const s=(r.rv&&r.rv.src)||{},out=[];
  if(s.google)out.push(`<div class="rvrow">Clinic rating on Google — ${esc(s.google.r)} (${num(s.google.n)}) · <a href="${esc(rvGmaps(r))}" target="_blank" rel="noopener">View on Google ↗</a></div>`);
  if(s.healthshare)out.push(`<div class="rvrow">Healthshare (this doctor) — ${esc(s.healthshare.r)} (${num(s.healthshare.n)})${r.u?` · <a href="${safeUrl(r.u)}" target="_blank" rel="noopener">View on Healthshare ↗</a>`:''}</div>`);
  return out.join('');
}
function rvBlock(r){
  const badge=rvBadge(r);
  if(!badge)return '';
  if(!(r.rv&&r.rv.score!=null))return `<div class="rv">${badge}</div>`;
  const rows=rvSrcRows(r);
  return `<div class="rv">${badge}`+
    `<details class="rvhow"><summary>How this score is calculated</summary><div class="rvmeth">${RV_METHOD}</div></details>`+
    (rows?`<details class="rvsrc"><summary>Rating breakdown</summary>${rows}</details>`:'')+
    `</div>`;
}
// ---- popups ----
function popup(r,back){
  const tt=r.t&&r.t!=='?'?`<span class="tag t${esc(r.t)}" style="background:${tcol(r.t)}">Tier ${esc(r.t)}</span>`:'';
  const av=r.av||null,g=r.g||null;
  return `<div class="pop">`+
    (back?`<a class="gback" role="button" tabindex="0">‹ Back to ${num(back)} at this location</a>`:'')+
    `<div class="phead">${avHTML(r,'lg')}<div class="pht"><div class="pn">${esc(r.n)}</div><div class="ps">${esc(r.s)}${r.sub?' · '+esc(r.sub):''}</div></div></div>`+
    `<div class="mo">${r.pr?'<b>'+esc(r.pr)+'</b><br>':''}${r.addr?esc(r.addr):esc(r.su)+' '+esc(r.pc)+' · '+esc(r.st)}</div>`+
    (r.loc==='suburb'?`<div class="approx" title="This practice could not be located to street level, so the pin is placed in the suburb">Approximate position · the pin is in ${esc(r.su)||'the suburb'}, not at the door</div>`:'')+
    ((r.ph||r.w||(r.loc!=='suburb'&&r.lat!=null&&r.lng!=null))?`<div class="acts">`+
      (r.ph?`<a class="act" href="tel:${esc(String(r.ph).replace(/[^0-9+]/g,''))}">${ICO.phone}${esc(r.ph)}</a>`:'')+
      ((r.loc!=='suburb'&&r.lat!=null&&r.lng!=null)?`<a class="act" href="https://www.google.com/maps/dir/?api=1&destination=${encodeURIComponent(r.lat+','+r.lng)}" target="_blank" rel="noopener">${ICO.dir}Directions</a>`:'')+
      (r.w?`<a class="act" href="${safeUrl(r.w)}" target="_blank" rel="noopener">${ICO.web}Website</a>`:'')+
      `</div>`:'')+

    (av?`<div class="avail"><b>HotDoc · ${esc(av.label||'bookable')}</b>`+
      (av.clinic?`<div>at ${esc(String(av.clinic).trim())}</div>`:'')+
      ((av.slots&&av.slots.length)?`<div class="slots">${av.slots.slice(0,4).map(s=>`<span class="slot">${esc(s)}</span>`).join('')}</div>`:'')+
      (av.href?`<a href="${safeUrl(av.href)}" target="_blank" rel="noopener">Book on HotDoc →</a>`:'')+
      (av.maps&&!g?`${av.href?' · ':''}<a href="${safeUrl(av.maps)}" target="_blank" rel="noopener">Google Maps ↗</a>`:'')+
      `</div>`:'')+
    rvBlock(r)+
    ((r.src||r.u)?`<div class="links">Source: `+
      (r.u?`<a href="${safeUrl(r.u)}" target="_blank" rel="noopener">${esc(r.src||'profile')} ↗</a>`:esc(r.src))+
      `</div>`:'')+
    `</div>`;
}
const GROUP_CAP=200;
function groupHTML(g){
  const rs=g.rows,shown=rs.slice(0,GROUP_CAP);
  return `<div class="pop grp"><div class="gh">${num(rs.length)} practitioners at this location</div>`+
    `<div class="gsub">${esc(g.label)}</div><div class="gl">`+
    shown.map(r=>`<a class="gi" role="button" tabindex="0" data-i="${r.__i}">${avHTML(r,'sm')}<span class="gn">${esc(r.n)}</span><span class="gs">${esc(r.s)}</span></a>`).join('')+
    `</div><a class="gall" role="button" tabindex="0">${rs.length>shown.length?`Showing ${num(shown.length)} of ${num(rs.length)} — list`:'List'} all ${num(rs.length)} in the sidebar →</a></div>`;
}

// ---- list ----
const listEl=$('list'),countEl=$('count');
const PAGE=60,EMPTY={type:'FeatureCollection',features:[]};
let matched=[],view=[],rendered=0,selI=-1,hovI=-1,kb=-1,mapReady=false,curFC=null,lastGroup=null,map=null,pop=null,curCats=new Set();
let origin=null,userMoved=false,geoCtl=null,toastT=null,gpsOK=!!(navigator.geolocation&&window.isSecureContext);
if(gpsOK&&navigator.permissions&&navigator.permissions.query){try{navigator.permissions.query({name:'geolocation'}).then(s=>{if(s&&s.state==='denied')gpsOK=false;}).catch(()=>{});}catch(e){}}      // origin = where "near me" is measured from
// ---- motion helpers (presentation only): count tween, loading skeletons, reduced-motion guard ----
const RM=!!(window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches);
const EASE=t=>1-Math.pow(1-t,3);             // ease-out cubic: camera moves start fast and settle softly
const SKROWS='<div class="skl" role="status" aria-label="Loading the directory">'+'<div class="skr"><i></i><b></b><span></span></div>'.repeat(6)+'</div>';
const SKPOP='<div class="pop skp" role="status" aria-label="Loading"><div class="skh"><i></i><div><b></b><span></span></div></div><span></span><span class="s2"></span></div>';
let __cv=null,__craf=0;
function tweenCount(){                       // the headline number rolls to its new value instead of snapping
  const b=countEl.querySelector('b');if(!b)return;const to=+String(b.textContent).replace(/[^0-9]/g,'');if(!isFinite(to))return;
  const from=__cv==null?to:__cv;__cv=to;cancelAnimationFrame(__craf);if(from===to||RM)return;
  const t0=performance.now(),D=240;b.textContent=num(from);
  const step=t=>{const p=Math.min(1,(t-t0)/D),e=1-Math.pow(1-p,3);b.textContent=num(Math.round(from+(to-from)*e));if(p<1)__craf=requestAnimationFrame(step);};
  __craf=requestAnimationFrame(step);
}
function fmtKm(d){return d<0.975?Math.max(50,Math.round(d*20)*50)+' m':d<10?d.toFixed(1)+' km':Math.round(d).toLocaleString()+' km';}
function rowHTML(r){
  const t=r.t&&TCOL[r.t]&&r.t!=='?'?r.t:'';
  const av=r.av?`<span class="b bav">● ${esc(r.av.label||'Bookable')}${r.av.slots&&r.av.slots[0]?' · '+esc(r.av.slots[0]):''}</span>`:'';
  const rvb=rvBadge(r);
  const ml='';
  const dist=(origin&&F.sort==='near'&&r.__d!=null)?`<span class="b bdist">${origin.precise?'':'≈ '}${fmtKm(r.__d)}</span>`:'';
  const loc=[r.su,r.pc].filter(Boolean).join(' ');
  return `<div class="row${r.__i===selI?' sel':''}" role="option" aria-selected="${r.__i===selI}" data-i="${r.__i}">`+
    avHTML(r)+
    `<div class="r1"><span class="nm">${hl(r.n)}</span></div>`+
    `<div class="r2"><span class="sp">${hl(r.s)}</span>${r.sub?' · '+hl(r.sub):''}${r.pr?' · '+hl(r.pr):''}</div>`+
    `<div class="r3"><span class="loc">${hl(loc)}${r.st&&r.st!=='?'?' · '+esc(r.st):''}</span>${dist}${av}${rvb}${ml}</div></div>`;
}
function renderMore(){
  if(rendered>=view.length)return;
  const end=Math.min(view.length,rendered+PAGE);let h='';
  for(let k=rendered;k<end;k++)h+=rowHTML(view[k]);
  const note=listEl.querySelector('.endnote');if(note)note.remove();
  listEl.insertAdjacentHTML('beforeend',h);rendered=end;
  if(rendered>=view.length&&view.length>PAGE)listEl.insertAdjacentHTML('beforeend',`<div class="endnote">End of list · ${num(view.length)} shown</div>`);
}
function inBounds(){
  const b=map.getBounds(),s=b.getSouth(),n=b.getNorth();let w=b.getWest(),e=b.getEast();
  if(e-w>=360)return matched.filter(r=>r.lat>=s&&r.lat<=n);
  const k=Math.floor((w+180)/360)*360;w-=k;e-=k;          // normalise when the view sits on another world copy
  return matched.filter(r=>r.lat>=s&&r.lat<=n&&((r.lng>=w&&r.lng<=e)||(r.lng+360>=w&&r.lng+360<=e)));
}
function refreshList(){
  if(!__hydrated){view=matched;rendered=0;countEl.title='';
    countEl.innerHTML=`<b>${num(matched.length)}</b><span class="cl"> practitioners</span>`;
    listEl.innerHTML=SKROWS;return;}
  view=(inviewOn()&&mapReady)?inBounds():matched;
  countEl.title='';
  if(inviewOn()&&mapReady){countEl.innerHTML=`<b>${num(view.length)}</b> in view`;countEl.title=`${num(view.length)} in the current map view, of ${num(matched.length)} matching`;}
  else if(matched.length<DATA.length)countEl.innerHTML=`<b>${num(matched.length)}</b><span class="cl"> of ${num(DATA.length)}</span>`;
  else countEl.innerHTML=`<b>${num(DATA.length)}</b><span class="cl"> practitioners</span>`;
  tweenCount();
  listEl.innerHTML='';rendered=0;kb=-1;listEl.scrollTop=0;
  if(!view.length){
    listEl.innerHTML=`<div class="empty"><b>No matches</b>${inviewOn()&&matched.length?'Nothing in this part of the map — zoom out or switch off “In map view”.':'Check the spelling, try fewer words, or clear the filters with ↺.'}</div>`;
    return;
  }
  renderMore();
}
listEl.addEventListener('scroll',()=>{if(rendered<view.length&&listEl.scrollTop+listEl.clientHeight>listEl.scrollHeight-500)renderMore();});
function markRow(){
  const p=listEl.querySelector('.row.sel');if(p){p.classList.remove('sel');p.setAttribute('aria-selected','false');}
  if(selI>=0){const el=listEl.querySelector('.row[data-i="'+selI+'"]');if(el){el.classList.add('sel');el.setAttribute('aria-selected','true');}}
}

// ---- map ----
// detailed vector basemap (streets, buildings, parks, house numbers) from OpenFreeMap; the plainer Carto map is the fallback
const STYLES_MAIN={light:'https://tiles.openfreemap.org/styles/positron',dark:'https://tiles.openfreemap.org/styles/fiord'};
const STYLES_ALT={light:'https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json',dark:'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json'};
let STYLES=STYLES_MAIN,FONT=['Noto Sans Bold'];
const PINCOL={gp:'#1d4ed8',sp:'#0891b2',diag:'#d97706',allied:'#db2777',pharmacy:'#059669',hospital:'#dc2626',physio:'#65a30d',psych:'#7c3aed',dental:'#4f46e5'};
function glyph(x,m,k){   // white type-glyph on the colour disc; secondary to colour, aids discernibility + colour-blind users
  x.save();x.fillStyle='#fff';x.strokeStyle='#fff';x.lineWidth=2.4;x.lineJoin='round';x.lineCap='round';
  if(k==='hospital'){x.fillRect(m-3.2,m-10,6.4,20);x.fillRect(m-10,m-3.2,20,6.4);}
  else if(k==='sp'){x.beginPath();for(let i=0;i<10;i++){const r=i%2?4.3:10,a=-Math.PI/2+i*Math.PI/5,px=m+r*Math.cos(a),py=m+r*Math.sin(a);i?x.lineTo(px,py):x.moveTo(px,py);}x.closePath();x.fill();}
  else if(k==='physio'){x.beginPath();x.moveTo(m-10,m);x.lineTo(m-4.5,m);x.lineTo(m-2,m-7.5);x.lineTo(m+1.5,m+7.5);x.lineTo(m+4,m);x.lineTo(m+10,m);x.stroke();}
  else if(k==='pharmacy'){x.save();x.translate(m,m);x.rotate(-Math.PI/4);if(x.roundRect){x.beginPath();x.roundRect(-9,-4.6,18,9.2,4.6);x.stroke();}else{x.strokeRect(-9,-4.6,18,9.2);}x.beginPath();x.moveTo(0,-4.6);x.lineTo(0,4.6);x.stroke();x.restore();}
  else if(k==='gp'){x.beginPath();x.moveTo(m-6,m-10);x.lineTo(m-6,m-3);x.quadraticCurveTo(m-6,m+4,m,m+4);x.quadraticCurveTo(m+6,m+4,m+6,m-3);x.lineTo(m+6,m-10);x.stroke();x.beginPath();x.arc(m-6,m-10,1.7,0,7);x.arc(m+6,m-10,1.7,0,7);x.fill();x.beginPath();x.moveTo(m,m+4);x.lineTo(m,m+7);x.stroke();x.beginPath();x.arc(m,m+9,2.7,0,7);x.stroke();}
  else if(k==='diag'){x.beginPath();x.moveTo(m-6.5,m-9);x.lineTo(m-1,m-6);x.stroke();x.beginPath();x.moveTo(m-3.5,m-7.5);x.quadraticCurveTo(m+4,m-4.5,m+1.5,m+2);x.stroke();x.beginPath();x.moveTo(m+1.5,m+2);x.lineTo(m,m+4.5);x.stroke();x.beginPath();x.moveTo(m-6.5,m+5);x.lineTo(m+4.5,m+5);x.stroke();x.beginPath();x.moveTo(m-7,m+9.5);x.lineTo(m+7,m+9.5);x.stroke();x.beginPath();x.moveTo(m+4.5,m+9.5);x.quadraticCurveTo(m+5.5,m+4,m+2.5,m+1);x.stroke();}
  else if(k==='psych'){x.beginPath();x.arc(m,m,8,0,7);x.stroke();x.beginPath();x.moveTo(m,m-8);x.quadraticCurveTo(m-4.5,m-4,m-1.5,m);x.quadraticCurveTo(m+2,m+4,m,m+8);x.stroke();x.beginPath();x.moveTo(m-7.2,m-2.5);x.quadraticCurveTo(m-3,m-1.5,m-4,m+3.2);x.stroke();}
  else if(k==='allied'){x.beginPath();x.moveTo(m,m-1.5);x.bezierCurveTo(m,m-4.5,m-5,m-6.5,m-6.6,m-2.8);x.bezierCurveTo(m-8,m+0.6,m-3.6,m+2.8,m,m+5.6);x.bezierCurveTo(m+3.6,m+2.8,m+8,m+0.6,m+6.6,m-2.8);x.bezierCurveTo(m+5,m-6.5,m,m-4.5,m,m-1.5);x.fill();x.beginPath();x.arc(m,m+5.5,8,0.17*Math.PI,0.83*Math.PI);x.stroke();}
  else if(k==='dental'){x.beginPath();x.moveTo(m-7,m-3);x.bezierCurveTo(m-7.5,m-10.5,m+7.5,m-10.5,m+7,m-3);x.bezierCurveTo(m+6.6,m+1,m+5,m+3,m+4,m+8);x.quadraticCurveTo(m+3.2,m+10.2,m+2.2,m+7);x.lineTo(m+1.2,m+2.2);x.quadraticCurveTo(m,m+1,m-1.2,m+2.2);x.lineTo(m-2.2,m+7);x.quadraticCurveTo(m-3.2,m+10.2,m-4,m+8);x.bezierCurveTo(m-5,m+3,m-6.6,m+1,m-7,m-3);x.closePath();x.fill();}
  x.restore();
}
function pinImage(col,kind,avail){        // a round badge drawn once per kind: white type-glyph on a colour disc; green outer ring = bookable online
  const S=64,c=document.createElement('canvas');c.width=c.height=S;const x=c.getContext('2d'),m=S/2;
  x.shadowColor='rgba(16,24,40,.35)';x.shadowBlur=6;x.shadowOffsetY=2;
  x.beginPath();x.arc(m,m,avail?25:21,0,7);x.fillStyle=avail?'#16a34a':'#ffffff';x.fill();
  x.shadowColor='transparent';
  if(avail){x.beginPath();x.arc(m,m,21,0,7);x.fillStyle='#ffffff';x.fill();}
  x.beginPath();x.arc(m,m,17.5,0,7);x.fillStyle=col;x.fill();
  glyph(x,m,kind);
  const d=x.getImageData(0,0,S,S);return {width:S,height:S,data:new Uint8Array(d.data.buffer)};
}
function addPinImages(){['gp','sp','diag','allied','pharmacy','hospital','physio','psych','dental'].forEach(k=>[0,1].forEach(a=>{const id='pin-'+k+(a?'-av':'');if(!map.hasImage(id))map.addImage(id,pinImage(PINCOL[k],k,a),{pixelRatio:2});}));}
function curTheme(){
  const t=document.documentElement.getAttribute('data-theme');
  if(t==='dark'||t==='light')return t;
  return (window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches)?'dark':'light';
}
function featuresFor(rows){
  const fs=new Array(rows.length),cats=new Set();
  for(let k=0;k<rows.length;k++){const r=rows[k];
    const kind=r.k||(r.s==='General Practitioner'?'gp':'spec');
    const pk=kind==='gp'?'gp':kind==='spec'?'sp':kind==='diag'?'diag':kind==='pharmacy'?'pharmacy':kind==='hospital'?'hospital':kind==='physio'?'physio':kind==='psych'?'psych':kind==='dental'?'dental':'allied';
    cats.add(kind==='gp'?'GP':kind==='spec'?'Specialist':kind==='diag'?'Imaging':kind==='pharmacy'?'Pharmacy':kind==='hospital'?'Hospital':kind==='physio'?'Physio':kind==='psych'?'Psych':kind==='dental'?'Dental':'allied');
    fs[k]={type:'Feature',geometry:{type:'Point',coordinates:[r.lng,r.lat]},properties:{i:r.__i,t:r.t,p:'pin-'+pk+(r.av?'-av':''),g:kind==='gp'?1:0,a:r.av?1:0,n:r.n,d:kind==='gp'?(r.pr||''):(r.s||'')}};}
  curCats=cats;
  return {type:'FeatureCollection',features:fs};
}
function ptFeature(r){return r?{type:'Feature',geometry:{type:'Point',coordinates:[r.lng,r.lat]},properties:{}}:EMPTY;}
function setPt(id,r){if(!map)return;try{const s=map.getSource(id);if(s)s.setData(ptFeature(r));}catch(e){}}
function fitTo(rows,animate){
  if(!map||!rows.length)return;
  let a=180,b=90,c=-180,d=-90;
  for(let k=0;k<rows.length;k++){const r=rows[k];if(r.lng<a)a=r.lng;if(r.lng>c)c=r.lng;if(r.lat<b)b=r.lat;if(r.lat>d)d=r.lat;}
  map.fitBounds([[a,b],[c,d]],{padding:56,maxZoom:14,duration:animate?480:0,easing:EASE});
}
function addLayers(){
  if(map.getSource('pts'))return;
  const dark=curTheme()==='dark',ring=dark?'#ffffff':'#121922';
  map.addSource('pts',{type:'geojson',data:curFC||EMPTY,cluster:true,clusterRadius:46,clusterMaxZoom:17,clusterProperties:{g:['+',['get','g']],a:['+',['get','a']]}});
  map.addSource('hov',{type:'geojson',data:ptFeature(hovI>=0?DATA[hovI]:null)});
  map.addSource('sel',{type:'geojson',data:ptFeature(selI>=0?DATA[selI]:(lastGroup?{lng:lastGroup.coord[0],lat:lastGroup.coord[1]}:null))});
  addPinImages();
  // groups: a small dot with a quiet number label beside it (the wide, nearly invisible circle is only the click target)
  const cr=['interpolate',['linear'],['get','point_count'],2,13,10,16,40,20,150,25,600,30,2000,34];
  map.addLayer({id:'clusters',type:'circle',source:'pts',filter:['has','point_count'],paint:{'circle-color':'#000000','circle-opacity':0.01,'circle-radius':['max',18,cr]}});   // click target now covers the whole bubble
  // dot colour = what the group mostly is (GPs blue, specialists purple, a mix in between); green edge = someone there is bookable online
  const share=['/',['get','g'],['get','point_count']],ink=dark?'#eaf0f5':'#1f2a37',halo=dark?'rgba(11,16,21,.92)':'rgba(255,255,255,.95)';
  const ccol=dark?['interpolate',['linear'],share,0,'#22d3ee',0.5,'#38bdf8',1,'#60a5fa']:['interpolate',['linear'],share,0,PINCOL.sp,0.5,'#0ea5e9',1,PINCOL.gp];
  const chov=['boolean',['feature-state','hover'],false];
  // soft glow under each bubble for depth (purely decorative)
  map.addLayer({id:'cluster-glow',type:'circle',source:'pts',filter:['has','point_count'],paint:{
    'circle-color':ccol,'circle-radius':['+',cr,['case',chov,11,7]],'circle-blur':0.9,'circle-opacity':['case',chov,dark?0.62:0.5,dark?0.4:0.28]}});
  map.addLayer({id:'cluster-dot',type:'circle',source:'pts',filter:['has','point_count'],paint:{
    'circle-opacity':['case',chov,0.84,0.58],   // translucent cluster bubble (sleeker; fill only — stroke + count label stay opaque + legible); fills in on hover. user 2026-11-26
    'circle-color':ccol,'circle-radius':['+',cr,['case',chov,2,0]],
    'circle-stroke-width':['case',['>',['get','a'],0],3,2.5],'circle-stroke-opacity':1,'circle-stroke-color':['case',['>',['get','a'],0],dark?'#4ade80':'#16a34a',dark?'#e6edf5':['interpolate',['linear'],share,0,PINCOL.sp,0.5,'#0ea5e9',1,PINCOL.gp]]}});   // light theme: solid rim in the bubble's own hue (a white rim vanished on the pale basemap)
  const cnt=['get','point_count_abbreviated'];
  map.addLayer({id:'cluster-count',type:'symbol',source:'pts',filter:['has','point_count'],
    layout:{'text-field':cnt,'text-font':FONT,
            'text-size':['interpolate',['linear'],['get','point_count'],2,11.5,40,13,600,15,2000,16],
            'text-anchor':'center','text-offset':[0,0],'text-allow-overlap':true,'text-ignore-placement':true},
    paint:{'text-color':ink,'text-halo-color':halo,'text-halo-width':2.2}});
  map.addLayer({id:'pt',type:'symbol',source:'pts',filter:['!',['has','point_count']],layout:{
    'icon-image':['get','p'],'icon-allow-overlap':true,'icon-ignore-placement':true,
    'icon-size':['interpolate',['linear'],['zoom'],4,0.5,10,0.72,14,0.9,17,1.05]}});
  // close in, single pins carry their name (and practice or specialty); labels that would collide are left out
  map.addLayer({id:'pt-label',type:'symbol',source:'pts',minzoom:13.5,filter:['!',['has','point_count']],
    layout:{'text-field':['step',['zoom'],['format',['get','n'],{}],15.5,['format',['get','n'],{},'\n',{},['get','d'],{'font-scale':0.86,'text-color':dark?'#a3b1bf':'#566473'}]],
            'text-font':FONT,'text-size':11.5,'text-anchor':'left','text-justify':'left','text-offset':[1.15,0],'text-max-width':13,'text-optional':true,'text-padding':4},
    paint:{'text-color':ink,'text-halo-color':halo,'text-halo-width':2}});
  map.addLayer({id:'hov',type:'circle',source:'hov',paint:{'circle-radius':16,'circle-color':'rgba(0,0,0,0)','circle-stroke-width':2,'circle-stroke-color':ring,'circle-stroke-opacity':0.55}});
  map.addLayer({id:'sel',type:'circle',source:'sel',paint:{'circle-radius':18,'circle-color':dark?'rgba(122,167,255,0.25)':'rgba(37,99,235,0.18)','circle-stroke-width':3,'circle-stroke-color':dark?'#7aa7ff':'#2563eb'}});
  // expanding ring that "pings" twice when something is selected (driven by ping(); invisible at rest)
  map.addLayer({id:'sel-ping',type:'circle',source:'sel',paint:{'circle-radius':18,'circle-color':'rgba(0,0,0,0)','circle-stroke-width':2.5,'circle-stroke-color':dark?'#7aa7ff':'#2563eb','circle-stroke-opacity':0,
    'circle-radius-transition':{duration:0},'circle-stroke-opacity-transition':{duration:0}}});
}
let __praf=0;
function ping(){
  if(RM||!map||!map.getLayer('sel-ping'))return;cancelAnimationFrame(__praf);
  const t0=performance.now(),D=900,N=2;
  const step=t=>{const p=(t-t0)/D;try{
      if(p>=N){map.setPaintProperty('sel-ping','circle-stroke-opacity',0);return;}
      const q=p%1,e=1-Math.pow(1-q,3);
      map.setPaintProperty('sel-ping','circle-radius',18+e*26);map.setPaintProperty('sel-ping','circle-stroke-opacity',0.6*(1-q));
    }catch(err){return;}
    __praf=requestAnimationFrame(step);};
  __praf=requestAnimationFrame(step);
}
function showPopup(lngLat,html){
  if(!pop)return;
  hideToast();                       // the location notice must never cover an open popup
  pop.setLngLat(lngLat).setHTML(html);
  if(!pop.isOpen())pop.addTo(map);
  if(map.isMoving())map.once('moveend',()=>setTimeout(uncover,30));else setTimeout(uncover,30);
}
function uncover(){                  // phones: the floating card sits over the top of the map, so slide an overlapped popup clear of it
  try{if(!isPhone()||!pop||!pop.isOpen()||map.isMoving())return;
    const top=pop.getElement().getBoundingClientRect().top,sb=($('topcard')||$('side')).getBoundingClientRect().bottom+6;
    if(top<sb)map.panBy([0,top-sb],{duration:220},{dnmNudge:true});}catch(e){}
}
function clearSel(){selI=-1;markRow();setPt('sel',null);if(!map){const m=$('mapmsg');if(m.dataset.msg&&m.querySelector('.pop'))m.textContent=m.dataset.msg;}}
function popRoom(){try{const el=pop&&pop.getElement();return el?el.offsetHeight+16:0;}catch(e){return 0;}}
function nudge(ll){   // if the popup fits neither above nor below the point, pan just enough for it to fit above
  try{const H=map.getContainer().clientHeight,h=popRoom(),y=map.project(ll).y;if(h&&h<H&&y<h&&y+h>H)map.panBy([0,y-h],{duration:250},{dnmNudge:true});}catch(e){}
}
const DET=new Map();                              // id -> fetched /r detail (immutable within a deploy)
function fetchDetail(i){
  if(DET.has(i))return Promise.resolve(DET.get(i));
  return fetch('/r/'+i,{credentials:'same-origin'}).then(r=>r.ok?r.json():null).then(d=>{
    const o=(__hydrated?DATA[i]:OV[i])||null;
    if(d){d.__i=i;if(o){d.lat=o.lat;d.lng=o.lng;if(!d.k)d.k=o.k;}DET.set(i,d);return d;}
    return o;                                     // fetch failed: fall back to the in-memory row (no breakdown/addr)
  }).catch(()=>(__hydrated?DATA[i]:OV[i])||null);
}
function select(i,o){
  o=o||{};const base=(__hydrated?DATA[i]:OV[i]);if(!base)return;
  if(!map){selI=i;markRow();const m=$('mapmsg');m.hidden=false;m.innerHTML='<div class="pop">Loading…</div>';
    fetchDetail(i).then(d=>{if(selI===i&&d)m.innerHTML=popup(d,0);});return;}
  selI=i;markRow();setPt('sel',base);ping();
  const back=(o.back&&lastGroup&&lastGroup.ids.has(i))?lastGroup.rows.length:0;
  if(!back&&!o.keepGroup)lastGroup=null;
  const ll=[base.lng,base.lat];
  showPopup(ll,SKPOP);                                              // instant skeleton frame; fill when the detail arrives
  fetchDetail(i).then(d=>{if(selI===i&&d&&pop&&pop.isOpen()){pop.setHTML(popup(d,back));setTimeout(uncover,30);nudge(ll);}});
  if(o.fly&&!(inviewOn()&&mapReady&&map.getBounds().contains(ll))){
    const H=map.getContainer().clientHeight,h=popRoom(),dy=(h>H/2&&h<H)?Math.min(h-H/2,H/2-12):0;
    map.flyTo({center:ll,offset:[0,dy],zoom:Math.max(map.getZoom(),inviewOn()?12:15),speed:2.6,curve:1.25,maxDuration:1300});
    if(!map.isMoving())nudge(ll);
  }else nudge(ll);
  if(o.scroll&&__hydrated){const k=view.indexOf(base);if(k>=rendered&&k<1500)while(rendered<=k)renderMore();
    const el=listEl.querySelector('.row[data-i="'+i+'"]');if(el)el.scrollIntoView({block:'nearest'});}
}
function showGroup(g){
  lastGroup=g;selI=-1;markRow();setPt('sel',{lng:g.coord[0],lat:g.coord[1]});ping();
  showPopup(g.coord,groupHTML(g));nudge(g.coord);
}
function wireMap(){
  let pinHit=false;
  map.on('click','pt',e=>{const f=e.features&&e.features[0];if(f){pinHit=true;select(+f.properties.i,{scroll:true});}});
  map.on('click','clusters',e=>{
    const f=e.features&&e.features[0];if(!f)return;userMoved=true;
    const src=map.getSource('pts'),id=f.properties.cluster_id,n=f.properties.point_count,coord=f.geometry.coordinates.slice();
    src.getClusterExpansionZoom(id).then(z=>{
      if(z<=17&&z<=map.getMaxZoom())return map.easeTo({center:coord,zoom:z,duration:340,easing:EASE});
      if(!__hydrated)return map.easeTo({center:coord,zoom:Math.min(17,map.getMaxZoom()),duration:340,easing:EASE});   // names not loaded yet: just zoom
      // cannot be split by zooming: these practitioners share one location (same clinic / postcode centre)
      return src.getClusterLeaves(id,n,0).then(leaves=>{
        const rows=sortRows(leaves.map(l=>DATA[+l.properties.i]).filter(Boolean));
        if(!rows.length)return;
        if(rows.length===1)return select(rows[0].__i,{scroll:true});
        const first=rows[0],label=[first.su,first.pc].filter(Boolean).join(' ')+(first.st&&first.st!=='?'?' · '+first.st:'');
        const same=rows.every(r=>r.lng===first.lng&&r.lat===first.lat);
        showGroup({rows:rows,ids:new Set(rows.map(r=>r.__i)),coord:same?[first.lng,first.lat]:coord,label:label});
      });
    }).catch(()=>{});
  });
  ['pt','clusters'].forEach(l=>{
    map.on('mouseenter',l,()=>{map.getCanvas().style.cursor='pointer';});
    map.on('mouseleave',l,()=>{map.getCanvas().style.cursor='';});
  });
  // hover feedback on bubbles: the one under the pointer fills in and lifts (feature-state only — no data involved)
  let hovC=null;const setHov=(id,on)=>{try{map.setFeatureState({source:'pts',id:id},{hover:on});}catch(e){}};
  map.on('mousemove','clusters',e=>{const f=e.features&&e.features[0],id=f&&f.id!=null?f.id:null;if(id===hovC)return;if(hovC!=null)setHov(hovC,false);hovC=id;if(id!=null)setHov(id,true);});
  map.on('mouseleave','clusters',()=>{if(hovC!=null){setHov(hovC,false);hovC=null;}});
  let mvT=null;
  map.on('moveend',e=>{if(inviewOn()&&!(e&&e.dnmNudge)){clearTimeout(mvT);mvT=setTimeout(refreshList,140);}
    if(e&&e.originalEvent&&!inviewOn()&&map.getZoom()>=8)$('areaBtn').hidden=false;if(inviewOn())$('areaBtn').hidden=true;});
  // registered last: a click on empty map (or a cluster) closes the popup; a click on a pin switches it in place
  map.on('click',()=>{const hit=pinHit;pinHit=false;if(!hit&&pop&&pop.isOpen())pop.remove();});
}
let mapTheme=curTheme();
function syncMapTheme(){
  const t=curTheme();if(!map||t===mapTheme)return;
  mapTheme=t;mapReady=false;
  map.once('style.load',()=>{addLayers();mapReady=true;if(inviewOn())refreshList();});
  map.setStyle(STYLES[t],{diff:false});
}
function hideToast(){clearTimeout(toastT);$('toast').hidden=true;}
function showToast(text,actLabel,fn){
  $('toastText').textContent=text;const b=$('toastAct');
  b.hidden=!actLabel;b.textContent=actLabel||'';b.onclick=fn?()=>{hideToast();fn();}:null;
  $('toast').hidden=false;clearTimeout(toastT);toastT=setTimeout(hideToast,12000);
}
function SRC(){return __hydrated?DATA:OV;}
function setOrigin(o){
  origin=o;const R=6371,rad=Math.PI/180,cl=Math.cos(o.lat*rad);
  const _s=SRC();for(let i=0;i<_s.length;i++){const r=_s[i],dl=(r.lat-o.lat)*rad,dg=(r.lng-o.lng)*rad;
    const a=Math.pow(Math.sin(dl/2),2)+cl*Math.cos(r.lat*rad)*Math.pow(Math.sin(dg/2),2);
    r.__d=2*R*Math.asin(Math.min(1,Math.sqrt(a)));}
  const sel=$('sortby');
  if(!sel.querySelector('option[value="near"]')){const op=document.createElement('option');op.value='near';op.textContent='Nearest';sel.insertBefore(op,sel.firstChild);}
  if(o.precise){F.sort='near';sel.value='near';}
  if(o.precise||F.sort==='near')apply();
}
// approximate start position: the server estimates the visitor's city from the request IP (see /geo)
const geoP=(/^https?:$/.test(location.protocol)&&window.fetch)
  ?fetch('/geo',{credentials:'same-origin'}).then(r=>r.ok?r.json():null).catch(()=>null):Promise.resolve(null);
function nearMe(g){
  if(!map||!g||typeof g.lat!=='number'||typeof g.lng!=='number')return;
  if(g.country&&g.country!=='AU')return;
  if(!g.city&&(g.accuracy_km||0)>=200)return;                     // only a country-level guess: not useful
  if(g.lat<-44.5||g.lat>-9||g.lng<112||g.lng>154.5)return;
  // never yank the map away from what the user is doing — but the DEFAULT category view (doctors on) is not a user
  // filter, so don't let it block the approximate-location open (it did once imaging/allied/etc. categories were added)
  if(userMoved||F.toks.length||F.spec||F.states.size||F.tiers.size||F.sources.size||F.emailOnly||F.availOnly||F.ratedOnly||F.loc||selI>=0||origin||(pop&&pop.isOpen()))return;
  setOrigin({lat:g.lat,lng:g.lng,precise:false,label:g.city||g.region||''});
  let n25=0,n150=0;const _s=SRC();for(let i=0;i<_s.length;i++){const d=_s[i].__d;if(d<=25)n25++;if(d<=150)n150++;}
  if(n150<5)return;
  const canGps=!!(geoCtl&&gpsOK);
  // the list follows the area shown, but only if that area actually holds results (registered before the move starts)
  map.once('moveend',()=>{if(!F.inview&&!userMoved&&inBounds().length){F.inview=true;F.inviewAuto=true;refreshList();updateChrome();}});
  map.flyTo({center:[g.lng,g.lat],zoom:n25>=20?10.6:8,speed:2,curve:1.3,maxDuration:1600});
  showToast('📍 Near '+(origin.label||'you')+' (approximate)',canGps?'Use my exact location':'',canGps?()=>geoCtl.trigger():null);
}
function mapFail(msg){$('loading').hidden=true;const m=$('mapmsg');m.hidden=false;m.textContent=msg;m.dataset.msg=msg;$('inviewBtn').hidden=true;}
if(typeof maplibregl==='undefined'){mapFail('The map library could not be loaded (check your connection). The list and search still work.');}
else{
  try{
    map=new maplibregl.Map({container:'map',style:STYLES[curTheme()],center:[134,-27],zoom:3.3,maxZoom:17.9,attributionControl:false,fadeDuration:120});
    const altT=setTimeout(useAlt,9000);map.once('load',()=>clearTimeout(altT));
    function useAlt(){if(mapReady||STYLES===STYLES_ALT)return;STYLES=STYLES_ALT;FONT=['Open Sans Bold','Noto Sans Regular'];try{map.setStyle(STYLES[curTheme()],{diff:false});}catch(e){}}
    map.on('error',e=>{const st=e&&e.error&&e.error.status;if(!mapReady&&(st>=400||/style/i.test(String(e&&e.error&&e.error.message))))useAlt();});
    map.addControl(new maplibregl.AttributionControl({compact:true}));
    map.addControl(new maplibregl.NavigationControl({showCompass:false}),'top-right');
    if(gpsOK){
      geoCtl=new maplibregl.GeolocateControl({positionOptions:{enableHighAccuracy:true,timeout:10000},fitBoundsOptions:{maxZoom:13},trackUserLocation:false});
      map.addControl(geoCtl,'top-right');
      geoCtl.on('geolocate',p=>{if(p&&p.coords){userMoved=true;hideToast();setOrigin({lat:p.coords.latitude,lng:p.coords.longitude,precise:true});}});
    }
    map.on('movestart',e=>{if(e&&e.originalEvent)userMoved=true;});
    pop=new maplibregl.Popup({maxWidth:'330px',offset:12,closeOnClick:false,focusAfterOpen:false});
    pop.on('close',()=>{lastGroup=null;clearSel();});
    map.on('load',()=>{$('loading').hidden=true;addLayers();wireMap();mapReady=true;if(inviewOn())refreshList();if(selI<0)fitTo(matched.length?matched:OV,false);geoP.then(nearMe);});
  }catch(err){map=null;mapFail('The map could not start in this browser (WebGL is required). The list and search still work.');}
}

// ---- apply filters ----
function isFiltered(){return !!(F.toks.length||F.spec||F.states.size||F.tiers.size||F.types.size||F.sources.size||F.emailOnly||F.availOnly||F.ratedOnly||F.loc);}
function updateChrome(){
  const mob=true;                                    // every filter lives behind the filter button
  // the default doctor view (GPs + Specialists on) is not "a filter"; only count changes away from it
  const typeExtra=[...F.types].filter(x=>x!=='GP'&&x!=='Specialist').length+DTYPES.filter(x=>!F.types.has(x)).length;
  const n=(F.spec?1:0)+F.states.size+F.sources.size+(mob?F.tiers.size+typeExtra+(F.emailOnly?1:0)+(F.availOnly?1:0)+(F.ratedOnly?1:0):0),mn=$('moreN');
  mn.hidden=!n;mn.textContent=n||'';
  const hasQ=$('q').value.length>0;$('qclear').hidden=!hasQ;$('qhint').hidden=hasQ;
  $('specclear').hidden=!$('spec').value;
  setOn($('inviewBtn'),inviewOn());
  $('reset').style.opacity=(isFiltered()||F.inview||F.sort!=='rating')?'1':'.45';
  const lp=$('locpill');lp.hidden=!F.loc;
  if(F.loc)$('loctext').innerHTML=`📍 Listing <b>${num(matched.length)}</b> at the selected location`;
}
function recompile(){
  const c=compile(F.qraw,F.exact);F.cq=c;F.sets=c.sets;F.fixes=c.fixes;F.specQ=c.toks.some(isSpecWord);
  F.hln=[];c.sets.forEach(s=>s.forEach(n=>{if(!n.d)F.hln.push(n);}));
  if(F.spec&&!F.specExact){const sc=compile(F.spec,false);F.specSets=sc.sets.filter((s,i)=>!sc.soft[i]);}else F.specSets=[];
}
function renderFixes(){
  const el=$('didyou');
  if(F.exact){el.hidden=false;el.innerHTML='Spelling help is off for this search · <a role="button" tabindex="0" data-x="0">turn it back on</a>';return;}
  if(!F.fixes.length){el.hidden=true;el.innerHTML='';return;}
  el.hidden=false;
  el.innerHTML='Also showing '+F.fixes.map(f=>`<b>${esc(f.to)}</b> for “${esc(f.from)}”`).join(', ')+' · <a role="button" tabindex="0" data-x="1">search only what I typed</a>';
}
function apply(o){
  o=o||{};qDirty=false;
  if(__hydrated){recompile();matched=sortRows(DATA.filter(match));}
  else matched=OV.filter(r=>F.types.has(catOf(r)));          // pre-hydrate: only the kind/type filter can run on OV
  curFC=featuresFor(matched);
  if(mapReady&&map.getSource('pts'))map.getSource('pts').setData(curFC);
  if(__hydrated){
    if(selI>=0&&!match(DATA[selI])){if(pop&&pop.isOpen())pop.remove();else clearSel();}
    else if(lastGroup&&pop&&pop.isOpen()){if(selI<0)pop.remove();else if(!lastGroup.rows.every(match)){lastGroup=null;const r=DATA[selI];showPopup([r.lng,r.lat],popup(r,0));}}
  }
  refreshList();updateChrome();renderFixes();renderLegend();syncCats();
  if(mapReady&&matched.length&&(o.fit==='always'||(o.fit==='auto'&&matched.length<=4000&&!(inviewOn()&&view.length))))fitTo(matched,true);
}

// ---- build controls ----
function mkChip(el,v,html,n,cls){
  const b=document.createElement('button');b.type='button';b.className='chip'+(cls?' '+cls:'');b.dataset.v=v;
  b.setAttribute('aria-pressed','false');b.innerHTML=html+(n!=null?`<span class="n">${num(n)}</span>`:'');el.appendChild(b);return b;
}
function setOn(c,on){c.classList.toggle('on',on);c.setAttribute('aria-pressed',on?'true':'false');}
function chipGroup(el,set,fit){
  el.addEventListener('click',e=>{const c=e.target.closest('.chip');if(!c||!el.contains(c))return;const v=c.dataset.v;
    if(set.has(v)){set.delete(v);setOn(c,false);}else{set.add(v);setOn(c,true);}
    apply({fit:(fit&&set.size)?'always':null});});
}
const typesEl=$('types'),tiersEl=$('tiers'),statesEl=$('states');
const TCNT=META.typeCounts||{},KN=META.kinds||{},PROFS=META.profs||{};
const alliedList=PROF_ORDER.filter(p=>PROFS[p]);                      // allied professions that actually have data
const DTYPES=[(KN.gp||TCNT.GP)?'GP':null,(KN.spec||TCNT.Specialist)?'Specialist':null].filter(Boolean);  // "doctors": shown by default
function alliedToks(){return alliedList.map(p=>'allied:'+p);}
function syncAlliedMaster(){const m=typesEl.querySelector('.chip.allied-master');if(!m)return;const t=alliedToks();setOn(m,t.length>0&&t.every(x=>F.types.has(x)));}
function wireTypes(){
  typesEl.addEventListener('click',e=>{const c=e.target.closest('.chip');if(!c||!typesEl.contains(c))return;const v=c.dataset.v;
    if(v==='__allied'){const t=alliedToks(),on=t.length>0&&t.every(x=>F.types.has(x));   // master: all professions on/off together
      t.forEach(x=>{if(on)F.types.delete(x);else F.types.add(x);});
      typesEl.querySelectorAll('.chip').forEach(ch=>{if(ch.dataset.v.indexOf('allied:')===0)setOn(ch,!on);});
      setOn(c,!on);apply();return;}
    if(F.types.has(v)){F.types.delete(v);setOn(c,false);}else{F.types.add(v);setOn(c,true);}
    if(v.indexOf('allied:')===0)syncAlliedMaster();
    apply();});
}
function syncCats(){const el=$('cats');if(!el||!F.types)return;el.querySelectorAll('.cat[data-v]').forEach(c=>setOn(c,F.types.has(c.dataset.v)));}
function renderLegend(){const L=$('legend');if(!L)return;const it=[];
  if(curCats.has('GP'))it.push(['#1d4ed8','GP']);
  if(curCats.has('Specialist'))it.push(['#0891b2','Specialist']);
  if(curCats.has('Imaging'))it.push(['#d97706','Imaging / pathology']);
  if(curCats.has('Pharmacy'))it.push(['#059669','Pharmacy']);
  if(curCats.has('Hospital'))it.push(['#dc2626','Hospital']);
  if(curCats.has('Physio'))it.push(['#65a30d','Physiotherapy']);
  if(curCats.has('Psych'))it.push(['#7c3aed','Psychology']);
  if(curCats.has('Dental'))it.push(['#4f46e5','Dentist']);
  if(curCats.has('allied'))it.push(['#db2777','Allied health']);
  let h=it.map(x=>`<span><i style="background:${x[0]}"></i>${esc(x[1])}</span>`).join('');
  if(META.hasAvail)h+='<span><i class="bk"></i>Bookable online</span>';
  L.innerHTML=h;}
(function(){
  if(!DTYPES.length&&!alliedList.length&&!KN.diag&&!KN.pharmacy&&!KN.hospital)return;
  $('typeRow').hidden=false;
  // each type chip carries its map-pin colour, so the filter reads as the map key
  const tChip=(v,label,n,col,cls)=>{const b=mkChip(typesEl,v,`<span class="dot" style="background:${col}"></span>${label}`,n,(cls?cls+' ':'')+'tc');b.style.setProperty('--c',col);return b;};
  if(KN.gp||TCNT.GP)tChip('GP','GPs',KN.gp||TCNT.GP,PINCOL.gp);
  if(KN.spec||TCNT.Specialist)tChip('Specialist','Specialists',KN.spec||TCNT.Specialist,PINCOL.sp);
  if(KN.pharmacy)tChip('Pharmacy','Pharmacies',KN.pharmacy,PINCOL.pharmacy);
  if(KN.hospital)tChip('Hospital','Hospitals',KN.hospital,PINCOL.hospital);
  if(KN.dental)tChip('Dental','Dentists',KN.dental,PINCOL.dental);
  if(KN.diag)tChip('Imaging','Imaging &amp; pathology',KN.diag,PINCOL.diag).title='Radiology, nuclear medicine & pathology';
  if(KN.physio)tChip('Physio','Physiotherapists',KN.physio,PINCOL.physio);
  if(KN.psych)tChip('Psych','Psychologists',KN.psych,PINCOL.psych);
  if(alliedList.length){
    const tot=alliedList.reduce((a,p)=>a+(PROFS[p]||0),0);
    tChip('__allied','Other allied health',tot,PINCOL.allied,'allied-master');
    // the individual professions fold away behind one expander so the type list stays short
    const ex=document.createElement('button');ex.type='button';ex.className='chipx';ex.id='alliedExp';ex.setAttribute('aria-expanded','false');
    const exLab=()=>{const open=typesEl.classList.contains('allied-open');ex.innerHTML=(open?'Hide professions':num(alliedList.length)+' professions')+'<span class="chev">▾</span>';ex.setAttribute('aria-expanded',open?'true':'false');};
    ex.addEventListener('click',e=>{e.stopPropagation();typesEl.classList.toggle('allied-open');exLab();});
    typesEl.appendChild(ex);exLab();
    alliedList.forEach(p=>tChip('allied:'+p,PROF_LABEL[p]||p,PROFS[p],PINCOL.allied,'ap'));
  }
  // quick category pills under the search box: one tap toggles a type without opening the filter panel (they drive the same chips)
  (function(){const el=$('cats');if(!el)return;
    [['GP','GPs',PINCOL.gp],['Specialist','Specialists',PINCOL.sp],['Pharmacy','Pharmacies',PINCOL.pharmacy],['Dental','Dentists',PINCOL.dental],['Hospital','Hospitals',PINCOL.hospital],['Imaging','Imaging',PINCOL.diag],['Physio','Physio',PINCOL.physio],['Psych','Psychology',PINCOL.psych]]
      .forEach(([v,lab,col])=>{if(!typesEl.querySelector('.chip[data-v="'+v+'"]'))return;
        const b=document.createElement('button');b.type='button';b.className='cat';b.dataset.v=v;b.setAttribute('aria-pressed','false');b.style.setProperty('--c',col);
        b.innerHTML=`<span class="dot" style="background:${col}"></span>${lab}`;el.appendChild(b);});
    if(alliedList.length){const m=document.createElement('button');m.type='button';m.className='cat more';m.textContent='More…';m.addEventListener('click',()=>{if($('more').hidden)$('moreBtn').click();});el.appendChild(m);}
    el.addEventListener('click',e=>{const c=e.target.closest('.cat[data-v]');if(!c)return;const chip=typesEl.querySelector('.chip[data-v="'+c.dataset.v+'"]');if(chip)chip.click();});
  })();
  F.types=new Set(DTYPES);                                           // doctors on; imaging & allied start hidden
  typesEl.querySelectorAll('.chip').forEach(c=>{if(F.types.has(c.dataset.v))setOn(c,true);});
})();
['A','B','C','D'].filter(t=>META.tiers[t]).forEach(t=>mkChip(tiersEl,t,`<span class="dot" style="background:${tcol(t)}"></span>${t}`,META.tiers[t],'t'+t).title='Tier '+t+(t==='A'?' — top priority':t==='D'?' — lowest priority':''));
['NSW/ACT','VIC','QLD','WA','SA','TAS','NT','?'].filter(s=>META.states[s]).forEach(s=>mkChip(statesEl,s,esc(s==='?'?'Unknown':s),META.states[s]));
(function(){const dl=$('speclist');let h='';META.specialties.forEach(x=>{h+=`<option value="${esc(x[0])}"></option>`;});dl.innerHTML=h;})();
wireTypes();chipGroup(tiersEl,F.tiers);chipGroup(statesEl,F.states,true);
function flag(id,key){$(id).addEventListener('click',()=>{F[key]=!F[key];setOn($(id),F[key]);apply();});}

if(META.hasAvail){$('availChip').hidden=false;flag('availChip','availOnly');}
if(HAS_RATED){$('ratedChip').hidden=false;flag('ratedChip','ratedOnly');}
if(!OV.some(r=>r.rv&&r.rv.score!=null)){const o=$('sortby').querySelector('option[value="rating"]');if(o)o.remove();}
if(!META.hasAvail){const o=$('sortby').querySelector('option[value="avail"]');if(o)o.remove();}
if(!$('sortby').querySelector('option[value="rating"]'))F.sort='tier';   // R1 degenerate: no scores anywhere -> tier
$('sortby').value=F.sort;                                                 // show the default (Top rated) in the control
$('sub').textContent=`${META.count.toLocaleString()} mapped · ${META.specialties.length} specialties · colour = review score`;
// brand line follows the data, so every build path (local build, server refresh) reads the same
(function(){const kind=(TCNT.GP&&TCNT.Specialist)?['GPs & specialists','practitioner map']:TCNT.GP?['GP prospects','GP map']:null;
  if(kind){$('tagline').textContent=kind[0];document.title='docnearme — '+kind[1];}})();
// public-map chrome (presentation only): a broader tagline now the map covers every provider type, and the internal
// A–D "tier" filter + "Best tier" sort are tucked away while a review-score sort exists (nothing in the data changes)
(function(){if(!$('sortby').querySelector('option[value="rating"]'))return;
  if(TCNT.GP&&TCNT.Specialist)$('tagline').textContent='Healthcare near you';
  const fr=tiersEl.closest('.frow');if(fr)fr.hidden=true;
  const o=$('sortby').querySelector('option[value="tier"]');if(o&&F.sort!=='tier')o.remove();})();

// search
let qT=null,qDirty=false;
const qEl=$('q');
qEl.disabled=true;qEl.placeholder='Preparing search…';
function setQuery(v){F.qraw=v;F.q=norm(v);F.toks=F.q?F.q.split(' '):[];}
qEl.addEventListener('input',()=>{setQuery(qEl.value);F.exact=false;qDirty=true;updateChrome();clearTimeout(qT);
  qT=setTimeout(()=>apply({fit:F.q.replace(/\s/g,'').length>=3?'auto':null}),150);});
$('qclear').addEventListener('click',()=>{qEl.value='';setQuery('');F.exact=false;clearTimeout(qT);apply();qEl.focus();});
function fixToggle(e){const a=e.target&&e.target.closest&&e.target.closest('a[data-x]');if(!a)return;if(e.type==='keydown'){if(e.key!=='Enter'&&e.key!==' ')return;e.preventDefault();}
  F.exact=a.dataset.x==='1';apply({fit:'auto'});}
$('didyou').addEventListener('click',fixToggle);$('didyou').addEventListener('keydown',fixToggle);
function setKb(n){
  const rows=listEl.querySelectorAll('.row');if(!rows.length)return;
  if(n>=rows.length&&rendered<view.length){renderMore();return setKb(n);}
  const p=listEl.querySelector('.row.kb');if(p)p.classList.remove('kb');
  kb=Math.max(0,Math.min(rows.length-1,n));
  const el=listEl.querySelectorAll('.row')[kb];if(el){el.classList.add('kb');el.scrollIntoView({block:'nearest'});}
}
qEl.addEventListener('keydown',e=>{
  if(e.key==='ArrowDown'){e.preventDefault();setKb(kb+1);}
  else if(e.key==='ArrowUp'){e.preventDefault();setKb(kb-1);}
  else if(e.key==='Enter'){e.preventDefault();clearTimeout(qT);if(qDirty||!matched.length){setQuery(qEl.value);apply();}
    if(inviewOn()&&mapReady&&!view.length){if(matched.length)fitTo(matched,true);return;}   // nothing in this view: go to the matches
    const el=listEl.querySelectorAll('.row')[kb>=0?kb:0];if(el)select(+el.dataset.i,{fly:true});}
  else if(e.key==='Escape'){if(qEl.value){e.preventDefault();qEl.value='';setQuery('');F.exact=false;clearTimeout(qT);apply();}else qEl.blur();}
});
document.addEventListener('keydown',e=>{
  if(e.key!=='/'||e.metaKey||e.ctrlKey||e.altKey)return;
  const t=e.target,tag=t&&t.tagName;if(tag==='INPUT'||tag==='TEXTAREA'||tag==='SELECT'||(t&&t.isContentEditable))return;
  e.preventDefault();qEl.focus();qEl.select();
});

// specialty (type to filter: exact pick from the list, or any part of a specialty name)
const specEl=$('spec');let sT=null;
specEl.disabled=true;
function setSpec(v,picked){v=v.trim().toLowerCase();F.spec=v;F.specExact=!!picked&&SPECSET.has(v);}
specEl.addEventListener('input',e=>{setSpec(specEl.value,!e.inputType||e.inputType==='insertReplacementText');updateChrome();clearTimeout(sT);sT=setTimeout(()=>apply({fit:F.spec?'auto':null}),160);});
$('specclear').addEventListener('click',()=>{specEl.value='';setSpec('');clearTimeout(sT);apply();specEl.focus();});

// more filters, sort, in-view, reset, location pill, theme
$('moreBtn').addEventListener('click',()=>{const m=$('more'),open=m.hidden;m.hidden=!open;$('side').classList.toggle('more-open',open);$('moreBtn').setAttribute('aria-expanded',open?'true':'false');});
// ---- floating card: the results list opens on demand (button, typing, choosing a filter) and folds away again ----
const isPhone=()=>!!(window.matchMedia&&window.matchMedia('(max-width:760px)').matches);
function syncPad(){if(!map)return;try{const open=!$('side').classList.contains('mini');
  if(isPhone()){const tc=$('topcard'),sh=$('sheet');   // phones: search card on top, results sheet at the bottom — keep the map's focus between them
    map.setPadding({top:(tc?tc.offsetHeight:0)+14,right:0,bottom:sh?Math.min(sh.offsetHeight,Math.round(window.innerHeight*0.64)):0,left:0});}
  else map.setPadding({top:0,right:0,bottom:0,left:open?414:0});}catch(e){}}
function setList(open){const sd=$('side');if(sd.classList.contains('mini')!==open)return;sd.classList.toggle('mini',!open);
  $('listBtn').setAttribute('aria-expanded',open?'true':'false');$('listBtn').querySelector('.lb-t').textContent=open?'Hide':'Show list';syncPad();
  if(open&&rendered<view.length&&listEl.clientHeight>=listEl.scrollHeight-40)renderMore();}
$('listBtn').addEventListener('click',()=>setList($('side').classList.contains('mini')));
// phones: the results sheet opens/closes with a tap on its handle or a short swipe up/down on its header
(function(){const sh=$('sheet'),gr=$('grab');if(!sh||!gr)return;let y0=null;
  gr.addEventListener('click',()=>setList($('side').classList.contains('mini')));
  sh.addEventListener('touchstart',e=>{y0=(e.target.closest&&e.target.closest('#list'))?null:e.touches[0].clientY;},{passive:true});
  sh.addEventListener('touchend',e=>{if(y0==null)return;const dy=e.changedTouches[0].clientY-y0;y0=null;if(Math.abs(dy)>28)setList(dy<0);},{passive:true});})();
$('q').addEventListener('input',()=>{if($('q').value.trim())setList(true);});
$('spec').addEventListener('input',()=>{if($('spec').value.trim())setList(true);});
$('filters').addEventListener('click',e=>{if(e.target.closest('.chip'))setList(true);});
document.addEventListener('click',e=>{if(e.target.closest&&e.target.closest('.gall'))setList(true);});
syncPad();window.addEventListener('resize',syncPad);
$('areaBtn').addEventListener('click',()=>{$('areaBtn').hidden=true;if(!inviewOn()){F.inview=true;F.inviewAuto=false;refreshList();updateChrome();}setList(true);});
$('sortby').addEventListener('change',e=>{F.sort=e.target.value;apply();});
$('inviewBtn').addEventListener('click',()=>{F.inview=!inviewOn();F.inviewAuto=false;refreshList();updateChrome();});
$('locclear').addEventListener('click',()=>{F.loc=null;apply();});
$('reset').addEventListener('click',()=>{
  F.q='';F.toks=[];F.spec='';F.specExact=false;F.states.clear();F.tiers.clear();F.sources.clear();F.types.clear();
  F.emailOnly=false;F.availOnly=false;F.ratedOnly=false;F.loc=null;F.sort='rating';F.inview=false;F.inviewAuto=false;F.exact=false;F.qraw='';
  qEl.value='';specEl.value='';$('sortby').value='rating';clearTimeout(qT);clearTimeout(sT);
  document.querySelectorAll('#side .chip.on').forEach(c=>setOn(c,false));
  F.types=new Set(DTYPES);typesEl.querySelectorAll('.chip').forEach(c=>{if(F.types.has(c.dataset.v))setOn(c,true);});
  if(pop&&pop.isOpen())pop.remove();
  apply();if(mapReady)fitTo(__hydrated?DATA:OV,true);
});
$('themeBtn').addEventListener('click',()=>{
  const t=curTheme()==='dark'?'light':'dark';
  document.documentElement.setAttribute('data-theme',t);
  try{localStorage.setItem('dnm-theme',t);}catch(e){}
  syncMapTheme();
});
if(window.matchMedia){const mq=window.matchMedia('(prefers-color-scheme: dark)');const h=()=>syncMapTheme();
  if(mq.addEventListener)mq.addEventListener('change',h);else if(mq.addListener)mq.addListener(h);}

// list interactions
listEl.addEventListener('click',e=>{const el=e.target.closest('.row');if(!el||el.dataset.i==null)return;if(isPhone())setList(false);select(+el.dataset.i,{fly:true});});
listEl.addEventListener('mouseover',e=>{const el=e.target.closest('.row');const i=el&&el.dataset.i!=null?+el.dataset.i:-1;
  if(i===hovI)return;hovI=i;setPt('hov',i>=0?DATA[i]:null);});
listEl.addEventListener('mouseleave',()=>{if(hovI>=0){hovI=-1;setPt('hov',null);}});
// popup interactions (group list -> detail -> back; list everyone at a location in the sidebar)
document.addEventListener('click',e=>{
  const t=e.target;if(!t||!t.closest)return;
  const gi=t.closest('.gi');
  if(gi&&gi.dataset.i!=null){e.preventDefault();select(+gi.dataset.i,{back:true,keepGroup:true,scroll:true});return;}
  if(t.closest('.gback')){e.preventDefault();if(lastGroup)showGroup(lastGroup);return;}
  if(t.closest('.gall')){e.preventDefault();if(!lastGroup)return;const g=lastGroup;F.loc=g.ids;pop.remove();apply();}
});

$('toastX').addEventListener('click',hideToast);
if(/^https?:$/.test(location.protocol))$('foot').hidden=false;
document.addEventListener('keydown',e=>{if((e.key==='Enter'||e.key===' ')&&e.target&&e.target.closest&&e.target.closest('.gi,.gback,.gall')){e.preventDefault();e.target.closest('.gi,.gback,.gall').click();
  const pe=pop&&pop.isOpen()&&pop.getElement(),f=pe&&pe.querySelector('.gback,.gi,a[href]');if(f)f.focus({preventScroll:true});}});
__hydrateFn=function(){                                 // runs once, when the deferred index arrives
  for(let i=0;i<DATA.length;i++){DATA[i].__i=i;indexRecord(DATA[i]);}
  (function(){const ix=DATA.map((r,i)=>i);ix.sort((a,b)=>{const x=DATA[a].__n,y=DATA[b].__n;return x<y?-1:x>y?1:a-b;});for(let k=0;k<ix.length;k++)DATA[ix[k]].__nr=k;})();
  if(origin){const R=6371,rad=Math.PI/180,cl=Math.cos(origin.lat*rad);          // recompute near-me distances over full data
    for(let i=0;i<DATA.length;i++){const r=DATA[i],dl=(r.lat-origin.lat)*rad,dg=(r.lng-origin.lng)*rad;
      r.__d=2*R*Math.asin(Math.min(1,Math.sqrt(Math.pow(Math.sin(dl/2),2)+cl*Math.cos(r.lat*rad)*Math.pow(Math.sin(dg/2),2))));}}
  qEl.disabled=false;qEl.placeholder='e.g. cardiologist, Chatswood, Dr Smith';document.body.classList.add('hyd');
  specEl.disabled=false;
  apply({fit:'auto'});                                  // first full render: search + all filters now live
};
apply();                                                // overview-time: map on OV, list shows "loading"
}
if(window.__DNM_OV)__dnmOv(window.__DNM_OV);
if(window.__DNM_IX)__dnmIx(window.__DNM_IX);
</script>
</body>
</html>
"""

def main():
    base = merge_specialists()
    print(f"Healthshare base: {len(base)}")
    net_new, rep = load_expansion(base)
    print("\n=== EXPANSION COVERAGE REPORT ===")
    print(f"  sources collected : {rep['files']}  ({rep['collected']} rows)")
    print(f"  overlap (already in base, by name+state): {rep['overlap']}")
    print(f"  NET-NEW added     : {rep['net_new']}   by source: {rep['net_new_by_source']}")
    print(f"  net-new consultant-physician specialties: {rep['phys_new_by_source']}")
    print(f"  net-new tiers (IMMEX scheme): {rep['net_new_tiers']}")
    rows = base + net_new
    print(f"\ntotal after merge : {len(rows)}")
    by_pc_loc, by_pc = load_postcode_geo()
    print(f"postcode geo entries: {len(by_pc)} postcodes, {len(by_pc_loc)} postcode+locality")
    records, missed = geocode(rows, by_pc_loc, by_pc)
    print(f"geocoded: {len(records)}  (missed {missed})")
    build(records)


if __name__ == "__main__":
    main()
