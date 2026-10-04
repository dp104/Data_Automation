#!/usr/bin/env python3
"""Browser front-end for the university scraper.  Run:  ./uniscrape-web   then open http://localhost:8765
Listens on 127.0.0.1 only unless UNISCRAPE_HOST is set (e.g. to 0.0.0.0 for a hosted deployment)."""
import html
import io
import json
import re
import os
import subprocess
import sys
import threading
import time
import types
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import auth
import billing
import notify
import generic_engine
import project_types
import projects
import template_engine
import uniscrape
from crawler import Fetcher, GenericCrawler
from crawler import base_domain
from extractor import LEVEL_ORDER, MONTHS

HERE = os.path.dirname(os.path.abspath(__file__))
# Writable data (accounts, scans, saved formats/projects, exports, audit log) lives under
# UNISCRAPE_DATA_DIR when set - point it at a mounted persistent disk in production (e.g. a Render
# disk) since the app's own directory is wiped on every redeploy there. Defaults to this folder,
# matching every previous version of the app.
DATA_DIR = os.environ.get("UNISCRAPE_DATA_DIR") or HERE


def _load_env_file(path):
    """A tiny KEY=VALUE loader so `saas.env` works the same on Windows as team.env does on the
    Mac (there sourced by team-start.sh) - never overrides a variable already set in the real
    environment."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_env_file(os.path.join(HERE, "saas.env"))

# Render (and most PaaS hosts) inject PORT and expect the app to bind 0.0.0.0; local use keeps the
# old "this computer only" default so a plain ./uniscrape-web run is never exposed on the network.
HOST = os.environ.get("UNISCRAPE_HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT") or os.environ.get("UNISCRAPE_PORT", "8765"))
# Team mode: the tool sits behind Cloudflare Access or Tailscale, which sign people in with Google
# Workspace and pass their identity in a header. Without a verified @ALLOWED_DOMAIN identity -> 403.
TEAM = os.environ.get("UNISCRAPE_TEAM") == "1"
AUTH = os.environ.get("UNISCRAPE_AUTH", "cloudflare")  # "cloudflare" or "tailscale": only that provider is trusted
# The cloudflare path verifies a signed JWT. The tailscale path can only trust a plain request header
# (Tailscale-User-Login) with NO signature of its own - that's safe only so long as `tailscale serve` is the
# sole way anything ever reaches this port, since it strips any client-supplied copy of that header before
# forwarding. Anyone who can reach this port directly (not through `tailscale serve`) could otherwise forge
# that header and impersonate any @ALLOWED_DOMAIN user. Require an explicit, conscious opt-in before trusting
# it at all, so this is a decision an operator makes on purpose rather than an assumption baked in silently.
TRUST_TAILSCALE_HEADER = os.environ.get("UNISCRAPE_TRUST_TAILSCALE_HEADER") == "1"
ALLOWED_DOMAIN = os.environ.get("UNISCRAPE_ALLOWED_DOMAIN", "flyurdream.com").lower()
CF_TEAM = os.environ.get("CF_ACCESS_TEAM", "")  # e.g. flyurdream  ->  flyurdream.cloudflareaccess.com
CF_AUD = os.environ.get("CF_ACCESS_AUD", "")    # Application Audience (AUD) tag from Cloudflare Access
OUT_DIR = os.environ.get("UNISCRAPE_OUT") or \
    (os.path.join(DATA_DIR, "outputs") if (TEAM or DATA_DIR != HERE) else os.path.expanduser("~/Downloads"))
AUDIT = os.path.join(DATA_DIR, "logs", "audit.log")
_jwks = None

# SaaS admin/subscriber login (separate from the TEAM header-trust mode above). A verified TEAM
# identity still gets full access with no separate account, as before; everyone else signs in here.
ADMIN_EMAIL = os.environ.get("UNISCRAPE_ADMIN_EMAIL", "admin@example.com").strip().lower()
ADMIN_PASSWORD = os.environ.get("UNISCRAPE_ADMIN_PASSWORD", "Admin@12345")


def audit(user, action, **detail):
    os.makedirs(os.path.dirname(AUDIT), exist_ok=True)
    with open(AUDIT, "a") as f:
        f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "user": user, "action": action, **detail}) + "\n")


def identify(headers):
    """Return the signed-in user's email, or None. Local (non-team) mode: the Mac's own user."""
    if not TEAM:
        return "local"
    email = None
    token = headers.get("Cf-Access-Jwt-Assertion")
    if AUTH == "cloudflare":
        if not (token and CF_TEAM and CF_AUD):
            return None  # verify Cloudflare's signed token, don't just trust a header
        global _jwks
        try:
            import jwt
            if _jwks is None:
                _jwks = jwt.PyJWKClient(f"https://{CF_TEAM}.cloudflareaccess.com/cdn-cgi/access/certs")
            key = _jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=CF_AUD)
            email = claims.get("email")
        except Exception:  # noqa: BLE001
            return None
    elif AUTH == "tailscale":  # `tailscale serve` sets this from the signed-in tailnet user and strips client copies
        if not TRUST_TAILSCALE_HEADER:
            return None  # the operator hasn't acknowledged the trust assumption - see TRUST_TAILSCALE_HEADER above
        email = headers.get("Tailscale-User-Login")
    if email and email.lower().endswith("@" + ALLOWED_DOMAIN):
        return email.lower()
    return None


def nav_html(user):
    """The top-bar account cluster substituted into <!--WHO--> on every tool page: who's signed
    in, their plan/status, an Admin link for admins, and a sign-out button."""
    if not user:
        return ""
    if user["role"] == "admin":
        badge = '<span class="planpill plan-active"><i class="ph-fill ph-shield-check"></i>Admin</span>'
    else:
        sub = billing.get_subscription(user["id"])
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        if sub and sub["status"] == "trialing" and sub["trial_end"] and now <= sub["trial_end"]:
            secs_left = max(0, time.mktime(time.strptime(sub["trial_end"], "%Y-%m-%d %H:%M:%S")) - time.time())
            left = f"{max(1, int(secs_left / 3600))}h" if secs_left < 2 * 86400 else f"{int(secs_left / 86400)}d"
            badge = f'<span class="planpill plan-trial"><i class="ph ph-hourglass"></i>Trial - {left} left</span>'
        elif sub and sub["status"] == "active" and sub["period_end"] and now <= sub["period_end"]:
            name = html.escape(billing.PLANS.get(sub["plan"], {}).get("name", sub["plan"] or ""))
            badge = f'<span class="planpill plan-active"><i class="ph-fill ph-check-circle"></i>{name} - Active</span>'
        else:
            badge = '<span class="planpill plan-expired"><a href="/pricing">Choose a plan</a></span>'
    admin_link = '<a class="navlink" href="/admin"><i class="ph ph-gauge"></i>Admin</a>' if user["role"] == "admin" else ""
    email = html.escape(user["email"])
    return (f'<div class="who">{badge}{admin_link}'
            f'<span class="avatar">{html.escape(email[:1].upper())}</span><span class="who-email">{email}</span>'
            f'<button type="button" class="navlink" onclick="fetch(\'/api/auth/logout\',{{method:\'POST\'}})'
            f'.then(()=>location.href=\'/login\')">Logout</button></div>')


OTP_ERRORS = {
    "not_found": "We couldn't find a pending code for this account. Please request a new one.",
    "expired": "This code has expired. Please request a new one.",
    "too_many": "Too many incorrect attempts. Please request a new code.",
    "invalid": "That code isn't right. Please check it and try again.",
}

JOBS = {}          # domain -> {"state", "log", "url", "started"}

_scan_locks = {}
_scan_locks_guard = threading.Lock()


def scan_lock(key):
    """One lock per distinct scan target (a university domain, an any-site profile id, a project id) so
    unrelated scans can run at the same time, while the same target still can never be scanned twice at
    once. (Previously a single process-wide lock serialised every scan of every kind - see git history.)"""
    with _scan_locks_guard:
        return _scan_locks.setdefault(key, threading.Lock())


class LogStream(io.TextIOBase):
    def __init__(self, job):
        self.job = job

    def write(self, s):
        for line in s.splitlines():
            if line.strip():
                self.job["log"].append(line.rstrip())
        del self.job["log"][:-200]
        return len(s)


class _JobStdoutRouter(io.TextIOBase):
    """print() writes to one process-global stream, so naively doing contextlib.redirect_stdout(...) per
    scan lets an unrelated request's print() (e.g. another thread's export-error log line) leak into
    whichever job happens to be the active redirect target at that instant - harmless but very confusing
    once more than one job can run at a time. Routing by the CURRENT THREAD instead keeps each job's log
    to itself no matter how many scans run concurrently; a thread with nothing registered falls through
    to the real stdout (the console), exactly like an unredirected print() always did."""

    def __init__(self, real):
        self._real = real
        self._local = threading.local()

    def route_to(self, stream):
        self._local.target = stream

    def stop_routing(self):
        self._local.target = None

    def write(self, s):
        return (getattr(self._local, "target", None) or self._real).write(s)

    def flush(self):
        target = getattr(self._local, "target", None)
        (target or self._real).flush()


_stdout_router = _JobStdoutRouter(sys.stdout)
sys.stdout = _stdout_router


def cache_path(dom):
    return os.path.join(uniscrape.CACHE, f"{dom}.json")


def run_scan(url, dom, job):
    args = types.SimpleNamespace(render="auto", delay=0.0, max_pages=8000, workers=10)
    with scan_lock(("university", dom)):
        job["state"] = "scanning"
        try:
            _stdout_router.route_to(LogStream(job))
            try:
                courses = uniscrape.scan(url, args)
            finally:
                _stdout_router.stop_routing()
            os.makedirs(uniscrape.CACHE, exist_ok=True)
            with open(cache_path(dom), "w") as f:
                json.dump({"url": url, "scanned_at": time.strftime("%Y-%m-%d %H:%M"),
                           "site_title": uniscrape.scan.site_title, "courses": courses,
                           "fetched_urls": uniscrape.scan.fetched_urls}, f, indent=1)
            job["state"] = "done" if courses else "empty"
        except Exception as e:  # noqa: BLE001
            job["log"].append("ERROR: the scan stopped unexpectedly. Please try again or tell the admin.")
            print(f"scan failed {url}: {e!r}", flush=True)
            job["state"] = "error"


def load(dom):
    with open(cache_path(dom)) as f:
        return json.load(f)


def summary(dom, url):
    data = load(dom)
    courses = data["courses"]
    cfg_file = os.path.join(uniscrape.CONFIGS, f"{dom}.json")
    cfg = json.load(open(cfg_file)) if os.path.exists(cfg_file) else {}
    lite = [{"level": c["level"], "closed": bool(c.get("intl_closed")), "fee": bool(c["fee"]),
             "options": [{"months": o["months"], "mode": o["mode"], "pt": o["part_time"]} for o in c["options"]]}
            for c in courses]
    return {"scanned_at": data["scanned_at"], "courses": lite, "levels": LEVEL_ORDER, "months": MONTHS,
            "defaults": uniscrape.uni_defaults(courses, dom, url, data, cfg)}


def recent_universities():
    out = []
    for name in os.listdir(uniscrape.CACHE) if os.path.isdir(uniscrape.CACHE) else []:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(uniscrape.CACHE, name)) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        cfg = os.path.join(uniscrape.CONFIGS, name)
        saved = ""
        if os.path.exists(cfg):  # the name the team confirmed on the details form wins
            try:
                saved = json.load(open(cfg)).get("name", "")
            except ValueError:
                pass
        out.append({"dom": name[:-5], "url": d.get("url") or f"https://www.{name[:-5]}/",
                    "name": saved or d.get("site_title") or name[:-5], "scanned_at": d.get("scanned_at", ""),
                    "courses": len(d.get("courses", []))})
    return sorted(out, key=lambda x: x["scanned_at"], reverse=True)[:12]


def recent_files():
    if not os.path.isdir(OUT_DIR):
        return []
    files = [f for f in os.listdir(OUT_DIR) if f.endswith(".xlsx")]
    files.sort(key=lambda f: os.path.getmtime(os.path.join(OUT_DIR, f)), reverse=True)
    out = []
    for f in files[:10]:
        csvf = f[:-5] + " - review.csv"
        out.append({"xlsx": f, "csv": csvf if os.path.exists(os.path.join(OUT_DIR, csvf)) else None,
                    "time": time.strftime("%d %b %H:%M", time.localtime(os.path.getmtime(os.path.join(OUT_DIR, f))))})
    return out


def complete_uni(uni, dom, data):
    """Fill in any university field the caller left out or blank, so a missing field never crashes the
    export - the same defaults the web form itself is pre-filled with (uniscrape.uni_defaults)."""
    cfg_file = os.path.join(uniscrape.CONFIGS, f"{dom}.json")
    try:
        cfg = json.load(open(cfg_file)) if os.path.exists(cfg_file) else {}
    except ValueError:
        cfg = {}
    defaults = uniscrape.uni_defaults(data.get("courses", []), dom, data.get("url") or f"https://{dom}/", data, cfg)
    out = dict(defaults)
    out.update({k: v for k, v in (uni or {}).items() if v not in (None, "")})
    return out


def export(req):
    dom = req["dom"]
    if not os.path.exists(cache_path(dom)):
        raise ValueError("Scan this website first.")
    data = load(dom)
    courses = data["courses"]
    if not req.get("incl_closed"):
        courses = [c for c in courses if not c.get("intl_closed")]
    levels = [l for l in LEVEL_ORDER if l in req["levels"]] + [l for l in req["levels"] if l not in LEVEL_ORDER]
    chosen = [c for c in courses if c["level"] in levels]
    order = req["intakes"]
    sheets, review = uniscrape.build_sheets(chosen, levels, req["modes"], order, bool(req.get("incl_pt")))
    if not sheets:
        return {"error": "No courses match that selection."}
    uni = complete_uni(req.get("uni"), dom, data)
    for k in ("cost_of_living", "application_fee"):
        uni[k] = uniscrape.number(uni.get(k, 0))
    uni["deposit_pct"] = uniscrape.number(uni.get("deposit_pct", 0.34), 0.34)
    uni["scholarship"] = {k: uniscrape.number(v) for k, v in uni.get("scholarship", {}).items()}
    tpl = template_engine.load(req["template"]) if req.get("template") else None
    if tpl:  # values typed for the format's fixed-value columns
        uni["fixed"] = {k: v for k, v in (req.get("fixed") or {}).items()}
    os.makedirs(uniscrape.CONFIGS, exist_ok=True)
    with open(os.path.join(uniscrape.CONFIGS, f"{dom}.json"), "w") as f:
        json.dump(uni, f, indent=1)
    os.makedirs(OUT_DIR, exist_ok=True)
    out, rv = uniscrape.save_outputs(sheets, review, uni, OUT_DIR, tpl)
    return {"xlsx": os.path.basename(out), "csv": os.path.basename(rv) if rv else None, "review": len(review),
            "sheets": [[m, len(r)] for m, r in sheets]}


def clean_uni(uni):
    for k in ("cost_of_living", "application_fee"):
        if k in uni:
            uni[k] = uniscrape.number(uni.get(k, 0))
    uni["scholarship"] = {k: uniscrape.number(v) for k, v in (uni.get("scholarship") or {}).items()}
    return uni


def export_many(req):
    """Several universities in one go: one file each, or all of them in one file (combined)."""
    tpl = template_engine.load(req["template"])
    if req.get("layout") == "per_intake" and not tpl["per_intake"]:
        tpl = dict(tpl, per_intake=True, sheet_style="Mon")
    levels = [l for l in LEVEL_ORDER if l in req["levels"]] + [l for l in req["levels"] if l not in LEVEL_ORDER]
    per_site = []
    for site in req["sites"]:
        dom = site["dom"]
        if not os.path.exists(cache_path(dom)):
            raise ValueError(f"{dom}: scan this website first.")
        data = load(dom)
        courses = data["courses"]
        if not req.get("incl_closed"):
            courses = [c for c in courses if not c.get("intl_closed")]
        chosen = [c for c in courses if c["level"] in levels]
        sheets, review = uniscrape.build_sheets(chosen, levels, req["modes"], req["intakes"], bool(req.get("incl_pt")))
        uni = clean_uni(complete_uni(site.get("uni"), dom, data))
        uni["fixed"] = dict(site.get("fixed") or {})
        os.makedirs(uniscrape.CONFIGS, exist_ok=True)
        with open(os.path.join(uniscrape.CONFIGS, f"{dom}.json"), "w") as f:
            json.dump(uni, f, indent=1)
        per_site.append((dom, uni, sheets, review))
    os.makedirs(OUT_DIR, exist_ok=True)
    files = []
    if req.get("combined") and len(per_site) > 1:
        months = [m for m in req["intakes"]]
        merged, review = [], []
        for m in months:
            rows = [dict(r, _uni=uni) for _, uni, sheets, _ in per_site for mm, rs in sheets if mm == m for r in rs]
            if rows:
                merged.append((m, rows))
        for _, uni, _, rv in per_site:
            review += [[uni.get("name", "")] + r for r in rv]
        if not merged:
            return {"error": "No courses match that selection."}
        name = (req.get("file_name") or f"{len(per_site)} universities").strip()[:80]
        out, rv = uniscrape.save_outputs(merged, [r[1:] for r in review], {"name": name}, OUT_DIR, tpl)
        files.append({"xlsx": os.path.basename(out), "csv": os.path.basename(rv) if rv else None, "review": len(review),
                      "sheets": [[m, len(r)] for m, r in merged], "name": name})
    else:
        for dom, uni, sheets, review in per_site:
            if not sheets:
                files.append({"name": uni.get("name") or dom, "error": "No courses match that selection."})
                continue
            out, rv = uniscrape.save_outputs(sheets, review, uni, OUT_DIR, tpl)
            files.append({"xlsx": os.path.basename(out), "csv": os.path.basename(rv) if rv else None,
                          "review": len(review), "sheets": [[m, len(r)] for m, r in sheets], "name": uni.get("name") or dom})
    return {"files": files}


# ---------------------------------------------------------------- "Any website" mode
ANY_JOBS = {}  # profile id -> {"state", "log", "rows", "fetched"}
ANY_CACHE = os.path.join(DATA_DIR, "cache", "any")


def any_cache(pid):
    return os.path.join(ANY_CACHE, f"{pid}.json")


def any_learn(req):
    t = template_engine.load(req["template"])
    examples = [e for e in req.get("examples", []) if str(e.get("url", "")).startswith("http")][:5]
    if not examples:
        raise ValueError("Add at least one example page link.")
    if any(urlparse(e["url"]).path.strip("/") == "" for e in examples):
        raise ValueError("One example link is the website's homepage. Paste the page of that row's item instead.")
    f = Fetcher()
    try:
        res = generic_engine.learn(t, examples, f.get)
    finally:
        f.close()
    return res


def any_scan(pid, start_url, max_pages):
    job = ANY_JOBS[pid]
    prof = generic_engine.load_profile(pid)
    pattern = re.compile(prof["pattern"])
    rows, seen = [], {}

    def on_page(u, html):
        if not pattern.search(urlparse(u).path):
            return False
        row, got = generic_engine.extract(prof, html, u)
        if got == 0:
            return False
        # the same item reached by another address ("?term=2027-28", tracking parameters): keep the fullest row
        key = urlparse(u).path.rstrip("/")
        filled = sum(1 for v in row.values() if v not in (None, ""))
        row["_url"] = u
        if key in seen:
            i = seen[key]
            if filled > sum(1 for k, v in rows[i].items() if k != "_url" and v not in (None, "")):
                rows[i] = row
            return False
        seen[key] = len(rows)
        rows.append(row)
        job["rows"] = rows
        return True

    with scan_lock(("any", pid)):
        job["state"] = "scanning"
        fetcher = Fetcher()
        try:
            _stdout_router.route_to(LogStream(job))
            try:
                if fetcher.is_blocked(start_url):
                    print("   site blocks plain requests -> using headless Chrome for every page")
                    fetcher.browser_only = True
                seeds = [e for e in prof.get("examples", [])]
                crawler = GenericCrawler(start_url, fetcher, on_page, prof["pattern"], max_pages=max_pages, workers=8)
                crawler.run(seeds)
            finally:
                _stdout_router.stop_routing()
            os.makedirs(ANY_CACHE, exist_ok=True)
            with open(any_cache(pid), "w") as f:
                json.dump({"scanned_at": time.strftime("%Y-%m-%d %H:%M"), "rows": rows, "start_url": start_url}, f)
            job["state"] = "done" if rows else "empty"
        except Exception as e:  # noqa: BLE001
            job["log"].append("ERROR: the scan stopped unexpectedly. Please try again.")
            print(f"any scan failed {pid}: {e!r}", flush=True)
            job["state"] = "error"
        finally:
            fetcher.close()


def any_export(req):
    prof = generic_engine.load_profile(req["profile"])
    t = template_engine.load(prof["template"])
    rows = req.get("rows")
    if rows is None:
        rows = json.load(open(any_cache(prof["id"])))["rows"]
    ruled = [c["header"] for c in prof["columns"] if c.get("rule")]
    fixed = {k: v for k, v in (req.get("fixed") or {}).items()}
    prof["fixed"] = fixed
    generic_engine.save_profile(prof)
    name = re.sub(r'[\\/:*?"<>|]+', " ", (req.get("file_name") or f"{prof['domain']} - {t['name']}")).strip()[:80]
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"{name}.xlsx")
    n = 2
    while os.path.exists(out):
        out = os.path.join(OUT_DIR, f"{name} ({n}).xlsx")
        n += 1
    template_engine.render_rows(t, rows, out, fixed)
    review = [[r.get("_url", "")] + [h for h in ruled if r.get(h) in (None, "")] for r in rows
              if any(r.get(h) in (None, "") for h in ruled)]
    rv = None
    if review:
        rv = out[:-5] + " - review.csv"
        import csv
        with open(rv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Page", "Columns with no value on that page"])
            w.writerows([[r[0], ", ".join(r[1:])] for r in review])
    return {"xlsx": os.path.basename(out), "csv": os.path.basename(rv) if rv else None, "review": len(review),
            "rows": len(rows)}


# ---------------------------------------------------------------- typed projects
def project_run(pid, max_pages):
    job = projects.JOBS[pid]
    with scan_lock(("project", pid)):
        job["state"] = "scanning"
        try:
            _stdout_router.route_to(LogStream(job))
            try:
                rows, notes = projects.run(pid, max_pages=max_pages)
            finally:
                _stdout_router.stop_routing()
            job["notes"] = notes
            job["state"] = "done" if rows else "empty"
        except Exception as e:  # noqa: BLE001
            job["log"].append("ERROR: the collection stopped unexpectedly. Please try again.")
            print(f"project run failed {pid}: {e!r}", flush=True)
            job["state"] = "error"


def project_view(pid):
    p = projects.load(pid)
    r = projects.last_run(pid) or {}
    rows = r.get("rows", [])
    changes = [x for x in rows if x.get("_change") in ("up", "down")]
    t = project_types.TYPES[p["type"]]
    return {"project": p, "type": project_types.public(t), "finished_at": r.get("finished_at", ""), "count": len(rows),
            "notes": r.get("notes", []), "preview": rows[:30], "changes": changes[:200],
            "fields": [{"key": f["key"], "header": f["header"], "type": f["type"]} for f in t["fields"]]}


_REDOS_PROBE_SRC = "import re,sys\ntry:\n re.compile(sys.argv[1]).search('a'*40+'!')\nexcept Exception:\n pass\n"


def pattern_is_safe(pattern, budget=3.0):
    """Best-effort ReDoS guard: reject a page-matching pattern that can't even finish a worst-case probe
    match within `budget` seconds. Runs the probe in a disposable, dependency-free subprocess (not a
    thread, and not multiprocessing.Process - both of those would re-import this whole module with its
    heavy dependencies, which is slow enough on its own to cause false positives) so a catastrophically-
    backtracking regex can be killed outright instead of hanging a worker thread forever. Fails OPEN
    (treats the pattern as safe) if the probe itself can't run, so an environment where subprocesses
    can't be spawned never loses the save feature over this."""
    try:
        subprocess.run([sys.executable, "-c", _REDOS_PROBE_SRC, pattern], timeout=budget,
                       capture_output=True, check=False)
        return True
    except subprocess.TimeoutExpired:
        return False
    except Exception:  # noqa: BLE001
        return True


class Handler(BaseHTTPRequestHandler):
    # Without this, a request that claims a large Content-Length but sends its body slowly (or not at all)
    # would make self.rfile.read(n) block that worker thread forever. 30s is generous for a local request.
    timeout = 30

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json", headers=None):
        b = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def resolve_user(self):
        """The account behind this request: a verified TEAM header identity (treated as a staff
        admin with no separate signup needed), or the SaaS session cookie's account, or None."""
        staff_email = identify(self.headers)
        if TEAM and staff_email:
            return {"id": f"staff:{staff_email}", "email": staff_email, "role": "admin", "name": staff_email}
        return auth.user_from_cookie(self.headers.get("Cookie"))

    def require(self, level):
        """'public': no login needed. 'auth': any signed-in account. 'member': signed-in and
        (admin, or an active/trialing subscription). 'admin': signed-in admin only.
        Sends the right redirect/error and returns False when access is refused."""
        self.auth_user = self.resolve_user()
        self.user = self.auth_user["email"] if self.auth_user else None
        if level == "public":
            return True
        if not self.auth_user:
            if self.command == "GET":
                self.send(302, "", "text/plain", {"Location": "/login?next=" + quote(self.path, safe="")})
            else:
                self.send(401, {"error": "Please sign in."})
            return False
        if level == "admin" and self.auth_user["role"] != "admin":
            self.send(403, {"error": "Admin access only."})
            return False
        if level == "member" and not billing.has_access(self.auth_user):
            if self.command == "GET":
                self.send(302, "", "text/plain", {"Location": "/pricing"})
            else:
                self.send(402, {"error": "Your trial or subscription has ended. Please choose a plan to continue."})
            return False
        return True

    PUBLIC_GET = {"/login", "/signup", "/forgot-password", "/pricing", "/app.css", "/favicon.ico",
                  "/api/auth/me", "/api/billing/plans", "/healthz"}
    PUBLIC_POST = {"/api/auth/login", "/api/auth/signup", "/api/auth/verify-signup-otp", "/api/auth/resend-otp",
                   "/api/auth/forgot-password", "/api/auth/reset-password"}
    ADMIN_PATHS = {"/admin", "/api/admin/stats"}
    AUTH_ONLY_PATHS = {"/api/auth/logout", "/api/billing/create-order", "/api/billing/verify"}

    def level_for(self, path):
        if path in self.PUBLIC_GET or path in self.PUBLIC_POST:
            return "public"
        if path in self.ADMIN_PATHS:
            return "admin"
        if path in self.AUTH_ONLY_PATHS:
            return "auth"
        return "member"

    def render_page(self, page_name):
        who = nav_html(self.auth_user)
        with open(os.path.join(HERE, "web", page_name), encoding="utf-8") as f:
            page = f.read()
        return self.send(200, page.replace("<!--WHO-->", who), "text/html; charset=utf-8")

    def do_GET(self):
        u = urlparse(self.path)
        if not self.require(self.level_for(u.path)):
            return
        if u.path == "/healthz":
            return self.send(200, "ok", "text/plain")
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path in ("/", "/project", "/university"):
            page_name = {"/": "home.html", "/project": "project.html", "/university": "index.html"}[u.path]
            return self.render_page(page_name)
        if u.path == "/login":
            return self.render_page("login.html")
        if u.path == "/signup":
            return self.render_page("signup.html")
        if u.path == "/forgot-password":
            return self.render_page("forgot-password.html")
        if u.path == "/pricing":
            return self.render_page("pricing.html")
        if u.path == "/admin":
            return self.render_page("admin.html")
        if u.path == "/api/auth/me":
            if not self.auth_user:
                return self.send(200, {"user": None})
            sub = billing.get_subscription(self.auth_user["id"])
            return self.send(200, {"user": {"email": self.auth_user["email"], "name": self.auth_user.get("name"),
                                            "role": self.auth_user["role"]},
                                   "subscription": sub, "access": billing.has_access(self.auth_user)})
        if u.path == "/api/admin/stats":
            return self.send(200, billing.admin_stats())
        if u.path == "/api/billing/plans":
            return self.send(200, {"order": billing.PLAN_ORDER, "plans": billing.PLANS,
                                   "trial_days": billing.TRIAL_DAYS, "key_id": billing.RAZORPAY_KEY_ID})
        if u.path == "/api/owner":  # who to contact for a new kind of project (configs/owner.json)
            try:
                o = json.load(open(os.path.join(DATA_DIR, "configs", "owner.json")))
            except (OSError, ValueError):
                o = {}
            return self.send(200, {"name": str(o.get("name", "")), "email": str(o.get("email", ""))})
        if u.path == "/api/types":
            return self.send(200, [project_types.public(project_types.TYPES[k]) for k in project_types.ORDER])
        if u.path == "/api/projects":
            return self.send(200, projects.list_projects())
        if u.path == "/api/project":
            try:
                return self.send(200, project_view(q.get("id", "")))
            except (OSError, ValueError, KeyError):
                return self.send(404, {"error": "Project not found."})
        if u.path == "/api/project/status":
            job = projects.JOBS.get(q.get("id", ""), {"state": "idle", "log": [], "count": 0})
            return self.send(200, {"state": job["state"], "log": job.get("log", [])[-25:], "count": job.get("count", 0),
                                   "notes": job.get("notes", [])})
        if u.path == "/api/project/mapping":
            try:
                tpl = template_engine.load(q.get("template", ""))
                return self.send(200, {"mapping": projects.suggest_mapping(tpl, q.get("type", "")),
                                       "fields": [{"key": f["key"], "header": f["header"]} for f in project_types.TYPES[q["type"]]["fields"]]})
            except (OSError, ValueError, KeyError):
                return self.send(404, {"error": "Format not found."})
        if u.path == "/app.css":
            with open(os.path.join(HERE, "web", "app.css"), "rb") as f:
                return self.send(200, f.read(), "text/css; charset=utf-8", {"Cache-Control": "no-cache"})
        if u.path == "/favicon.ico":
            svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
                   '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
                   '<stop offset="0" stop-color="#5b4fe8"/><stop offset="1" stop-color="#13b0a6"/></linearGradient></defs>'
                   '<rect width="32" height="32" rx="8" fill="url(#g)"/>'
                   '<text x="16" y="23" font-family="Arial,Helvetica,sans-serif" font-size="17" '
                   'font-weight="700" fill="#ffffff" text-anchor="middle">D</text></svg>')
            return self.send(200, svg, "image/svg+xml", {"Cache-Control": "public, max-age=604800"})
        if u.path in ("/any", "/any/"):
            return self.render_page("any.html")
        if u.path == "/api/any/profiles":
            return self.send(200, generic_engine.list_profiles())
        if u.path == "/api/any/profile":
            try:
                return self.send(200, generic_engine.load_profile(q.get("id", "")))
            except (OSError, ValueError):
                return self.send(404, {"error": "Site setup not found."})
        if u.path == "/api/any/status":
            pid = q.get("id", "")
            job = ANY_JOBS.get(pid)
            if not job and os.path.exists(any_cache(pid)):
                d = json.load(open(any_cache(pid)))
                job = {"state": "done", "log": [f"Saved scan from {d['scanned_at']}"], "rows": d["rows"]}
            job = job or {"state": "idle", "log": [], "rows": []}
            return self.send(200, {"state": job["state"], "log": job["log"][-25:], "count": len(job["rows"]),
                                   "preview": job["rows"][:25] if job["state"] != "scanning" else job["rows"][-25:]})
        if u.path == "/api/templates":
            return self.send(200, template_engine.list_templates())
        if u.path == "/api/template":
            try:
                t = template_engine.load(q.get("id", ""))
            except (OSError, ValueError):
                return self.send(404, {"error": "Format not found."})
            return self.send(200, {"template": t, "fields": template_engine.FIELDS,
                                   "uni_fields": template_engine.uni_fields(t),
                                   "intake_year": template_engine.needs_intake_year(t),
                                   "questions": template_engine.questions(t)})
        if u.path == "/api/recent":
            return self.send(200, recent_universities())
        if u.path == "/api/files":
            return self.send(200, recent_files())
        if u.path == "/api/status":
            job = JOBS.get(q.get("dom"), {"state": "idle", "log": []})
            res = {"state": job["state"], "log": job["log"][-25:]}
            if job["state"] == "done":
                res["summary"] = summary(q["dom"], job["url"])
            return self.send(200, res)
        if u.path == "/api/download":
            name = os.path.basename(q.get("f", ""))
            path = os.path.join(OUT_DIR, name)
            if not name or not os.path.isfile(path) or not name.endswith((".xlsx", ".csv")):
                return self.send(404, {"error": "not found"})
            ctype = "text/csv" if name.endswith(".csv") else \
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            audit(self.user, "download", file=name)
            with open(path, "rb") as f:
                return self.send(200, f.read(), ctype,
                                 {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.require(self.level_for(self.path)):
            return
        n = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self.send(400, {"error": "bad request"})
        if self.path == "/api/auth/signup":
            try:
                u = auth.create_user(req.get("email", ""), req.get("password", ""), req.get("name", ""), verified=False)
            except ValueError as e:
                return self.send(400, {"error": str(e)})
            otp = auth.create_verification(u["email"], "signup")
            notify.send_email(u["email"], "Verify your Data Scraper account", notify.otp_email_body(otp, "signup"))
            audit(u["email"], "signup_pending")
            resp = {"ok": True, "stage": "otp", "email": u["email"]}
            if not notify.EMAIL_CONFIGURED:
                resp["dev_otp"] = otp
            return self.send(200, resp)
        if self.path == "/api/auth/verify-signup-otp":
            email = auth.normalize_email(req.get("email", ""))
            result = auth.check_verification(email, "signup", req.get("otp", ""))
            if result != "ok":
                return self.send(400, {"error": OTP_ERRORS[result]})
            u = auth.get_user_by_email(email)
            if not u:
                return self.send(400, {"error": "Account not found. Please sign up again."})
            auth.mark_verified(u["id"])
            auth.consume_verification(email, "signup")
            billing.start_trial(u["id"])
            token = auth.create_session(u["id"])
            audit(email, "signup_verified")
            return self.send(200, {"ok": True}, headers={"Set-Cookie": auth.session_cookie_header(token)})
        if self.path == "/api/auth/resend-otp":
            email = auth.normalize_email(req.get("email", ""))
            purpose = req.get("purpose") if req.get("purpose") in ("signup", "reset") else "signup"
            u = auth.get_user_by_email(email)
            if purpose == "signup" and (not u or u["verified"]):
                return self.send(400, {"error": "Nothing to verify for this account."})
            try:
                otp = auth.create_verification(email, purpose, resend=True)
            except ValueError as e:
                return self.send(429, {"error": str(e)})
            if u:
                notify.send_email(email, "Your verification code", notify.otp_email_body(otp, purpose))
            resp = {"ok": True}
            if not notify.EMAIL_CONFIGURED and u:
                resp["dev_otp"] = otp
            return self.send(200, resp)
        if self.path == "/api/auth/login":
            u = auth.authenticate(req.get("email", ""), req.get("password", ""))
            if not u:
                return self.send(400, {"error": "Incorrect email or password."})
            if not u["verified"]:
                otp = auth.create_verification(u["email"], "signup")
                notify.send_email(u["email"], "Verify your Data Scraper account", notify.otp_email_body(otp, "signup"))
                resp = {"error": "Please verify your email first - we've sent a new code.",
                        "needs_verification": True, "email": u["email"]}
                if not notify.EMAIL_CONFIGURED:
                    resp["dev_otp"] = otp
                return self.send(403, resp)
            token = auth.create_session(u["id"])
            audit(u["email"], "login")
            return self.send(200, {"ok": True, "role": u["role"]}, headers={"Set-Cookie": auth.session_cookie_header(token)})
        if self.path == "/api/auth/logout":
            token = auth.session_token_from_headers(self.headers)
            if token:
                auth.destroy_session(token)
            return self.send(200, {"ok": True}, headers={"Set-Cookie": auth.clear_cookie_header()})
        if self.path == "/api/auth/forgot-password":
            email = auth.normalize_email(req.get("email", ""))
            u = auth.get_user_by_email(email)
            resp = {"ok": True}  # always "ok" - never reveal whether an account exists
            if u:
                otp = auth.create_verification(email, "reset")
                notify.send_email(email, "Reset your Data Scraper password", notify.otp_email_body(otp, "reset"))
                audit(email, "password_reset_requested")
                if not notify.EMAIL_CONFIGURED:
                    resp["dev_otp"] = otp
            return self.send(200, resp)
        if self.path == "/api/auth/reset-password":
            email = auth.normalize_email(req.get("email", ""))
            result = auth.check_verification(email, "reset", req.get("otp", ""))
            if result != "ok":
                return self.send(400, {"error": OTP_ERRORS[result]})
            u = auth.get_user_by_email(email)
            if not u:
                return self.send(400, {"error": "Account not found."})
            try:
                auth.set_password(u["id"], req.get("new_password", ""))
            except ValueError as e:
                return self.send(400, {"error": str(e)})
            auth.consume_verification(email, "reset")
            audit(email, "password_reset")
            return self.send(200, {"ok": True})
        if self.path == "/api/billing/create-order":
            try:
                return self.send(200, billing.create_order(self.auth_user["id"], req.get("plan", "")))
            except (ValueError, RuntimeError) as e:
                return self.send(400, {"error": str(e)})
        if self.path == "/api/billing/verify":
            try:
                sub = billing.verify_payment(self.auth_user["id"], req.get("plan", ""), req.get("order_id", ""),
                                             req.get("payment_id", ""), req.get("signature", ""))
            except (ValueError, KeyError) as e:
                return self.send(400, {"error": str(e)})
            audit(self.auth_user["email"], "payment", plan=sub["plan"], amount=sub["amount"])
            return self.send(200, {"ok": True, "subscription": sub})
        if self.path == "/api/scan":
            url = (req.get("url") or "").strip()
            if not url:
                return self.send(400, {"error": "Enter a university website link."})
            if not url.startswith("http"):
                url = "https://" + url
            dom = base_domain(urlparse(url).netloc)
            job = JOBS.get(dom)
            if job and job["state"] in ("scanning", "queued"):
                return self.send(200, {"dom": dom})
            JOBS[dom] = job = {"state": "queued", "log": [], "url": url}
            audit(self.user, "scan", url=url, rescan=bool(req.get("rescan")))
            billing.log_usage(self.auth_user["id"], "scan", url)
            if os.path.exists(cache_path(dom)) and not req.get("rescan"):
                job["state"] = "done"
                job["log"].append(f"Using saved scan from {load(dom)['scanned_at']} (tick 'Scan again' for fresh data)")
            else:
                threading.Thread(target=run_scan, args=(url, dom, job), daemon=True).start()
            return self.send(200, {"dom": dom})
        if self.path == "/api/template/learn":  # body: {name, filename, data (base64 .xlsx)}
            import base64
            import tempfile
            try:
                raw = base64.b64decode(req.get("data", ""))
                if len(raw) > 15_000_000 or not raw.startswith(b"PK"):
                    return self.send(400, {"error": "Please upload an Excel .xlsx file (max 15 MB)."})
                with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
                    tmp.write(raw)
                name = (req.get("name") or os.path.splitext(req.get("filename") or "My format")[0]).strip()[:60]
                t = template_engine.learn(tmp.name, name)
                os.unlink(tmp.name)
            except ValueError as e:
                return self.send(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                print(f"template learn failed for {self.user}: {e!r}", flush=True)
                return self.send(400, {"error": "Could not read that Excel file. Check it is a normal .xlsx with a header row."})
            return self.send(200, {"template": t, "fields": template_engine.FIELDS, "questions": template_engine.questions(t)})
        if self.path == "/api/template/save":  # body: {template} after the user reviewed the column mapping
            t = req.get("template") or {}
            if not t.get("id") or not t.get("columns"):
                return self.send(400, {"error": "Nothing to save."})
            for c in t["columns"]:
                if c.get("field") not in template_engine.FIELDS:
                    c["field"] = "blank"
            t["sample"] = os.path.basename(str(t.get("sample", "")))
            if not os.path.isfile(os.path.join(template_engine.TEMPLATES, t["sample"])):
                return self.send(400, {"error": "The sample file for this format is missing. Upload it again."})
            allowed = {"id", "name", "sample", "header_row", "sheet_title", "per_intake", "sheet_style", "columns", "created",
                       "answered", "rows", "kind"}
            t = {k: v for k, v in t.items() if k in allowed}
            t["id"] = template_engine.slug(t.get("name") or t["id"])
            template_engine.save(t)
            audit(self.user, "save_format", format=t["name"], columns=[c["header"] for c in t["columns"]])
            return self.send(200, {"ok": True, "id": t["id"]})
        if self.path == "/api/project/save":
            t = project_types.TYPES.get(req.get("type"))
            if not t or "fields" not in t:
                return self.send(400, {"error": "Unknown project type."})
            sites = []
            for x in req.get("sites", []):
                x = str(x).strip()
                if x and not x.startswith("http"):
                    x = "https://" + x
                if re.match(r"^https?://[^/\s]+\.[^/\s]+", x):
                    sites.append(x)
            if not sites:
                return self.send(400, {"error": "Add at least one website link."})
            name = (req.get("name") or t["name"]).strip()[:80]
            pid = req.get("id")
            if not pid:
                # a brand new project: atomically claim a unique id so two people (or two tabs) creating
                # a same-named project at the same moment can never silently overwrite one another
                base = projects.pid_of(f"{t['id']}-{name}")
                os.makedirs(projects.PROJECTS, exist_ok=True)
                pid = base
                for attempt in range(6):
                    try:
                        fd = os.open(os.path.join(projects.PROJECTS, f"{pid}.json"), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                        os.close(fd)
                        break
                    except FileExistsError:
                        pid = f"{base}-{uuid.uuid4().hex[:6]}"
                else:
                    return self.send(409, {"error": "Could not claim a unique project id. Try again."})
            fmt = req.get("format") or "standard"
            p = {"id": pid, "name": name, "type": t["id"], "sites": sites[:20], "format": fmt,
                 "mapping": req.get("mapping") if fmt != "standard" else None, "created": time.strftime("%Y-%m-%d")}
            projects.save(p)
            audit(self.user, "project_save", project=pid, type=t["id"], sites=sites)
            return self.send(200, {"ok": True, "id": pid})
        if self.path == "/api/project/run":
            pid = str(req.get("id", ""))
            try:
                projects.load(pid)
            except (OSError, ValueError):
                return self.send(404, {"error": "Project not found."})
            job = projects.JOBS.get(pid)
            if job and job["state"] in ("scanning", "queued"):
                return self.send(200, {"ok": True})
            projects.JOBS[pid] = {"state": "queued", "log": [], "count": 0, "notes": []}
            maxp = max(20, min(int(req.get("max_pages") or 1500), 8000))
            audit(self.user, "project_run", project=pid, max_pages=maxp)
            billing.log_usage(self.auth_user["id"], "project_run", pid)
            threading.Thread(target=project_run, args=(pid, maxp), daemon=True).start()
            return self.send(200, {"ok": True})
        if self.path == "/api/project/export":
            try:
                p = projects.load(str(req.get("id", "")))
                os.makedirs(OUT_DIR, exist_ok=True)
                res = projects.export(p, OUT_DIR, req.get("fixed") or {})
                audit(self.user, "project_export", project=p["id"], file=res["xlsx"], rows=res["rows"])
                billing.log_usage(self.auth_user["id"], "project_export", p["id"])
                return self.send(200, res)
            except ValueError as e:
                return self.send(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                print(f"project export failed: {e!r}", flush=True)
                return self.send(500, {"error": "Could not create the Excel file. Please try again."})
        if self.path == "/api/project/delete":
            pid = re.sub(r"[^a-z0-9-]", "", str(req.get("id", "")))
            for f in (os.path.join(projects.PROJECTS, f"{pid}.json"), projects.run_file(pid)):
                if os.path.exists(f):
                    os.remove(f)
            audit(self.user, "project_delete", project=pid)
            return self.send(200, {"ok": True})
        if self.path == "/api/any/learn":
            try:
                return self.send(200, any_learn(req))
            except ValueError as e:
                return self.send(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                print(f"any learn failed for {self.user}: {e!r}", flush=True)
                return self.send(400, {"error": "Could not read the example pages. Check the links open in a browser."})
        if self.path == "/api/any/save":
            if not req.get("template"):
                return self.send(400, {"error": "Choose an output format first."})
            try:
                t = template_engine.load(req["template"])
            except (OSError, ValueError, KeyError):
                return self.send(404, {"error": "That output format no longer exists. Choose it again."})
            dom = str(req.get("domain", "")).strip().lower()
            if not dom:
                return self.send(400, {"error": "A website is required."})
            try:
                re.compile(req.get("pattern") or ".*")
            except re.error:
                return self.send(400, {"error": "The page pattern is not valid."})
            if not pattern_is_safe(req.get("pattern") or ".*"):
                return self.send(400, {"error": "That page pattern is too complex and could hang the crawler. Try a simpler one."})
            pid = generic_engine.profile_id(t["id"], dom)
            cols = []
            for c in t["columns"]:
                got = next((x for x in req.get("columns", []) if x.get("header") == c["header"]), {})
                rule = got.get("rule") if isinstance(got.get("rule"), dict) else None
                if rule and rule.get("type") not in ("css", "label", "jsonld", "meta", "page_url"):
                    rule = None
                cols.append({"header": c["header"], "rule": rule, "sample": got.get("sample", ""), "typed": bool(got.get("typed")),
                             "default": str(got.get("default", ""))[:300]})
            prof = {"id": pid, "name": f"{dom} ({t['name']})", "template": t["id"], "domain": dom,
                    "pattern": req.get("pattern") or ".*", "columns": cols,
                    "examples": [e for e in req.get("examples", []) if str(e).startswith("http")][:5],
                    "created": time.strftime("%Y-%m-%d")}
            try:
                generic_engine.save_profile(prof)
            except Exception as e:  # noqa: BLE001
                print(f"any save failed for {self.user}: {e!r}", flush=True)
                return self.send(500, {"error": "Could not save the site setup. Please try again."})
            audit(self.user, "any_save", profile=pid)
            return self.send(200, {"ok": True, "id": pid})
        if self.path == "/api/any/scan":
            pid = str(req.get("profile", ""))
            try:
                prof = generic_engine.load_profile(pid)
            except (OSError, ValueError):
                return self.send(404, {"error": "Site setup not found."})
            start = (req.get("start_url") or f"https://{prof['domain']}/").strip()
            if not start.startswith("http"):
                start = "https://" + start
            if urlparse(start).netloc.lower().removeprefix("www.") != prof["domain"].removeprefix("www."):
                return self.send(400, {"error": "The start link must be on " + prof["domain"]})
            job = ANY_JOBS.get(pid)
            if job and job["state"] in ("scanning", "queued"):
                return self.send(200, {"ok": True})
            if os.path.exists(any_cache(pid)) and not req.get("rescan"):
                return self.send(200, {"ok": True, "cached": True})
            ANY_JOBS[pid] = {"state": "queued", "log": [], "rows": []}
            maxp = max(20, min(int(req.get("max_pages") or 2000), 8000))
            audit(self.user, "any_scan", profile=pid, start=start, max_pages=maxp)
            billing.log_usage(self.auth_user["id"], "any_scan", pid)
            threading.Thread(target=any_scan, args=(pid, start, maxp), daemon=True).start()
            return self.send(200, {"ok": True})
        if self.path == "/api/any/export":
            try:
                res = any_export(req)
                audit(self.user, "any_export", profile=req.get("profile"), file=res["xlsx"], rows=res["rows"])
                billing.log_usage(self.auth_user["id"], "any_export", req.get("profile") or "")
                return self.send(200, res)
            except Exception as e:  # noqa: BLE001
                print(f"any export failed for {self.user}: {e!r}", flush=True)
                return self.send(500, {"error": "Could not create the Excel file. Please try again."})
        if self.path == "/api/export_many":
            try:
                res = export_many(req)
                audit(self.user, "export", format=req.get("template"), sites=[x.get("dom") for x in req.get("sites", [])],
                      combined=bool(req.get("combined")), files=[f.get("xlsx") for f in res.get("files", [])],
                      levels=req.get("levels"), intakes=req.get("intakes"))
                billing.log_usage(self.auth_user["id"], "export_many", "")
                return self.send(200, res)
            except ValueError as e:
                return self.send(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                print(f"export failed for {self.user}: {e!r}", flush=True)
                return self.send(500, {"error": "Could not create the Excel file. Please try again or tell the admin."})
        if self.path == "/api/export":
            try:
                res = export(req)
                audit(self.user, "export", dom=req.get("dom"), file=res.get("xlsx"), format=req.get("template"),
                      levels=req.get("levels"),
                      intakes=req.get("intakes"))
                billing.log_usage(self.auth_user["id"], "export", req.get("dom") or "")
                return self.send(200, res)
            except ValueError as e:
                return self.send(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                # full detail for the admin log only; users never see server paths
                print(f"export failed for {self.user}: {e!r}", flush=True)
                return self.send(500, {"error": "Could not create the Excel file. Please try again or tell the admin."})
        self.send(404, {"error": "not found"})


PAGE_FILE = os.path.join(HERE, "web", "index.html")


def main():
    template_engine.seed_standard_format()
    created = auth.ensure_admin(ADMIN_EMAIL, ADMIN_PASSWORD)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    link = f"http://localhost:{PORT}"
    if HOST == "127.0.0.1":
        print(f"Data Scraper SaaS is running:  {link}\n(keep this window open; close it to stop the tool)")
    else:
        print(f"Data Scraper SaaS is running on {HOST}:{PORT}")
    if DATA_DIR != HERE:
        print(f"Data directory: {DATA_DIR} (set via UNISCRAPE_DATA_DIR)")
    if created:
        print(f"Admin account ready -> {ADMIN_EMAIL} / {ADMIN_PASSWORD}  (set UNISCRAPE_ADMIN_EMAIL / "
              "UNISCRAPE_ADMIN_PASSWORD in saas.env to change this; do that before any real deployment).")
    if billing.RAZORPAY_KEY_ID.startswith("rzp_test_SAMPLEKEYID"):
        print("Razorpay is on SAMPLE placeholder keys - payments will show a clear error until you set real "
              "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET (test or live) in saas.env. Everything else (signup, "
              "login, trial, admin dashboard) works right now.")
    if TEAM:
        print(f"TEAM MODE: only verified @{ALLOWED_DOMAIN} users; files in {OUT_DIR}; audit log {AUDIT}")
        if AUTH == "tailscale":
            if TRUST_TAILSCALE_HEADER:
                print("WARNING: UNISCRAPE_AUTH=tailscale trusts the 'Tailscale-User-Login' header with no "
                      "signature check of its own. This is safe ONLY if `tailscale serve` is the sole way "
                      "anything ever reaches this port - if this port is ever reachable another way (a stray "
                      "port-forward, another local process, binding beyond 127.0.0.1), anyone could forge that "
                      f"header and impersonate any @{ALLOWED_DOMAIN} user. Prefer UNISCRAPE_AUTH=cloudflare "
                      "(a verified JWT) if you can. See TEAM_SETUP.md.")
            else:
                print("UNISCRAPE_AUTH=tailscale is set but UNISCRAPE_TRUST_TAILSCALE_HEADER=1 was not, so every "
                      "request will be refused (403) until you set it - that variable is your acknowledgement "
                      "that trust depends entirely on `tailscale serve` being the only path to this port.")
    if not os.environ.get("UNISCRAPE_NO_BROWSER") and not TEAM and HOST == "127.0.0.1":
        threading.Timer(1.0, lambda: webbrowser.open(link)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
