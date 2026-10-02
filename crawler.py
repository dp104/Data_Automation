"""Site discovery: sitemaps + prioritised same-site crawl, with optional headless-browser rendering."""
import gzip
import heapq
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# URL path words that suggest course listings / course detail pages
COURSE_HINT = re.compile(
    r"(course|courses|programme|programmes|program|programs|degree|degrees|study|studying|"
    r"undergraduate|postgraduate|graduate|masters?|bachelors?|phd|doctoral|research-degrees|"
    r"find-a-course|coursefinder|course-finder|subjects?|qualifications?|a-z|az-|diploma|certificate|"
    r"taught|mba|msc|bsc|/ma-|/ba-|llm|foundation)", re.I)

SKIP_PATH = re.compile(
    r"(/news|/events?/|/blog|/staff|/people|/profiles?/|/publications?|/library|/alumni|/jobs|/vacanc|"
    r"/careers-service|/login|/signin|/sign-in|/cart|/basket|/calendar|/press|/media-centre|/podcast|"
    r"/wp-json|/feed|/tag/|/author/|/search\?|/print/|/share|/cdn-cgi|/sso|/cas/|/shibboleth|"
    r"^/research/(?!degrees|study|phd|postgraduate|programmes|courses)|/about-us/governance|/policies|/freedom-of-information|"
    r"/whats-on|/conference|/venue-hire|/shop|/donate|/give)", re.I)

SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|svg|webp|ico|css|js|json|xml|zip|docx?|xlsx?|pptx?|mp4|mp3|mov|avi|ics|csv|txt|rss|woff2?|ttf|eot)(\?|$)", re.I)

AWARD_SLUG = re.compile(r"(^|[-_])(msc|ma|mba|llm|mres|mphil|phd|dba|bsc|ba|beng|meng|llb|bed|pgdip|pgcert|pgce|"
                        r"hons|bachelor|master|masters|diploma|certificate|foundation|degree|doctorate)([-_]|$)", re.I)
LOW_VALUE = re.compile(r"(?:^|/|-)(?:funding|scholarships?|bursar(?:y|ies)|applying|how-to-apply|apply|open-days?|visit-us|visit|"
                       r"clearing|accommodation|visas?|fees-and-finance|contextual-offers?|student-life|support|facilities|"
                       r"webinars?|guides?|faqs?|contact|agents?|countries|your-country|events?|resources)(?:/|$)", re.I)

# Other languages / locales are duplicates of the same courses
LOCALE_PATH = re.compile(r"^/(zh|cn|ar|es|fr|de|it|ja|ko|pt|ru|vi|th|id|tr|hi|ur|zh-hans|zh-hant)(/|$)", re.I)


def base_domain(host):
    host = host.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    # keep e.g. rgu.ac.uk / unimelb.edu.au / uni.edu
    if len(parts) >= 3 and parts[-2] in ("ac", "edu", "co", "com", "org", "gov", "net") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def normalise(url):
    url, _ = urldefrag(url)
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return None
    path = re.sub(r"/{2,}", "/", p.path) or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    # drop tracking query strings, keep paging/filters that may be listings
    q = "&".join(x for x in p.query.split("&") if x and not re.match(r"(utm_|fbclid|gclid|_ga|mc_|hsa_)", x))
    return f"https://{p.netloc.lower()}{path}" + (f"?{q}" if q else "")


BLOCK_RE = re.compile(r"just a moment|cf-browser-verification|challenge-platform|attention required|access denied|"
                      r"request unsuccessful|incapsula|captcha|are you a robot|pardon our interruption", re.I)


class Fetcher:
    """HTTP fetcher that falls back to headless Chrome for JavaScript pages and bot-protected sites.
    Each worker thread gets its own browser, so rendering runs in parallel."""

    def __init__(self, timeout=15, delay=0.0, render="auto", log=print):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9",
                               "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
        adapter = requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=1)
        self.s.mount("http://", adapter)
        self.s.mount("https://", adapter)
        self.timeout = timeout
        self.delay = delay
        self.render_mode = render
        self.browser_only = render == "always"
        self.log = log
        self._lock = threading.Lock()
        self._loop = None

    # ---- plain HTTP
    def http_get(self, url):
        try:
            if self.delay:
                time.sleep(self.delay)
            r = self.s.get(url, timeout=self.timeout, allow_redirects=True)
            ctype = r.headers.get("content-type", "")
            if r.status_code >= 400:
                return r.status_code, r.url, "", bool(BLOCK_RE.search(r.text[:3000]))
            if not re.search(r"html|xml|text/plain", ctype) and not url.endswith(".gz"):
                return r.status_code, r.url, "", False
            if url.endswith(".gz") or r.content[:2] == b"\x1f\x8b":
                try:
                    return r.status_code, r.url, gzip.decompress(r.content).decode("utf-8", "ignore"), False
                except OSError:
                    pass
            if "charset" not in ctype.lower():  # undeclared encoding: requests assumes Latin-1 ("Â£"); detect instead
                m = re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', r.content[:4000], re.I)
                r.encoding = m.group(1).decode() if m else "utf-8"
            return r.status_code, r.url, r.text, bool(len(r.text) < 20000 and BLOCK_RE.search(r.text[:5000]))
        except requests.RequestException:
            return 0, url, "", False

    def is_blocked(self, url):
        status, _, _, blocked = self.http_get(url)
        return blocked or status in (401, 403, 429, 503)

    def polite_get(self, url, raw=False):
        """get() for project crawls: honours the site's pace. On 429/403 it backs off and slows down instead of
        pushing on; after repeated refusals it gives up on that page (the run reports it)."""
        st, fu, txt = self.get(url, raw=raw)
        if st in (429, 403) and not self.browser_only:
            with self._lock:
                self.refusals = getattr(self, "refusals", 0) + 1
                self.delay = min(max(self.delay * 2, 1.0), 10.0)
            time.sleep(self.delay * 3)
            st, fu, txt = self.get(url, raw=raw)
        return st, fu, txt

    def get(self, url, raw=False):
        """Returns (status, final_url, text). raw=True for robots/sitemaps (no JS rendering)."""
        if self.browser_only:
            st, fu, html = self.browser_get(url, raw=raw)
            if not raw and html and self.needs_render(html):
                st, fu, html = self.browser_get(url, spa=True)
            return st, fu, html
        status, final, text, blocked = self.http_get(url)
        if blocked:
            return self.browser_get(url, raw=raw)
        if not raw and text and self.needs_render(text):
            st2, f2, t2 = self.browser_get(url, spa=True)
            if t2:
                return st2, f2, t2
        return status, final, text

    def needs_render(self, html):
        if self.render_mode == "never":
            return False
        if self.render_mode == "always":
            return True
        soup = BeautifulSoup(html, "lxml")
        for t in soup(["script", "style", "noscript"]):
            t.decompose()
        text = soup.get_text(" ", strip=True)
        links = len(soup.find_all("a", href=True))
        spa = re.search(r'id=["\'](?:root|app|__next|__nuxt|main-app)["\']|ng-version|data-reactroot', html)
        return len(text) < 300 or (spa is not None and (len(text) < 1500 or links < 10))

    # ---- headless browser
    TRACKERS = re.compile(r"google-analytics|googletagmanager|doubleclick|facebook|hotjar|clarity\.ms|linkedin|"
                          r"tiktok|twitter|snapchat|pinterest|youtube|vimeo|hubspot|intercom|livechat|zopim|"
                          r"cookiebot|onetrust|trustarc|qualtrics|adservice|adsystem|bing\.com", re.I)
    TABS = 6  # pages rendered in parallel

    # One browser on one dedicated thread (async Playwright); any worker thread can submit pages to it.
    def _start_browser(self):
        with self._lock:
            if getattr(self, "_loop", None) is not None:
                return
            import asyncio
            ready = threading.Event()
            self._loop = asyncio.new_event_loop()

            def runner():
                asyncio.set_event_loop(self._loop)
                self._loop.run_until_complete(self._launch())
                ready.set()
                self._loop.run_forever()

            self._thread = threading.Thread(target=runner, daemon=True)
            self._thread.start()
            ready.wait(90)

    async def _launch(self):
        import asyncio
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        await self._new_browser()
        self._sem = asyncio.Semaphore(self.TABS)

    async def _new_browser(self):
        self._browser = await self._pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        self._ctxt = await self._browser.new_context(user_agent=UA, locale="en-GB", viewport={"width": 1366, "height": 900})

        async def route(r):
            req = r.request
            if req.resource_type in ("image", "media", "font") or self.TRACKERS.search(req.url):
                await r.abort()
            else:
                await r.continue_()
        await self._ctxt.route("**/*", route)

    async def _render(self, url, raw, spa):
        async with self._sem:
            if not self._browser.is_connected():  # crashed -> relaunch
                await self._new_browser()
            page = await self._ctxt.new_page()
            page.set_default_timeout(30000)
            page.on("dialog", lambda d: None)
            try:
                resp = await page.goto(url, timeout=45000, wait_until="domcontentloaded")
                for _ in range(12):  # bot challenge pages reload themselves once solved
                    if not BLOCK_RE.search(await page.title() or ""):
                        break
                    await page.wait_for_timeout(1000)
                status = resp.status if resp else 0
                if raw:
                    try:
                        body = await resp.text() if resp else ""
                    except Exception:  # noqa: BLE001
                        body = ""
                    if BLOCK_RE.search(body[:3000]) or not body:
                        body = await page.evaluate("() => document.documentElement.textContent || ''")
                    return status, page.url, body
                if spa:
                    try:
                        await page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:  # noqa: BLE001
                        pass
                await page.evaluate("() => { document.querySelectorAll('details').forEach(d => d.open = true); }")
                if spa:
                    await page.mouse.wheel(0, 20000)
                    await page.wait_for_timeout(600)
                return status, page.url, await page.content()
            finally:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass

    def browser_get(self, url, raw=False, spa=False):
        if self.render_mode == "never" and not self.browser_only:
            return 0, url, ""
        import asyncio
        try:
            self._start_browser()
            fut = asyncio.run_coroutine_threadsafe(self._render(url, raw, spa), self._loop)
            return fut.result(timeout=100)  # hard cap: a stuck page can never stall the crawl
        except Exception as e:  # noqa: BLE001
            try:
                fut.cancel()
            except Exception:  # noqa: BLE001
                pass
            if "Executable doesn't exist" in str(e):
                self.log(f"   browser unavailable: {e}")
            return 0, url, ""

    def close(self):
        if getattr(self, "_loop", None) is None:
            return
        import asyncio

        async def shutdown():
            try:
                await self._browser.close()
                await self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
        try:
            asyncio.run_coroutine_threadsafe(shutdown(), self._loop).result(timeout=20)
        except Exception:  # noqa: BLE001
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)


def discover_sitemap_urls(fetcher, root, log=print):
    p = urlparse(root)
    origin = f"{p.scheme}://{p.netloc}"
    candidates = []
    _, _, robots = fetcher.get(origin + "/robots.txt", raw=True)
    for line in robots.splitlines():
        if line.lower().startswith("sitemap:"):
            candidates.append(line.split(":", 1)[1].strip())
    candidates += [origin + "/sitemap.xml", origin + "/sitemap_index.xml", origin + "/sitemap-index.xml",
                   origin + "/sitemap/sitemap.xml", origin + "/course-sitemap.xml"]
    seen, urls = set(), set()
    queue = list(dict.fromkeys(candidates))
    while queue and len(seen) < 400:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        _, _, xml = fetcher.get(sm, raw=True)
        if not xml or "<loc" not in xml:
            continue
        locs = [x.strip() for x in re.findall(r"<loc>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</loc>", xml, re.S)]
        if "<sitemapindex" in xml:
            # prioritise course-ish child sitemaps, but read all of them
            locs.sort(key=lambda u: 0 if COURSE_HINT.search(u) else 1)
            queue.extend(locs)
        else:
            urls.update(l.replace("&amp;", "&") for l in locs)
    log(f"   sitemaps read: {len(seen)}, URLs listed: {len(urls)}")
    return urls


def same_host(url, root_host):
    """Treat example.ac.uk and www.example.ac.uk as one site so pages aren't crawled twice."""
    p = urlparse(url)
    h = p.netloc.lower()
    if h != root_host and h.removeprefix("www.") == root_host.removeprefix("www.") and (h.startswith("www.") or root_host.startswith("www.")):
        return url.replace(p.netloc, root_host, 1)
    return url


class Crawler:
    """Priority crawl: course-looking URLs first; general pages only near the top of the site."""

    def __init__(self, root, fetcher, on_page, max_pages=4000, workers=10, log=print):
        self.root = normalise(root)
        self.root_host = urlparse(self.root).netloc
        self.dom = base_domain(self.root_host)
        self.fetched_urls = []
        self.fetcher = fetcher
        self.on_page = on_page  # callback(url, html) -> bool (is course page)
        self.max_pages = max_pages
        self.workers = workers
        self.log = log
        self.seen = set()
        self.heap = []
        self.fetched = 0
        self._n = 0

    def in_scope(self, url):
        p = urlparse(url)
        host = p.netloc.lower()
        if not (host == self.dom or host.endswith("." + self.dom)):
            return False
        if SKIP_EXT.search(p.path) or SKIP_PATH.search(p.path + ("?" + p.query if p.query else "")):
            return False
        if LOCALE_PATH.match(p.path):
            return False
        return True

    def priority(self, url, depth):
        p = urlparse(url)
        path = p.path.lower()
        score = 10
        if COURSE_HINT.search(path):
            score = 1
            last = path.rstrip("/").rsplit("/", 1)[-1]
            if AWARD_SLUG.search(last) or re.search(r"/(courses?|programmes?|programs?)/[^/]+(/[^/]+)?$", path):
                score = 0  # looks like a course detail page
            if LOW_VALUE.search(path):
                score = 6
        elif depth > 2:
            return None  # don't wander deep into unrelated sections
        if p.query:
            score += 2
        if urlparse(self.root).netloc != p.netloc:
            score += 1
        return score + depth * 0.1

    def push(self, url, depth):
        url = normalise(same_host(url, self.root_host)) if url else None
        if not url or url in self.seen or not self.in_scope(url):
            return
        pr = self.priority(url, depth)
        if pr is None:
            return
        self.seen.add(url)
        self._n += 1
        heapq.heappush(self.heap, (pr, self._n, url, depth))

    def links(self, base, html):
        soup = BeautifulSoup(html, "lxml")
        out = []
        for a in soup.find_all("a", href=True):
            h = a["href"].strip()
            if h.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            out.append(urljoin(base, h))
        return out

    def run(self, seeds):
        self.push(self.root, 0)
        for s in seeds:
            self.push(s, 1)
        course_pages = 0
        failed = 0
        last_course_at = 0
        last_log = time.time()
        inflight = {}
        submitted = 0
        with ThreadPoolExecutor(self.workers) as ex:
            while True:
                # keep the pool busy; a slow page never blocks the others
                while self.heap and len(inflight) < self.workers * 2 and submitted < self.max_pages:
                    if self.fetched - last_course_at > 300 and self.heap[0][0] >= 1:
                        break
                    _, _, u, d = heapq.heappop(self.heap)
                    inflight[ex.submit(self.fetcher.get, u)] = (u, d)
                    submitted += 1
                if not inflight:
                    if self.heap and self.fetched - last_course_at > 300:
                        self.log("   no new course pages in the last 300 pages - stopping early")
                    break
                done, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
                for f in done:
                    u, d = inflight.pop(f)
                    _, final, html = f.result()
                    self.fetched += 1
                    self.fetched_urls.append(u)
                    if not html:
                        failed += 1
                        last_course_at += 1  # failed fetches don't count towards the early stop
                        continue
                    final = normalise(final) or u
                    self.seen.add(final)
                    if self.on_page(final, html):
                        course_pages += 1
                        last_course_at = self.fetched
                    for l in self.links(final, html):
                        self.push(l, d + 1)
                if time.time() - last_log > 10:
                    last_log = time.time()
                    self.log(f"   crawled {self.fetched} pages | queue {len(self.heap)} | course pages {course_pages}")
        self.log(f"   crawl finished: {self.fetched} pages fetched ({failed} failed), {course_pages} course pages found")


GENERIC_SKIP = re.compile(r"(/login|/signin|/sign-in|/logout|/register|/cart|/basket|/checkout|/account|/my-account|/wishlist|"
                          r"/apply/?$|/application/?$|/print/?$|/share/?$|/compare/?$|/reviews/write|"
                          r"/cdn-cgi|/wp-json|/feed|/xmlrpc|/print/|/share|mailto:|tel:)", re.I)


class GenericCrawler(Crawler):
    """Crawl any site for pages that look like the user's example pages (learned URL pattern).
    Listing pages that lead to them (same first path segment, pagination) are followed first."""

    def __init__(self, root, fetcher, on_page, pattern, max_pages=3000, workers=8, log=print):
        super().__init__(root, fetcher, on_page, max_pages=max_pages, workers=workers, log=log)
        self.pattern = re.compile(pattern)
        first = re.match(r"\^/([^/\[\\(]+)", pattern)
        self.section = "/" + first.group(1).replace("\\", "") if first else None

    def in_scope(self, url):
        p = urlparse(url)
        host = p.netloc.lower()
        if not (host == self.dom or host.endswith("." + self.dom)):
            return False
        return not (SKIP_EXT.search(p.path) or GENERIC_SKIP.search(p.path) or LOCALE_PATH.match(p.path))

    def priority(self, url, depth):
        p = urlparse(url)
        if self.pattern.search(p.path):
            return 0 + depth * 0.01            # a page like the examples
        if self.section and p.path.startswith(self.section):
            return 1 + depth * 0.01            # same section: listings, categories
        if re.search(r"[?&](page|p|pg|start|offset)=\d+|/page/\d+", url):
            return 1.5                          # pagination
        if depth > 3:
            return None
        return 5 + depth


def load_robots(fetcher, root):
    """The site's robots.txt rules (None = no file / unreadable = everything allowed)."""
    import urllib.robotparser
    p = urlparse(root)
    st, _, txt = fetcher.get(f"{p.scheme}://{p.netloc}/robots.txt", raw=True)
    if not txt or st >= 400 or "<html" in txt[:200].lower():
        return None
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(txt.splitlines())
    return rp


class _PoliteFetch:
    def __init__(self, fetcher):
        self.f = fetcher

    def get(self, url, raw=False):
        return self.f.polite_get(url, raw=raw)

    def __getattr__(self, k):
        return getattr(self.f, k)


class TypeCrawler(Crawler):
    """Crawl for one project type: listing pages -> result cards -> each item's own page.
    Honours robots.txt; prefers links that look like items already found and 'next page' links."""

    def __init__(self, root, fetcher, on_page, max_pages=2000, workers=4, log=print, robots=None):
        super().__init__(root, _PoliteFetch(fetcher), on_page, max_pages=max_pages, workers=workers, log=log)
        self.robots = robots
        try:
            cd = robots.crawl_delay("*") if robots is not None else None
        except Exception:  # noqa: BLE001
            cd = None
        fetcher.delay = max(fetcher.delay, float(cd) if cd else 0.25)  # the site's Crawl-delay, else a short pause
        # a start link deeper than the homepage = only that section (+ the items and next pages it links to)
        sp = urlparse(self.root).path
        self.section = sp.rsplit("/", 1)[0] + "/" if sp.strip("/") else None
        self.boost = {}          # url -> priority (card links, next pages)
        self.item_dirs = {}      # "/products/" -> count of items found under it
        self.blocked = 0

    def allowed(self, url):
        if self.robots is None:
            return True
        try:
            return self.robots.can_fetch("*", url)
        except Exception:  # noqa: BLE001
            return True

    def in_scope(self, url):
        p = urlparse(url)
        host = p.netloc.lower()
        if not (host == self.dom or host.endswith("." + self.dom)):
            return False
        if SKIP_EXT.search(p.path) or GENERIC_SKIP.search(p.path) or LOCALE_PATH.match(p.path):
            return False
        if not self.allowed(url):
            self.blocked += 1
            return False
        return True

    def note_item(self, url):
        d = urlparse(url).path.rsplit("/", 1)[0] + "/"
        self.item_dirs[d] = self.item_dirs.get(d, 0) + 1

    def priority(self, url, depth):
        n = normalise(url)
        if n in self.boost:
            return self.boost[n]
        path = urlparse(url).path
        d = path.rsplit("/", 1)[0] + "/"
        if self.section and not path.startswith(self.section):
            return None                      # outside the chosen section (items/next pages come in via boost)
        if self.item_dirs.get(d, 0) >= 2:
            return 0.2                       # looks like the items found so far
        if re.search(r"[?&](page|p|pg|start|offset)=\d+|/page/\d+", url):
            return 0.8                       # pagination
        if depth > 3:
            return None
        return 3 + depth

    def push_boost(self, url, prio, depth):
        n = normalise(same_host(url, self.root_host))
        if not n:
            return
        self.boost[n] = prio
        if n in self.seen:
            return
        self.push(n, depth)
