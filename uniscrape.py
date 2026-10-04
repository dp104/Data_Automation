#!/usr/bin/env python3
"""University course scraper -> Flyurdream Excel format.

Usage:
    ./uniscrape https://www.rgu.ac.uk/
    ./uniscrape https://www.rgu.ac.uk/ --rescan             # ignore cached scan
    ./uniscrape https://www.rgu.ac.uk/ --levels Postgraduate,Undergraduate --intakes September,January --yes
"""
import argparse
import csv
import faulthandler
import json
import os
import re
import signal
import sys
import time
from collections import Counter
from urllib.parse import urlparse

from crawler import COURSE_HINT, Crawler, Fetcher, base_domain, discover_sitemap_urls, normalise
from excel_writer import write_workbook
from bs4 import BeautifulSoup

import extractor
from extractor import LEVEL_ORDER, MONTHS, extract, fee_only

HERE = os.path.dirname(os.path.abspath(__file__))
# Writable data lives under UNISCRAPE_DATA_DIR when set (point it at a mounted persistent disk in
# production - e.g. Render - since the app's own directory is wiped on every redeploy there).
DATA_DIR = os.environ.get("UNISCRAPE_DATA_DIR") or HERE
CACHE = os.path.join(DATA_DIR, "cache")
CONFIGS = os.path.join(DATA_DIR, "configs")

TLD_COUNTRY = {"uk": "United kingdom", "au": "Australia", "ca": "Canada", "nz": "New Zealand", "ie": "Ireland",
               "de": "Germany", "fr": "France", "nl": "Netherlands", "es": "Spain", "it": "Italy", "se": "Sweden",
               "fi": "Finland", "dk": "Denmark", "no": "Norway", "pl": "Poland", "cz": "Czech Republic",
               "hu": "Hungary", "at": "Austria", "ch": "Switzerland", "be": "Belgium", "pt": "Portugal",
               "mt": "Malta", "cy": "Cyprus", "lt": "Lithuania", "lv": "Latvia", "ee": "Estonia", "sg": "Singapore",
               "my": "Malaysia", "ae": "United Arab Emirates", "hk": "Hong Kong", "jp": "Japan", "kr": "South Korea",
               "edu": "USA", "us": "USA", "fr": "France"}


def ask(prompt, default=""):
    if not sys.stdin.isatty():
        return str(default)
    d = f" [{default}]" if default not in ("", None) else ""
    try:
        v = input(f"{prompt}{d}: ").strip()
    except EOFError:
        v = ""
    return v or str(default)


def choose(title, options, counts, preset=None, default_all=True):
    """Numbered multi-select. Returns list of chosen options."""
    if preset and preset.strip().lower() in ("all", "*"):
        return list(options)
    if preset:
        wanted = [p.strip().lower() for p in preset.split(",")]
        return [o for o in options if o.lower() in wanted or o.lower()[:3] in [w[:3] for w in wanted]]
    print(f"\n{title}")
    if not options:
        print("   (none)")
        return []
    for i, o in enumerate(options, 1):
        print(f"   {i:>2}. {o:<22} ({counts.get(o, 0)} courses)")
    while True:
        raw = ask("   Choose numbers separated by comma (e.g. 1,3 or 1-4), or 'all'", "all" if default_all else "")
        if raw.lower() in ("all", "a", "*"):
            return list(options)
        picked = []
        for part in re.split(r"[,\s]+", raw):
            if "-" in part and all(x.isdigit() for x in part.split("-", 1)):
                a, b = map(int, part.split("-", 1))
                picked += options[a - 1:b]
            elif part.isdigit() and 1 <= int(part) <= len(options):
                picked.append(options[int(part) - 1])
            elif part:
                picked += [o for o in options if o.lower().startswith(part.lower())]
        if picked or not sys.stdin.isatty():
            break
        print("   Please enter numbers from the list above.")
    return list(dict.fromkeys(picked))


def number(v, default=0):
    try:
        f = float(str(v).replace(",", "").replace("£", "").replace("$", "").replace("€", "").strip())
        return int(f) if f.is_integer() else f
    except ValueError:
        return default


def scan(url, args):
    dom = base_domain(urlparse(url).netloc)
    fetcher = Fetcher(render=args.render, delay=args.delay)
    extractor.set_region(dom.rsplit(".", 1)[-1])
    records = {}
    fee_cache = {}
    rendered = {}

    def on_page(u, html):
        try:
            recs = extract(u, html)
            if recs and not rendered.get(u) and fetcher.render_mode != "never" and not recs[0]["fee"] \
                    and not any(o["months"] or o["duration"] for o in recs[0]["options"]):
                # course page whose details load by JavaScript: read it again in the browser
                rendered[u] = True
                _, fu2, html2 = fetcher.browser_get(u, spa=True)
                if not html2:  # busy browser / timeout: one more try
                    _, fu2, html2 = fetcher.browser_get(u, spa=True)
                recs2 = extract(fu2 or u, html2) if html2 else []
                if recs2:
                    recs = recs2
        except Exception as e:  # noqa: BLE001
            print(f"   ! extract error {u}: {e}")
            return False
        if recs and (not recs[0]["fee"] or recs[0]["fee_is_guess"]):
            links = recs[0].get("fee_links", [])[:2]
            if "international" not in u.lower():  # e.g. deakin.edu.au/course/x -> /course/x-international
                links.append(u.rstrip("/") + "-international")
            for link in links:
                fee = fee_cache.get(link)
                if fee is None:
                    st, _, fhtml = fetcher.get(link)
                    fee = fee_cache[link] = (fee_only(fhtml, link) if fhtml and st < 400 else 0) or 0
                if fee:
                    for r in recs:
                        r["fee"], r["fee_is_guess"] = fee, False
                        r["fee_source"] = "course's own fees page: " + link
                    break
        if recs:
            records[normalise(u)] = recs
        return bool(recs)

    workers = args.workers
    if fetcher.is_blocked(url):
        print("   site blocks plain requests (bot protection) -> using headless Chrome for every page")
        fetcher.browser_only = True
        workers = min(workers, 8)
    # university name from the homepage (course pages may carry sub-brands)
    _, _, home = fetcher.get(url)
    site_title = site_name_from_home(home)
    print(f"\n[1/3] Reading sitemaps for {dom} ...")
    sm = discover_sitemap_urls(fetcher, url)
    seeds = [u for u in sm if COURSE_HINT.search(urlparse(u).path)]
    print(f"   course-like URLs in sitemaps: {len(seeds)}")
    print(f"[2/3] Crawling site (max {args.max_pages} pages, render={args.render}) ...")
    t0 = time.time()
    crawler = Crawler(url, fetcher, on_page, max_pages=args.max_pages, workers=workers)
    try:
        crawler.run(seeds)
    except KeyboardInterrupt:
        print("\n   interrupted - keeping what was scanned so far")
    finally:
        fetcher.close()
    print(f"   done in {int(time.time() - t0)}s")
    flat = [r for recs in records.values() for r in recs]
    scan.site_title = site_title
    scan.fetched_urls = crawler.fetched_urls
    return merge_duplicates(flat)


def site_name_from_home(html):
    """University name from the homepage: og:site_name or a <title> part that names an institution,
    skipping slogans such as "UK University in Dubai"."""
    if not html:
        return ""
    hs = BeautifulSoup(html, "lxml")
    og = hs.find("meta", property="og:site_name")
    cands = [og.get("content", "").strip()] if og else []
    if hs.title and hs.title.string:
        cands += [x.strip() for x in re.split(r"\s+[|–—:-]\s+", hs.title.string)]
    cands = [c for c in cands if c]
    named = [c for c in cands if re.search(r"universit|college|institut|school|academy|polytechnic", c, re.I)]
    proper = [c for c in named if not re.match(r"(?i)(uk|top|best|leading|study|welcome|home|the best)\b", c)
              and not re.search(r"\s(in|for|near)\s", c)]
    return (proper or named or cands or [""])[0]


def _richness(r):
    return (bool(re.search(r"international", r["url"], re.I)) and bool(r["fee"]),
            not r["fee_is_guess"], bool(r["fee"]) + any(o["months"] for o in r["options"]) + any(o["duration"] for o in r["options"]),
            len(r["options"]))


def merge_duplicates(flat):
    """Same course reached via several URLs (overview/fees/entry sub-pages, aliases): keep the richest
    record and fill its gaps from the others."""
    groups = {}
    for r in flat:
        # same course name at another campus (e.g. Coventry vs London) is a separate offering
        key = (re.sub(r"\W+", " ", r["course"].lower()).strip(), "|".join(sorted(r.get("campuses") or [r.get("campus") or ""])).lower())
        groups.setdefault(key, []).append(r)
    out = []
    for recs in groups.values():
        # different fees/options on different URLs = genuinely different courses (e.g. campuses)
        by_url_fee = {}
        for r in recs:
            by_url_fee.setdefault(r.get("canonical") or r["url"], r)
        recs = sorted(by_url_fee.values(), key=_richness, reverse=True)
        best = dict(recs[0])
        for r in recs[1:]:
            if best["fee"] and r["fee"] and r["fee"] != best["fee"] and not r["fee_is_guess"] and not best["fee_is_guess"] \
                    and "international" not in (best["url"] + r["url"]).lower():
                out.append(r)  # distinct offering
                continue
            if not best["fee"] and r["fee"]:
                best["fee"], best["fee_is_guess"] = r["fee"], r["fee_is_guess"]
                best["fee_source"] = (r.get("fee_source") or "") + " (from " + r["url"] + ")"
            if not any(o["months"] for o in best["options"]) and any(o["months"] for o in r["options"]):
                best["options"] = r["options"]
            if not any(o["duration"] for o in best["options"]):
                d = next((o["duration"] for o in r["options"] if o["duration"]), None)
                if d:
                    best["options"] = [dict(o, duration=d) for o in best["options"]]
            best["campus"] = best["campus"] or r["campus"]
        out.append(best)
    return out


def uni_defaults(courses, dom, url, data, cfg):
    site = Counter(c["site_name"] for c in courses if c["site_name"]).most_common(1)
    ctry = Counter(c["country"] for c in courses if c["country"]).most_common(1)
    city = Counter(c["city"] for c in courses if c["city"]).most_common(1)
    camp = Counter(c["campus"] for c in courses if c["campus"]).most_common(1)
    tld = dom.rsplit(".", 1)[-1]
    d = {
        "name": cfg.get("name") or data.get("site_title") or (site[0][0] if site else dom.split(".")[0].upper()),
        "country": cfg.get("country") or (TLD_COUNTRY.get(tld) or (ctry[0][0] if ctry else "")),
        "url": cfg.get("url") or f"https://{urlparse(url).netloc}/",
        "campus": cfg.get("campus") or (camp[0][0] if camp else "Main Campus"),
        "city": cfg.get("city") or (city[0][0] if city else ""),
        "region": cfg.get("region", ""),
        "cost_of_living": cfg.get("cost_of_living", 0),
        "deposit_pct": cfg.get("deposit_pct", 0.34),
        "credibility": cfg.get("credibility", "Yes"),
        "application_fee": cfg.get("application_fee", 0),
        "scholarship": cfg.get("scholarship", {}),
    }
    for k, v in cfg.items():  # values typed for other formats (deposit, currency, fixed columns...)
        d.setdefault(k, v)
    return d


def option_campuses(c, o):
    """Campuses for one study option: the option's own (start-date table row), else the course's list."""
    if o.get("campus"):
        return [o["campus"]]
    opt = list(dict.fromkeys(x["campus"] for x in c["options"] if x.get("campus")))
    return opt or c.get("campuses") or ([c["campus"]] if c.get("campus") else [""])


def build_sheets(chosen, sel_levels, sel_modes, sel_intakes, incl_pt):
    """chosen: courses already filtered to the selected levels. Returns (sheets, review_rows)."""
    def usable(c):
        return [o for o in c["options"] if incl_pt or not o["part_time"]]

    lvl_rank = {l: i for i, l in enumerate(sel_levels)}
    # per-course campus only matters for multi-campus universities; otherwise use the value typed on the form
    all_campus = Counter(cp for c in chosen for o in c["options"] for cp in option_campuses(c, o) if cp)
    multi_campus = len(all_campus) > 1 and all_campus.most_common(1)[0][1] < 0.9 * sum(all_campus.values())
    sheets = []
    review = []
    for month in sel_intakes:
        rows = []
        for c in chosen:
            opts = [o for o in usable(c) if o["mode"] in sel_modes and
                    ((month == "Unknown" and not o["months"]) or month in o["months"])]
            seen = set()
            for o in sorted(opts, key=lambda o: (o["mode"] != "On campus", o["part_time"], o["placement"], o["duration"] or 0)):
                for cp in (option_campuses(c, o) if multi_campus else [""]):
                    key = (o["mode"], o["duration"], cp)
                    if key in seen:
                        continue
                    seen.add(key)
                    name = c["course"] + (" (with placement)" if o["placement"] and "placement" not in c["course"].lower() else "")
                    rows.append(dict(c, course=name, level_out=c["level"], mode=o["mode"], duration=o["duration"],
                                     campus_out=cp, part_time=o["part_time"]))
        rows.sort(key=lambda r: (lvl_rank.get(r["level"], 99), r["course"].lower(), r["campus_out"], r["duration"] or 0))
        if rows:
            sheets.append((month, rows))
        for r in rows:
            issues = [n for n, bad in [("fee missing", not r["fee"]), ("fee guessed", r["fee_is_guess"]),
                                       ("duration missing", not r["duration"]), ("intake missing", month == "Unknown")] if bad]
            if issues:
                review.append([month, r["course"], r["level"], r["campus_out"], r["url"], r["fee"] or "",
                               r.get("fee_source", ""), r["duration"] or "", "; ".join(issues)])
    return sheets, review


def save_outputs(sheets, review, uni, out_dir, template=None):
    """Write the workbook (+ review CSV). Never overwrites an existing file. Returns (xlsx_path, csv_path|None)."""
    safe = re.sub(r'[\\/:*?"<>|]+', " ", uni["name"]).strip()
    out = os.path.join(out_dir, f"{safe}.xlsx")
    if os.path.exists(out):  # never overwrite an existing (e.g. hand-made) sheet
        stamp = time.strftime("%Y-%m-%d")
        out = os.path.join(out_dir, f"{safe} - generated {stamp}.xlsx")
        n = 2
        while os.path.exists(out):
            out = os.path.join(out_dir, f"{safe} - generated {stamp} ({n}).xlsx")
            n += 1
    if template:
        import template_engine
        template_engine.render(template, sheets, uni, out)
    else:
        write_workbook(out, sheets, uni)
    rv = None
    if review:
        rv = out[:-5] + " - review.csv"
        with open(rv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Intake", "Course", "Level", "Campus", "Course Link", "Fee", "Fee source", "Duration", "Check"])
            w.writerows(review)
    return out, rv


def main():
    # `kill -USR1 <pid>` prints every thread's stack (diagnosing a stalled crawl). POSIX only - SIGUSR1
    # doesn't exist on Windows, where this tool is only ever run for local development.
    if hasattr(signal, "SIGUSR1"):
        faulthandler.register(signal.SIGUSR1, all_threads=True)
    ap = argparse.ArgumentParser(description="Scan a university website and export courses to the Flyurdream Excel format")
    ap.add_argument("url")
    ap.add_argument("--rescan", action="store_true", help="ignore cached scan results")
    ap.add_argument("--max-pages", type=int, default=8000)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--delay", type=float, default=0.0, help="seconds between requests per worker")
    ap.add_argument("--render", choices=["auto", "never", "always"], default="auto",
                    help="use headless Chrome for JavaScript pages (auto = only when a page looks empty)")
    ap.add_argument("--levels", help="comma list, skips the level question")
    ap.add_argument("--intakes", help="comma list of months, skips the intake question")
    ap.add_argument("--modes", help="comma list (On campus,Online,Blended)")
    ap.add_argument("--include-part-time", action="store_true")
    ap.add_argument("--include-closed", action="store_true", help="keep courses not open to international students")
    ap.add_argument("--yes", action="store_true", help="accept saved/default values for university details")
    ap.add_argument("--out", default=os.path.expanduser("~/Downloads"))
    args = ap.parse_args()

    url = args.url.strip()
    if not url.startswith("http"):
        url = "https://" + url
    dom = base_domain(urlparse(url).netloc)
    os.makedirs(CACHE, exist_ok=True)
    os.makedirs(CONFIGS, exist_ok=True)
    cache_file = os.path.join(CACHE, f"{dom}.json")

    if os.path.exists(cache_file) and not args.rescan:
        with open(cache_file) as f:
            data = json.load(f)
        courses = data["courses"]
        print(f"Using cached scan from {data['scanned_at']} ({len(courses)} course rows). Use --rescan to scan again.")
    else:
        courses = scan(url, args)
        with open(cache_file, "w") as f:
            json.dump({"url": url, "scanned_at": time.strftime("%Y-%m-%d %H:%M"), "site_title": scan.site_title,
                       "courses": courses, "fetched_urls": scan.fetched_urls}, f, indent=1)
        data = {"site_title": scan.site_title}

    if not courses:
        print("\nNo course pages were found. Try --render always (JavaScript site) or pass the course-search page URL.")
        return 1

    # ---------- summary + selections
    print(f"\n[3/3] Found {len(courses)} course rows on {len({c['url'] for c in courses})} course pages")
    closed = [c for c in courses if c.get("intl_closed")]
    if closed:
        keep = args.include_closed or (not args.yes and sys.stdin.isatty() and ask(
            f"\n{len(closed)} courses say they are NOT open to international students (or are apprenticeships). "
            f"Include them? (y/n)", "n").lower().startswith("y"))
        if not keep:
            courses = [c for c in courses if not c.get("intl_closed")]
            print(f"   excluded {len(closed)} home-students-only courses")
    lv = Counter(c["level"] for c in courses)
    levels = [l for l in LEVEL_ORDER if l in lv] + [l for l in lv if l not in LEVEL_ORDER]
    sel_levels = choose("EDUCATION LEVELS found:", levels, lv, args.levels)

    pt = sum(1 for c in courses if c["level"] in sel_levels and any(o["part_time"] for o in c["options"]))
    incl_pt = args.include_part_time or bool(pt and not args.yes and sys.stdin.isatty() and ask(
        f"\nInclude part-time study options? ({pt} courses have them) (y/n)", "n").lower().startswith("y"))

    def usable(c):
        return [o for o in c["options"] if incl_pt or not o["part_time"]]

    chosen = [c for c in courses if c["level"] in sel_levels]
    md = Counter(m for c in chosen for m in {o["mode"] for o in usable(c)})
    modes = sorted(md, key=lambda m: -md[m])
    sel_modes = choose("MODES OF EDUCATION found:", modes, md, args.modes) if len(modes) > 1 or args.modes else modes

    it = Counter()
    for c in chosen:
        ms = {m for o in usable(c) if o["mode"] in sel_modes for m in (o["months"] or ["Unknown"])}
        it.update(ms)
    intakes = [m for m in MONTHS if m in it] + (["Unknown"] if it.get("Unknown") else [])
    print(f"   missing: fee {sum(1 for c in chosen if not c['fee'])}, intake {it.get('Unknown', 0)}")
    sel_intakes = choose("INTAKES found:", intakes, it, args.intakes)
    if set(sel_intakes) == set(intakes):  # 'all': biggest intake first, like Sep, Jan, ...
        sel_intakes.sort(key=lambda m: (m == "Unknown", -it[m]))
    if not sel_levels or not sel_intakes:
        print("Nothing selected.")
        return 1

    # ---------- per-university fixed values (remembered per domain)
    cfg_file = os.path.join(CONFIGS, f"{dom}.json")
    cfg = json.load(open(cfg_file)) if os.path.exists(cfg_file) else {}
    defaults = uni_defaults(courses, dom, url, data, cfg)
    if args.yes or not sys.stdin.isatty():
        uni = defaults
    else:
        print("\nUNIVERSITY DETAILS (press Enter to keep the value in brackets)")
        uni = dict(defaults)
        uni["name"] = ask("   University name", defaults["name"])
        uni["country"] = ask("   Country", defaults["country"])
        uni["url"] = ask("   University link", defaults["url"])
        uni["campus"] = ask("   Campus (used when a course page doesn't name one)", defaults["campus"])
        uni["city"] = ask("   City", defaults["city"])
        uni["region"] = ask("   Region", defaults["region"])
        uni["cost_of_living"] = number(ask("   Cost of living", defaults["cost_of_living"]))
        uni["deposit_pct"] = number(ask("   Minimum deposit as fraction of fee", defaults["deposit_pct"]), 0.34)
        uni["credibility"] = ask("   Credibility interview (Yes/No)", defaults["credibility"])
        uni["application_fee"] = number(ask("   Application fee", defaults["application_fee"]))
        sch = dict(defaults["scholarship"])
        for l in sel_levels:
            sch[l] = number(ask(f"   Scholarship for {l}", sch.get(l, 0)))
        uni["scholarship"] = sch
    uni.setdefault("scholarship", {})
    with open(cfg_file, "w") as f:
        json.dump(uni, f, indent=1)

    sheets, review = build_sheets(chosen, sel_levels, sel_modes, sel_intakes, incl_pt)
    if not sheets:
        print("No courses match that selection.")
        return 1

    out, rv = save_outputs(sheets, review, uni, args.out)
    print(f"\nSaved: {out}")
    for m, rows in sheets:
        print(f"   sheet {m:<10} {len(rows)} rows")
    if rv:
        print(f"   {len(review)} rows need a manual check -> {rv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
