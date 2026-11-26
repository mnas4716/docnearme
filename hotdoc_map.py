#!/usr/bin/env python3
"""
HotDoc availability mapper
==========================

Searches HotDoc the same way your browser does (it renders each public "find"
page and reads what's on screen), collects every clinic's address and its
next *bookable* appointment slots, looks up each clinic's coordinates, and
writes an interactive map (map.html) with a pin per clinic coloured by how
soon you can get in.

What it can and cannot see:
  - It sees OPEN/bookable slots that HotDoc shows publicly. It cannot see a
    doctor's full roster or their already-booked times.
  - There is no public HotDoc API for this, so it reads the rendered pages.
    Keep the scope and request rate modest and personal.

Usage:
    ./.venv/bin/python hotdoc_map.py

Edit the CONFIG block below to change the area, specialties, and size.
"""

from __future__ import annotations
import json, re, sys, time, datetime as dt
from pathlib import Path
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# CONFIG  —  edit this block
# ---------------------------------------------------------------------------
CONFIG = {
    # Which HotDoc specialties to include. Each is a URL segment in
    # https://www.hotdoc.com.au/find/<vertical>/<STATE>/<suburb-postcode>
    # Common ones: doctor (GP), dentist, psychologist, physiotherapist,
    # optometrist, dietitian, podiatrist. Unknown/empty ones are skipped.
    "verticals": ["doctor", "dentist", "psychologist", "physiotherapist",
                  "optometrist", "podiatrist", "dietitian"],

    # Seed locations to search around. HotDoc returns ~30 nearest clinics per
    # seed, sorted by distance, so a few well-spread suburbs cover a city.
    # Each entry is (STATE, "suburb-postcode") exactly as in the HotDoc URL.
    "seeds": [
        ("NSW", "sydney-2000"),          # CBD
        ("NSW", "bondi-junction-2022"),  # east
        ("NSW", "randwick-2031"),        # south-east
        ("NSW", "chatswood-2067"),       # lower north shore
        ("NSW", "manly-2095"),           # northern beaches
        ("NSW", "hornsby-2077"),         # upper north
        ("NSW", "parramatta-2150"),      # central/west
        ("NSW", "blacktown-2148"),       # western Sydney
        ("NSW", "liverpool-2170"),       # south-west
        ("NSW", "hurstville-2220"),      # south
        ("NSW", "cronulla-2230"),        # far south
        ("NSW", "penrith-2750"),         # far west
    ],

    # Safety caps so a run stays modest.
    "max_clinics": 900,      # stop after this many unique clinics
    "delay_sec": 1.2,        # pause between page loads (be polite)
    "headless": True,        # set False to watch it work
    "page_timeout_ms": 45000,
}

OUT_DIR = Path(__file__).resolve().parent
DATA_DIR = OUT_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
GEO_CACHE = DATA_DIR / "geo_cache.json"

BASE = "https://www.hotdoc.com.au"

# ---------------------------------------------------------------------------
# In-page extraction (runs inside the rendered page)
# ---------------------------------------------------------------------------
_EXTRACT_CARDS_JS = r"""
() => {
  const anchors = [...document.querySelectorAll('a[href*="/medical-centres/"]')]
      .filter(a => /\/medical-centres\/[^/]+\/[^/]+\/(doctors|dentists|practitioners)$/
                     .test(a.getAttribute('href') || ''));
  const seen = new Map();
  for (const a of anchors) {
    let el = a, card = null;
    for (let i = 0; i < 7 && el; i++) {
      el = el.parentElement;
      if (el && /km away|Next Available/.test(el.innerText || '')) { card = el; break; }
    }
    if (card) {
      const href = a.href.split('?')[0];
      if (!seen.has(href)) seen.set(href, card.innerText);
    }
  }
  return [...seen.entries()].map(([href, text]) => ({ href, text }));
}
"""

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*(am|pm)\b", re.I)
DIST_RE = re.compile(r"([\d.]+)\s*km away", re.I)
DATED_RE = re.compile(r"\b([A-Z][a-z]{2}),\s*(\d{1,2})\s+([A-Z][a-z]{2})\b")  # "Mon, 16 Nov"


def _parse_slot_time(h: int, m: int, ap: str) -> tuple[int, int]:
    ap = ap.lower()
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return h, m


def parse_card(text: str, today: dt.date) -> dict:
    """Turn a card's innerText into a structured record."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    rec: dict = {"name": None, "address": None, "status": None,
                 "distance_km": None, "routine_requests": False,
                 "next_date_label": None, "slots": [], "earliest": None}
    if lines:
        rec["name"] = lines[0]
    # address: the line that ends in a state abbreviation
    for l in lines[1:4]:
        if re.search(r"\b(NSW|VIC|QLD|SA|WA|TAS|NT|ACT)\b", l):
            rec["address"] = l
            break
    for l in lines:
        if "·" in l and ("Open" in l or "Closed" in l):
            rec["status"] = l.replace("·", "-").strip()
            break
    if (dm := DIST_RE.search(text)):
        rec["distance_km"] = float(dm.group(1))
    rec["routine_requests"] = "Routine Requests" in text

    # date label after "Next Available"
    tail = text.split("Next Available", 1)[1] if "Next Available" in text else text
    label = None
    target = None
    if re.search(r"\bToday\b", tail):
        label, target = "Today", today
    elif re.search(r"\bTomorrow\b", tail):
        label, target = "Tomorrow", today + dt.timedelta(days=1)
    elif (dd := DATED_RE.search(tail)):
        day = int(dd.group(2)); mon = MONTHS.get(dd.group(3).lower())
        if mon:
            year = today.year
            cand = dt.date(year, mon, day)
            if cand < today:                      # month already passed -> next year
                cand = dt.date(year + 1, mon, day)
            label, target = dd.group(0), cand
    rec["next_date_label"] = label

    # slot times
    slots = []
    for (h, m, ap) in TIME_RE.findall(tail):
        slots.append(f"{int(h)}:{m}{ap.lower()}")
    rec["slots"] = slots

    if target is not None:
        first = TIME_RE.search(tail)
        if first:
            hh, mm = _parse_slot_time(int(first.group(1)), int(first.group(2)), first.group(3))
            earliest = dt.datetime(target.year, target.month, target.day, hh, mm)
        else:
            earliest = dt.datetime(target.year, target.month, target.day, 23, 59)
        rec["earliest"] = earliest.isoformat()
        rec["days_away"] = (target - today).days
    else:
        rec["days_away"] = None
    return rec


def load_geo_cache() -> dict:
    if GEO_CACHE.exists():
        try:
            return json.loads(GEO_CACHE.read_text())
        except Exception:
            return {}
    return {}


def save_geo_cache(cache: dict) -> None:
    GEO_CACHE.write_text(json.dumps(cache, indent=1))


_GEO_RES = [
    re.compile(r'"latitude"\s*:\s*(-?\d+\.\d+)\s*,\s*"longitude"\s*:\s*(-?\d+\.\d+)'),
    re.compile(r'[?&]ll=(-?\d+\.\d+)(?:%2C|,)\s*(-?\d+\.\d+)'),
    re.compile(r'(-3[0-9]\.\d{4,})[,%]\w*(1[0-9]{2}\.\d{4,})'),
]


def _find_geo(html: str):
    for rx in _GEO_RES:
        m = rx.search(html)
        if m:
            return float(m.group(1)), float(m.group(2))
    return None


def scrape(cfg: dict) -> list[dict]:
    today = dt.date.today()
    geo_cache = load_geo_cache()
    clinics: dict[str, dict] = {}   # keyed by clinic href

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=cfg["headless"])
        page = browser.new_page(viewport={"width": 1400, "height": 2000})
        page.set_default_timeout(cfg["page_timeout_ms"])

        # 1) collect clinic cards from each find page.
        # Loop seeds-outer / verticals-inner so every area contributes ALL
        # service types before the next area — keeps type coverage balanced
        # if the max_clinics cap is reached.
        stop = False
        for state, suburb in cfg["seeds"]:
            if stop:
                break
            for vertical in cfg["verticals"]:
                if len(clinics) >= cfg["max_clinics"]:
                    stop = True
                    break
                url = f"{BASE}/find/{vertical}/{state}/{suburb}"
                print(f"[find] {vertical:12s} {state}/{suburb} ...", flush=True)
                try:
                    page.goto(url, wait_until="domcontentloaded")
                    page.wait_for_function(
                        "() => /Next Available|No available|km away/.test(document.body.innerText)",
                        timeout=20000)  # bad/empty seed fails fast
                    page.wait_for_timeout(1200)
                    cards = page.evaluate(_EXTRACT_CARDS_JS)
                except Exception as e:
                    print(f"   ! skipped ({e.__class__.__name__})", flush=True)
                    continue
                for c in cards:
                    href = c["href"]
                    if href in clinics or len(clinics) >= cfg["max_clinics"]:
                        continue
                    rec = parse_card(c["text"], today)
                    rec["href"] = href
                    rec["vertical"] = vertical
                    clinics[href] = rec
                print(f"   -> {len(cards)} cards, {len(clinics)} unique so far", flush=True)
                time.sleep(cfg["delay_sec"])

        # 2) resolve coordinates for each unique clinic (cached)
        for i, (href, rec) in enumerate(clinics.items(), 1):
            if href in geo_cache:
                rec["lat"], rec["lng"] = geo_cache[href]
                continue
            print(f"[geo {i}/{len(clinics)}] {rec['name']}", flush=True)
            try:
                page.goto(href, wait_until="domcontentloaded")
                # coords arrive in a JSON-LD block injected shortly after render
                try:
                    page.wait_for_function(
                        "() => /\"latitude\"\\s*:|[?&]ll=/.test(document.documentElement.outerHTML)",
                        timeout=9000)
                except Exception:
                    pass
                page.wait_for_timeout(300)
                geo = _find_geo(page.content())
                if geo:
                    rec["lat"], rec["lng"] = geo
                    geo_cache[href] = list(geo)
                    save_geo_cache(geo_cache)
                else:
                    rec["lat"] = rec["lng"] = None
                    print("   ! no coords found", flush=True)
            except Exception as e:
                rec["lat"] = rec["lng"] = None
                print(f"   ! geo error ({e.__class__.__name__})", flush=True)
            if i % 25 == 0:   # checkpoint so a long run is recoverable
                (DATA_DIR / "clinics.json").write_text(
                    json.dumps(list(clinics.values()), indent=1))
            time.sleep(cfg["delay_sec"])

        browser.close()

    result = list(clinics.values())
    (DATA_DIR / "clinics.json").write_text(json.dumps(result, indent=1))
    print(f"\nSaved {len(result)} clinics -> {DATA_DIR/'clinics.json'}", flush=True)
    return result


# ---------------------------------------------------------------------------
# Map generation (Leaflet + OpenStreetMap, fully self-contained HTML)
# ---------------------------------------------------------------------------
def build_html(clinics: list[dict], out: Path) -> None:
    pts = [c for c in clinics if c.get("lat") and c.get("lng")]
    gj = json.dumps(pts)
    generated = dt.datetime.now().strftime("%a %d %b %Y, %-I:%M%p")
    html = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HotDoc availability map</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<style>
  :root{
    --bg:#f4f6f8; --panel:#ffffff; --ink:#1c2733; --muted:#5b6b7b; --line:#e2e8ee;
    --c0:#15a34a; --c1:#7cc243; --c2:#f0a92b; --c3:#e8603c; --cn:#9aa7b4;
  }
  @media (prefers-color-scheme: dark){
    :root:not([data-theme="light"]){
      --bg:#0f141a; --panel:#182028; --ink:#e7edf3; --muted:#9fb0bf; --line:#263340;
    }
  }
  :root[data-theme="dark"]{
    --bg:#0f141a; --panel:#182028; --ink:#e7edf3; --muted:#9fb0bf; --line:#263340;
  }
  *{box-sizing:border-box}
  html,body{height:100%;margin:0}
  body{font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
       color:var(--ink);background:var(--bg)}
  #wrap{display:flex;height:100vh;width:100%}
  #side{width:340px;flex:0 0 340px;background:var(--panel);border-right:1px solid var(--line);
        display:flex;flex-direction:column;overflow:hidden}
  #head{padding:16px 16px 10px}
  #head h1{font-size:17px;margin:0 0 2px}
  #head p{margin:0;color:var(--muted);font-size:12px}
  #legend{display:flex;flex-wrap:wrap;gap:6px 12px;padding:8px 16px 12px;border-bottom:1px solid var(--line);font-size:12px}
  .lg{display:flex;align-items:center;gap:6px;color:var(--muted)}
  .dot{width:11px;height:11px;border-radius:50%;display:inline-block}
  #list{overflow:auto;flex:1}
  .row{padding:11px 16px;border-bottom:1px solid var(--line);cursor:pointer}
  .row:hover{background:rgba(127,127,127,.07)}
  .row .nm{font-weight:600;font-size:13.5px;margin-bottom:2px}
  .row .ad{color:var(--muted);font-size:12px;margin-bottom:4px}
  .row .av{font-size:12px;display:flex;align-items:center;gap:6px}
  .vt{display:inline-block;font-size:10px;font-weight:700;letter-spacing:.02em;
      text-transform:uppercase;color:#fff;background:var(--muted);border-radius:4px;
      padding:1px 5px;vertical-align:middle}
  #filters{display:flex;flex-wrap:wrap;gap:5px;padding:9px 16px;border-bottom:1px solid var(--line)}
  #filters button{font:inherit;font-size:11px;border:1px solid var(--line);background:transparent;
      color:var(--muted);border-radius:12px;padding:2px 9px;cursor:pointer}
  #filters button.on{background:var(--ink);color:var(--panel);border-color:var(--ink)}
  #map{flex:1}
  .pin{border:2px solid #fff;border-radius:50%;box-shadow:0 1px 4px rgba(0,0,0,.4)}
  .pop b{font-size:14px}
  .pop .sub{color:#667;font-size:12px;margin:3px 0 6px}
  .pop .slots{display:flex;flex-wrap:wrap;gap:4px;margin:6px 0}
  .pop .slot{background:#eef3f7;border-radius:4px;padding:2px 6px;font-size:11px;color:#1c2733}
  .pop a{display:inline-block;margin-top:6px;color:#1061c4;text-decoration:none;font-weight:600;font-size:12px}
  @media (max-width:700px){#wrap{flex-direction:column}#side{width:100%;flex:0 0 44%;border-right:0;border-bottom:1px solid var(--line)}}
</style>
</head>
<body>
<div id="wrap">
  <div id="side">
    <div id="head">
      <h1>Doctor availability</h1>
      <p>__COUNT__ clinics &middot; generated __GEN__</p>
    </div>
    <div id="legend">
      <span class="lg"><i class="dot" style="background:var(--c0)"></i>Today</span>
      <span class="lg"><i class="dot" style="background:var(--c1)"></i>Tomorrow</span>
      <span class="lg"><i class="dot" style="background:var(--c2)"></i>This week</span>
      <span class="lg"><i class="dot" style="background:var(--c3)"></i>Later</span>
      <span class="lg"><i class="dot" style="background:var(--cn)"></i>None shown</span>
    </div>
    <div id="filters"></div>
    <div id="list"></div>
  </div>
  <div id="map"></div>
</div>
<script>
const CLINICS = __DATA__;
const COLORS = ["#15a34a","#7cc243","#f0a92b","#e8603c","#9aa7b4"];
const VLABEL = {doctor:"GP",dentist:"Dentist",psychologist:"Psychologist",
  physiotherapist:"Physio",optometrist:"Optometrist",podiatrist:"Podiatrist",
  dietitian:"Dietitian"};
// nudge clinics that share identical coordinates so each pin stays clickable
(function(){
  const g={};
  CLINICS.forEach(c=>{ if(!c.lat)return; const k=c.lat.toFixed(5)+','+c.lng.toFixed(5); (g[k]=g[k]||[]).push(c); });
  Object.values(g).forEach(arr=>{ if(arr.length<2)return;
    arr.forEach((c,i)=>{ const a=2*Math.PI*i/arr.length, r=0.0003*(1+Math.floor(i/arr.length));
      c.lat+=r*Math.cos(a); c.lng+=r*Math.sin(a); }); });
})();
function bucket(c){
  const d = c.days_away;
  if (d === null || d === undefined) return 4;
  if (d <= 0) return 0;
  if (d === 1) return 1;
  if (d <= 6) return 2;
  return 3;
}
function color(c){ return COLORS[bucket(c)]; }

const map = L.map('map');
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {maxZoom:19, attribution:'&copy; OpenStreetMap'}).addTo(map);

const markers = {};
const bounds = [];
const active = new Set();  // active verticals
CLINICS.sort((a,b)=>bucket(a)-bucket(b) || ((a.days_away??99)-(b.days_away??99)));
CLINICS.forEach((c,i)=>{
  if(!c.lat||!c.lng) return;
  active.add(c.vertical);
  bounds.push([c.lat,c.lng]);
  const m = L.circleMarker([c.lat,c.lng],{
    radius:9, color:'#fff', weight:2, fillColor:color(c), fillOpacity:0.95
  }).addTo(map);
  const slots=(c.slots||[]).slice(0,8).map(s=>`<span class="slot">${s}</span>`).join('');
  const when = c.next_date_label ? `<b style="color:${color(c)}">${c.next_date_label}</b>` : 'No availability shown';
  const vlab = VLABEL[c.vertical] ? `<span class="vt">${VLABEL[c.vertical]}</span> ` : '';
  m.bindPopup(
    `<div class="pop">${vlab}<b>${c.name||''}</b>`+
    `<div class="sub">${c.address||''}${c.distance_km!=null?' &middot; '+c.distance_km+'km':''}</div>`+
    `Next available: ${when}`+
    (slots?`<div class="slots">${slots}</div>`:'')+
    `<a href="${c.href}" target="_blank" rel="noopener">View &amp; book on HotDoc &rarr;</a></div>`
  );
  markers[i]=m;
});
if(bounds.length) map.fitBounds(bounds,{padding:[40,40]});
else map.setView([-33.8908,151.2743],12);

// side list
const list = document.getElementById('list');
const rows = {};
CLINICS.forEach((c,i)=>{
  if(!c.lat||!c.lng) return;
  const row=document.createElement('div');
  row.className='row';
  const vlab = VLABEL[c.vertical] ? `<span class="vt">${VLABEL[c.vertical]}</span> ` : '';
  row.innerHTML =
    `<div class="nm">${vlab}${c.name||''}</div>`+
    `<div class="ad">${c.address||''}</div>`+
    `<div class="av"><i class="dot" style="background:${color(c)}"></i>`+
    `${c.next_date_label? c.next_date_label + (c.slots&&c.slots[0]? ' · '+c.slots[0]:'') : 'No availability shown'}</div>`;
  row.onclick=()=>{ map.setView([c.lat,c.lng],15); markers[i].openPopup(); };
  list.appendChild(row);
  rows[i]=row;
});

// service-type filter chips
const fbar=document.getElementById('filters');
const verts=[...active];
function apply(){
  CLINICS.forEach((c,i)=>{
    if(!markers[i])return;
    if(active.has(c.vertical)){ if(!map.hasLayer(markers[i])) markers[i].addTo(map); rows[i].style.display=''; }
    else { if(map.hasLayer(markers[i])) map.removeLayer(markers[i]); rows[i].style.display='none'; }
  });
}
if(verts.length>1){
  verts.sort();
  verts.forEach(v=>{
    const btn=document.createElement('button');
    btn.textContent=VLABEL[v]||v; btn.className='on';
    btn.onclick=()=>{ if(active.has(v)){active.delete(v);btn.classList.remove('on');}
                      else{active.add(v);btn.classList.add('on');} apply(); };
    fbar.appendChild(btn);
  });
} else { fbar.style.display='none'; }
</script>
</body>
</html>
"""
    html = (html.replace("__DATA__", gj)
                .replace("__COUNT__", str(len(pts)))
                .replace("__GEN__", generated))
    out.write_text(html)
    print(f"Wrote map -> {out}", flush=True)


def main():
    clinics = scrape(CONFIG)
    build_html(clinics, OUT_DIR / "map.html")
    withgeo = sum(1 for c in clinics if c.get("lat"))
    print(f"\nDone. {withgeo}/{len(clinics)} clinics mapped. Open map.html")


if __name__ == "__main__":
    main()
