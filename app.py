#!/usr/bin/env python3
"""docnearme map — auth-protected server with a 30-min self-refresh."""
import os, re, json, time, hmac, hashlib, datetime, traceback, ipaddress, urllib.request, urllib.error
from html import escape
from flask import Flask, request, Response, send_from_directory
from apscheduler.schedulers.background import BackgroundScheduler
import server_refresh

HERE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)
USER = os.environ.get("APP_USER", "curbside")
PW = os.environ.get("APP_PASS")
# Beta gate switch. BETA_GATE=off (or 0/false/no) opens the whole site to the public; BETA_GATE=on keeps the login.
# Left unset, the login stays on only while APP_PASS exists, so removing APP_USER/APP_PASS also opens the site.
_gate = (os.environ.get("BETA_GATE") or "").strip().lower()
GATED = False if _gate in ("off", "0", "false", "no") else True if _gate in ("on", "1", "true", "yes") else bool(PW)
_last = {"info": None, "error": None, "at": None}


def do_refresh():
    try:
        _geo_purge()                    # drop expired location lookups even when nobody is visiting
    except Exception:
        pass
    try:
        _last["info"] = server_refresh.refresh()
        _last["error"] = None
    except Exception as e:
        _last["error"] = str(e)
        traceback.print_exc()
    _last["at"] = datetime.datetime.utcnow().isoformat() + "Z"


@app.before_request
def require_auth():
    # /privacy, /about and /contact are deliberately public so the practitioners they address can read
    # them and send a request; everything else needs the login
    if not GATED or request.path in ("/health", "/privacy", "/about", "/contact"):
        return None
    if not PW:
        return Response("Server not configured: APP_PASS is unset.", 500)
    a = request.authorization
    if not a or a.username != USER or a.password != PW:
        return Response("Login required.", 401,
                        {"WWW-Authenticate": 'Basic realm="docnearme"'})
    return None


@app.route("/")
def index():
    r = send_from_directory(HERE, "master_map.html")
    r.headers["Cache-Control"] = "public, max-age=0, s-maxage=300"   # shell is small; let Cloudflare hold it briefly
    return r


@app.route("/data/<path:name>")
def data_file(name):
    # The map records ship here as a content-hashed, immutable file: downloaded once, then cached forever by URL.
    if not re.fullmatch(r"map-(?:ov-|ix-)?[0-9a-f]{6,32}\.js", name):
        return Response("not found", 404)
    r = send_from_directory(os.path.join(HERE, "data_files"), name)
    r.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    r.headers["X-Robots-Tag"] = "noindex"
    r.headers["Content-Type"] = "application/javascript; charset=utf-8"
    return r


# ---- detail on demand: one record by id, email stripped -------------------
# The overview ships only [lat,lng,kind,id,score]; the client fetches /r/<id> on pin click for the full record.
# Source = the full records dump build() already writes (server_refresh sets S.DATA_OUT = HERE/_merged.json),
# same id order as the overview. `em` (and any field in DROP_FIELDS) is removed so emails are never served.
import threading
RECORDS_PATH = os.environ.get("RECORDS_PATH", os.path.join(HERE, "_merged.json"))
DROP_FIELDS = {"em"}            # never serve emails; add "t"/"rg" here too if internal fields shouldn't ship
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")   # inline emails also hide in scraped sub/ph/w
def _scrub_email(s):
    return re.sub(r"\s+", " ", _EMAIL_RE.sub(" ", s)).strip() if (s and "@" in s) else s
_rec_lock = threading.Lock()
_rec = {"mtime": None, "list": []}


def _records():
    try:
        m = os.path.getmtime(RECORDS_PATH)
    except OSError:
        return []
    with _rec_lock:
        if _rec["mtime"] != m:                      # reload after each 30-min refresh rewrites the file
            with open(RECORDS_PATH, encoding="utf-8") as f:
                _rec["list"] = json.load(f)
            _rec["mtime"] = m
        return _rec["list"]


@app.route("/r/<id>")
def record(id):
    if not re.fullmatch(r"\d+", id):
        return Response("not found", 404)
    recs = _records()
    i = int(id)
    if i >= len(recs):
        return Response("not found", 404)
    r = {k: v for k, v in recs[i].items() if k not in DROP_FIELDS}
    for _k, _v in list(r.items()):                 # scrub inline emails from served free-text; drop email-as-URL websites
        if isinstance(_v, str) and "@" in _v:
            r[_k] = "" if _k == "w" else _scrub_email(_v)
    resp = Response(json.dumps(r), mimetype="application/json")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


# ---- static pages (privacy / terms / about) -------------------------------
PAGES = {"privacy": "privacy.html", "terms": "terms.html", "about": "about.html"}


def _read(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


def _page(name):
    return Response(_read(PAGES[name]), mimetype="text/html")


@app.route("/privacy")
def privacy():
    return _page("privacy")


@app.route("/terms")
def terms():
    return _page("terms")


@app.route("/about")
def about():
    return _page("about")


# ---- public contact form ---------------------------------------------------
# The form posts here and the server emails the message. The destination address
# is a service variable (CONTACT_TO) and is never sent to the browser. Delivery
# uses the Resend HTTPS API (RESEND_API_KEY); nothing is stored on the server.
MAIL_TO = os.environ.get("CONTACT_TO", "").strip()
MAIL_FROM = os.environ.get("CONTACT_FROM", "docnearme contact form <onboarding@resend.dev>").strip()
RESEND_KEY = os.environ.get("RESEND_API_KEY", "").strip()
_SECRET = os.urandom(32)
_SENT = {}                       # ip -> [timestamps] (rate limiting, memory only)
ROLES = {"practitioner": "Practitioner", "practice": "Practice manager or staff", "other": "Someone else"}
KINDS = {"correct": "Correct my details", "remove": "Remove my listing", "access": "See what is held about me",
         "privacy": "Privacy question or complaint", "other": "Something else"}
EMAIL_RE = re.compile(r"^[^@\s<>\"',;]+@[^@\s<>\"',;]+\.[^@\s<>\"',;]{2,}$")


def mail_ready():
    return bool(MAIL_TO and RESEND_KEY)


def _token():
    ts = str(int(time.time()))
    return ts + "." + hmac.new(_SECRET, ts.encode(), hashlib.sha256).hexdigest()[:32]


def _token_ok(tok):
    try:
        ts, sig = str(tok).split(".", 1)
        good = hmac.new(_SECRET, ts.encode(), hashlib.sha256).hexdigest()[:32]
        age = time.time() - int(ts)
        return hmac.compare_digest(sig, good) and 2 <= age <= 6 * 3600   # filled in by a person, on a page we served
    except Exception:
        return False


def _one_line(s, n):
    return re.sub(r"[\r\n\t\x00-\x1f\x7f]+", " ", str(s or "")).strip()[:n]


def _rate_ok(ip):
    now, key = time.time(), ip or "unknown"
    for k in list(_SENT.keys()):
        _SENT[k] = [t for t in _SENT[k] if now - t < 3600]
        if not _SENT[k]:
            _SENT.pop(k, None)
    total = sum(len(v) for v in _SENT.values())
    return len(_SENT.get(key, [])) < 5 and total < 60       # 5 an hour per visitor, 60 an hour overall


def _send_mail(subject, body, reply_to):
    payload = json.dumps({"from": MAIL_FROM, "to": [MAIL_TO], "subject": subject, "text": body,
                          "reply_to": reply_to}).encode()
    req = urllib.request.Request("https://api.resend.com/emails", data=payload, method="POST",
                                 headers={"Authorization": "Bearer " + RESEND_KEY, "Content-Type": "application/json",
                                          "Accept": "application/json", "User-Agent": "docnearme-contact/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            ok = 200 <= r.status < 300
            print(f"[contact] delivery {'accepted' if ok else 'refused'} ({r.status})", flush=True)
            return ok
    except urllib.error.HTTPError as e:
        print(f"[contact] delivery refused ({e.code})", flush=True)      # never log the message or the sender
        return False
    except Exception as e:
        print(f"[contact] delivery failed ({e.__class__.__name__})", flush=True)
        return False


def _contact_page(alert="", values=None, state="form", status=200):
    html = _read("contact.html")
    v = values or {}
    if state != "form":
        a, b = html.index("<!--FORM-->"), html.index("<!--/FORM-->") + len("<!--/FORM-->")
        html = html[:a] + html[b:]
    else:
        for field in ("role", "kind"):                      # keep the chosen option when the form is shown again
            a = html.find(f'<select name="{field}">')
            b = html.find("</select>", a)
            if v.get(field) and a >= 0 and b > a:
                html = html[:a] + html[a:b].replace(f'<option value="{v[field]}">', f'<option value="{v[field]}" selected>') + html[b:]
        html = html.replace("__TOKEN__", _token())
        for field in ("name", "email", "subject", "message"):
            html = html.replace(f"__V_{field}__", escape(v.get(field, ""), quote=True))
    resp = Response(html.replace("__ALERT__", alert), status=status, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _alert(kind, title, text):
    return f'<div class="alert {kind}" role="{"status" if kind == "ok" else "alert"}"><b>{escape(title)}</b>{escape(text)}</div>'


@app.route("/contact", methods=["GET", "POST"])
def contact():
    if not mail_ready():
        return _contact_page(_alert("", "The contact form is not accepting messages yet.",
                                    "It is being set up. Please come back soon."), state="off")
    if request.method == "GET":
        if request.args.get("sent") == "1":
            return _contact_page(_alert("ok", "Thank you, your message has been sent.",
                                        "We will reply to the email address you gave."), state="sent")
        return _contact_page()
    f = request.form
    v = {"name": _one_line(f.get("name"), 120), "email": _one_line(f.get("email"), 200),
         "subject": _one_line(f.get("subject"), 200), "message": str(f.get("message") or "").strip()[:5000],
         "role": f.get("role") if f.get("role") in ROLES else "other",
         "kind": f.get("kind") if f.get("kind") in KINDS else "other"}
    if f.get("website"):                                      # hidden field only a script fills in: pretend it worked
        return Response("", 303, {"Location": "/contact?sent=1"})
    problems = []
    if len(v["name"]) < 2:
        problems.append("your name")
    if not EMAIL_RE.match(v["email"]):
        problems.append("a valid email address")
    if len(v["message"]) < 10:
        problems.append("a message of at least a few words")
    if problems:
        return _contact_page(_alert("err", "Please check the form.", "It still needs " + ", ".join(problems) + "."), v, status=400)
    if not _token_ok(f.get("t")):
        return _contact_page(_alert("err", "Please send it again.",
                                    "The form had been open too long, or was submitted too quickly. Your text is still below."), v, status=400)
    ip = _client_ip()
    if not _rate_ok(ip):
        return _contact_page(_alert("err", "Too many messages just now.", "Please try again in an hour."), v, status=429)
    body = "\n".join([
        "New message from the docnearme contact form",
        "",
        f"About:     {KINDS[v['kind']]}",
        f"From:      {v['name']} <{v['email']}>",
        f"They are:  {ROLES[v['role']]}",
        f"Concerns:  {v['subject'] or '(not given)'}",
        f"Sent:      {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC",
        "",
        v["message"],
        "",
        "--",
        "Reply to this email to answer the sender directly.",
    ])
    subject = _one_line(f"[docnearme] {KINDS[v['kind']]}: {v['subject'] or v['name']}", 180)
    if not _send_mail(subject, body, v["email"]):
        return _contact_page(_alert("err", "Sorry, the message could not be sent.",
                                    "Nothing was lost: your text is still below. Please try again in a few minutes."), v, status=502)
    _SENT.setdefault(ip or "unknown", []).append(time.time())
    return Response("", 303, {"Location": "/contact?sent=1"})


# ---- approximate visitor location (city level, from the request IP) -------
# Used only to centre the map when it opens. Nothing is written to disk; results
# are kept in memory briefly so a reload does not trigger another lookup.
_GEO = {}                       # ip -> (expires_at, payload)
GEO_URL = "https://get.geojs.io/v1/ip/geo/{ip}.json"


def _client_ip():
    cands = [request.headers.get("X-Real-IP", ""),
             request.headers.get("X-Forwarded-For", "").split(",")[0],
             request.remote_addr or ""]
    for c in cands:
        c = c.strip()
        if "%" in c:                      # no IPv6 zone suffixes: only a canonical address may pass
            continue
        try:
            a = ipaddress.ip_address(c)
        except ValueError:
            continue
        if a.is_global:
            return str(a)
    return None


def _lookup(ip):
    try:
        req = urllib.request.Request(GEO_URL.format(ip=ip),
                                     headers={"User-Agent": "map-geo/1.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=2.5) as r:
            d = json.load(r)
        lat, lng = float(d["latitude"]), float(d["longitude"])
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return {}
        try:
            acc = float(d.get("accuracy") or 0)
        except (TypeError, ValueError):
            acc = 0
        if not (0 <= acc < 50000):
            acc = 0
        return {"lat": round(lat, 3), "lng": round(lng, 3), "city": str(d.get("city") or "")[:80],
                "region": str(d.get("region") or "")[:80], "country": str(d.get("country_code") or "")[:2],
                "accuracy_km": acc}
    except Exception:
        return {}


def _geo_purge():
    now = time.time()
    for k, v in list(_GEO.items()):
        if v[0] <= now:
            _GEO.pop(k, None)


@app.route("/geo")
def geo():
    _geo_purge()
    out, ip, now = {}, _client_ip(), time.time()
    if ip:
        hit = _GEO.get(ip)
        if hit and hit[0] > now:
            out = hit[1]
        else:
            out = _lookup(ip)
            if len(_GEO) > 5000:
                _GEO.clear()
            _GEO[ip] = (now + (86400 if out else 300), out)
    resp = Response(json.dumps(out), mimetype="application/json")
    resp.headers["Cache-Control"] = "private, max-age=600" if out else "no-store"
    return resp


@app.route("/health")
def health():
    return "ok"


@app.route("/status")
def status():
    return Response(json.dumps(_last), mimetype="application/json")


# 30-minute self-refresh: first run ~90s after boot (serve the shipped map
# meanwhile), then every 30 minutes.
sched = BackgroundScheduler(daemon=True, timezone="UTC")
sched.add_job(do_refresh, "interval", minutes=30, max_instances=1, coalesce=True,
              next_run_time=datetime.datetime.now() + datetime.timedelta(seconds=90))
sched.start()


if __name__ == "__main__":
    app.run("0.0.0.0", int(os.environ.get("PORT", 8080)))
