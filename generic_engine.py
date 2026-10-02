""""Any website" mode: learn where each column of the user's sample sheet sits on a site's pages,
from one or two example pages, then read the same places on every similar page of that site.

No APIs and no AI. Sources, most robust first:
  jsonld   schema.org data embedded in the page (Product.offers.price, JobPosting.title, ...)
  meta     <meta property="og:title"> and similar
  label    the value that follows a label on the page ("Price" -> "£20.00")
  css      a CSS selector built from stable ids / itemprop / class names
  page_url the page address itself
Values are only ever read from the page; a column with no source stays blank.
"""
import json
import os
import re
from difflib import SequenceMatcher
from urllib.parse import urlparse

from bs4 import BeautifulSoup, NavigableString, Tag

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILES = os.path.join(HERE, "profiles")

SKIP_TAGS = {"script", "style", "noscript", "svg", "iframe", "template", "head"}
NUM_RE = re.compile(r"-?\d{1,3}(?:[,\s ]\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?")


# ---------------------------------------------------------------- text helpers
def clean(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def low(s):
    return clean(s).lower()


def number(s):
    """First number in a string: '£1,234.50' -> 1234.5"""
    m = NUM_RE.search(str(s or "").replace(" ", " "))
    if not m:
        return None
    v = re.sub(r"[,\s ]", "", m.group(0))
    try:
        f = float(v)
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def is_numberish(v):
    return isinstance(v, (int, float)) or bool(re.fullmatch(r"\s*[-+]?[\d,.\s]+\s*", str(v or ""))) and number(v) is not None


def same_value(sample, found):
    """Does text found on the page equal the sample cell?"""
    a, b = low(sample), low(found)
    if not a or not b:
        return False
    if a == b:
        return True
    if a.startswith("http") and b.startswith("http"):  # https://x/page == https://x/page/
        return a.rstrip("/").replace("http://", "https://") == b.rstrip("/").replace("http://", "https://")
    if is_numberish(sample):
        nb = number(found)
        return nb is not None and abs(float(number(sample)) - float(nb)) < 0.005 and len(b) < 40
    return False


def similarity(a, b):
    return SequenceMatcher(None, low(a), low(b)).ratio()


# ---------------------------------------------------------------- page structure
def own_text(el):
    return clean(" ".join(t for t in el.find_all(string=True, recursive=False) if isinstance(t, NavigableString)))


def full_text(el):
    return clean(el.get_text(" ", strip=True))


DYNAMIC_CLASS = re.compile(r"\d{3,}|^(css|sc|jsx|tw)-|__[a-z0-9]{5,}$|^[a-z]{1,2}\d|active|selected|hover|open|show|hidden")


def css_path(el, max_depth=5):
    """Short CSS selector for an element, anchored on itemprop / id / meaningful classes."""
    parts = []
    node = el
    for _ in range(max_depth):
        if not isinstance(node, Tag) or node.name in ("html", "body", "[document]"):
            break
        if node.get("itemprop"):
            parts.append(f'{node.name}[itemprop="{node["itemprop"]}"]')
            break
        nid = node.get("id")
        if nid and not re.search(r"\d{3,}", nid) and re.fullmatch(r"[A-Za-z][\w-]*", nid):
            parts.append(f"{node.name}#{nid}")
            break
        classes = [c for c in node.get("class", []) if re.fullmatch(r"[A-Za-z_][\w-]*", c) and not DYNAMIC_CLASS.search(c)]
        sel = node.name + "".join("." + c for c in classes[:2])
        parent = node.parent
        if isinstance(parent, Tag) and not classes:
            same = [s for s in parent.find_all(node.name, recursive=False)]
            if len(same) > 1:
                sel += f":nth-of-type({same.index(node) + 1})"
        parts.append(sel)
        node = node.parent
    return " > ".join(reversed(parts))


def leaf_elements(soup):
    """Elements that directly hold visible text."""
    out = []
    for el in soup.find_all(True):
        if el.name in SKIP_TAGS or any(p.name in SKIP_TAGS for p in el.parents if isinstance(p, Tag)):
            continue
        t = own_text(el)
        if t:
            out.append(el)
    return out


def label_for(el):
    """A short label right before the value: previous sibling, dt/th, or the parent's first text.
    Returns (label text, relation, label element)."""
    prev = el.find_previous_sibling()
    if prev is not None and 0 < len(full_text(prev)) <= 40:
        return full_text(prev), "prev_sibling", prev
    if el.name in ("dd", "td"):
        p = el.find_previous_sibling(["dt", "th"])
        if p is not None:
            return full_text(p), "prev_sibling", p
    par = el.parent
    if isinstance(par, Tag):
        pprev = par.find_previous_sibling()
        if pprev is not None and 0 < len(full_text(pprev)) <= 40 and len(full_text(par)) <= 200:
            return full_text(pprev), "parent_prev", pprev
    return None, None, None


# ---------------------------------------------------------------- page data (json-ld / meta)
def jsonld_flat(soup):
    """{'Product.offers.price': '21.99', ...} from schema.org JSON-LD."""
    out = {}

    def walk(o, prefix):
        if isinstance(o, dict):
            t = o.get("@type")
            t = t[0] if isinstance(t, list) and t else t
            base = prefix or (str(t) if t else "Thing")
            for k, v in o.items():
                if k.startswith("@"):
                    continue
                walk(v, f"{base}.{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o[:5]):
                walk(v, prefix if i == 0 else f"{prefix}[{i}]")
        elif o not in (None, ""):
            out.setdefault(prefix, clean(o))

    for s in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        try:
            data = json.loads(s.string or s.get_text() or "")
        except ValueError:
            continue
        items = data.get("@graph", [data]) if isinstance(data, dict) else data
        for it in items if isinstance(items, list) else [items]:
            walk(it, "")
    return out


def meta_flat(soup):
    out = {}
    for m in soup.find_all("meta"):
        k = m.get("property") or m.get("name") or m.get("itemprop")
        v = m.get("content")
        if k and v and not k.lower().startswith(("viewport", "robots", "google", "msapplication", "theme-color", "format-detection")):
            out.setdefault(k, clean(v))
    if soup.title and soup.title.string:
        out.setdefault("title", clean(soup.title.string))
    return out


# ---------------------------------------------------------------- rules
def apply_rule(rule, soup, url):
    """Read one value from a page with a learned rule. Returns text or None."""
    t = rule["type"]
    if t == "page_url":
        # the page's official address (no "?term=..." variations) when it declares one on this site
        can = soup.find("link", rel="canonical")
        href = can.get("href", "") if can else ""
        if href.startswith("http") and urlparse(href).netloc.lower() == urlparse(url).netloc.lower():
            return href.replace("http://", "https://", 1) if url.startswith("https://") else href
        return url
    if t == "jsonld":
        return jsonld_flat(soup).get(rule["path"])
    if t == "meta":
        return meta_flat(soup).get(rule["key"])
    if t == "css":
        try:
            els = soup.select(rule["selector"])
        except Exception:  # noqa: BLE001
            return None
        if not els:
            return None
        el = els[min(rule.get("index", 0), len(els) - 1)]
        v = el.get(rule["attr"]) if rule.get("attr") else full_text(el)
        if rule.get("numeric") and number(v) is None:
            return None
        return clean(v) or None
    if t == "label":
        want = low(rule["label"])
        for el in soup.find_all(rule.get("ltag") or True):
            if el.name in SKIP_TAGS or len(full_text(el)) > 60 or low(full_text(el)) != want:
                continue
            nxt = el.find_next_sibling(rule.get("vtag")) if rule["relation"] == "prev_sibling" and rule.get("vtag") \
                else el.find_next_sibling()
            if nxt is None or not full_text(nxt):
                continue
            if rule.get("numeric") and number(full_text(nxt)) is None:
                continue  # e.g. a menu link "International" followed by "Careers"
            return full_text(nxt)
        # "Label: value" written inside one element (also the fallback for other relations)
        pat = re.compile(rf"^\s*{re.escape(rule['label'])}\s*[:\-]\s*(.+)$", re.I)
        for el in leaf_elements(soup):
            m = pat.match(full_text(el))
            if m and len(m.group(1)) < 300:
                return clean(m.group(1))
        return None
    return None


def transform(value, sample):
    """Write the value the way the sample column is written (numbers as numbers)."""
    if value in (None, ""):
        return None
    if is_numberish(sample):
        n = number(value)
        return n if n is not None else None
    v = clean(value)
    return v[:1000]


RULE_RANK = {"jsonld": 0, "meta": 1, "label": 2, "css": 3, "page_url": 0}

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
          "december"]
DUR_HEADER = re.compile(r"duration|length|months?|years?|weeks?", re.I)
DUR_TEXT = re.compile(r"(\d+(?:\.\d+)?|one|two|three|four|five|six)\s*(years?|yrs?|months?|weeks?)", re.I)
WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}


def duration_months(text):
    m = DUR_TEXT.search(str(text or ""))
    if not m:
        return None
    n = WORDS.get(m.group(1).lower()) or float(m.group(1))
    unit = m.group(2).lower()
    months = n * 12 if unit.startswith(("y", "yr")) else (n / 4.345 if unit.startswith("w") else n)
    return int(round(months))


def month_of(text):
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", str(text or ""), re.I)
    return MONTHS[["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"].index(m.group(1).lower())] if m else None


def convert(rule, value, sample):
    """Rule-level conversions learned from the sample: '1 year' -> 12 (months), 'January' -> 'Jan'."""
    conv = rule.get("convert")
    if not conv or value in (None, ""):
        return value
    if conv == "months":
        return duration_months(value)
    if conv == "years":
        mth = duration_months(value)
        return None if mth is None else (mth // 12 if mth % 12 == 0 else round(mth / 12, 1))
    if conv in ("month_short", "month_full"):
        mo = month_of(value)
        return None if not mo else (mo[:3].title() if conv == "month_short" else mo.title())
    return value


def candidates_for(sample, soup, url):
    """All rules that read exactly this sample value from this page, best first."""
    out = []
    if not clean(sample):
        return out
    if low(sample).rstrip("/") == low(url).rstrip("/"):
        out.append({"type": "page_url", "desc": "The page address"})
    for path, v in jsonld_flat(soup).items():
        if same_value(sample, v):
            out.append({"type": "jsonld", "path": path, "desc": f"Page data: {path}"})
    for key, v in meta_flat(soup).items():
        if same_value(sample, v):
            out.append({"type": "meta", "key": key, "desc": f"Page tag: {key}"})
    for el in leaf_elements(soup):
        txt = full_text(el)
        im0 = re.match(r"^\s*([^:]{2,40}?)\s*:\s*(.+)$", txt) if len(txt) <= 120 else None
        if not same_value(sample, txt) and not same_value(sample, own_text(el)) and not (im0 and same_value(sample, im0.group(2))):
            continue
        exact = low(sample) in (low(txt), low(own_text(el)))
        im = re.match(r"^\s*([^:]{2,40}?)\s*:\s*(.+)$", txt)
        if im and same_value(sample, im.group(2)):  # "Study level: Undergraduate" written in one element
            out.append({"type": "label", "label": clean(im.group(1)), "relation": "inline", "vtag": el.name,
                        "desc": f'After "{clean(im.group(1))}:"', "_exact": True})
            continue
        lab, rel, lel = label_for(el)
        if lab and not same_value(sample, lab) and len(lab) <= 40:
            out.append({"type": "label", "label": lab, "relation": rel, "vtag": el.name, "ltag": getattr(lel, "name", None), "desc": f'Next to the label "{lab}"', "_exact": exact})
        if el.name in ("h1", "h2") and len(soup.find_all(el.name)) == 1:  # the page heading beats breadcrumbs etc.
            out.append({"type": "css", "selector": el.name, "index": 0, "desc": "Main heading" if el.name == "h1" else "Sub heading",
                        "_exact": exact, "_heading": True})
            continue
        sel = css_path(el)
        if sel:
            try:
                idx = soup.select(sel).index(el)
            except (ValueError, Exception):  # noqa: BLE001
                idx = 0
            out.append({"type": "css", "selector": sel, "index": idx, "desc": f"Page element {sel}", "_exact": exact})
    # unique, best first
    seen, uniq = set(), []
    # exact text matches first (so "0 reviews" beats "Tax £0.00"), then the more robust source types
    for c in sorted(out, key=lambda c: (not c.get("_exact", True), not c.get("_heading", False), RULE_RANK.get(c["type"], 9))):
        k = json.dumps({k: v for k, v in c.items() if k != "desc" and not k.startswith("_")}, sort_keys=True)
        if k not in seen:
            seen.add(k)
            uniq.append({k: v for k, v in c.items() if not k.startswith("_")})
    return uniq


def close_candidates(sample, header, soup, url):
    """Nothing on the page equals the sample. Look for the same thing written differently:
    a duration in other units, a month spelled out, or very similar text (a renamed course/product)."""
    out = []
    if not clean(sample):
        return out
    if clean(sample).startswith("http") and urlparse(clean(sample)).netloc.lower() == urlparse(url).netloc.lower():
        # an old link to this item (the page has moved): the column is the page address
        return [{"type": "page_url", "desc": "The page address (your sample link has changed)", "_score": 0.9}]
    is_num = is_numberish(sample)
    money_col = re.search(r"fee|price|cost|tuition|salary|amount|deposit|pay|rent|\$|£|€", header, re.I)
    if is_num and DUR_HEADER.search(header) and not money_col:
        want = float(number(sample))
        for el in leaf_elements(soup):
            txt = full_text(el)
            if len(txt) > 120:
                continue
            mth = duration_months(txt)
            if mth is None:
                continue
            for conv, val in (("months", mth), ("years", mth / 12)):
                if abs(val - want) < 0.01:
                    lab, rel, lel = label_for(el)
                    if lab and len(lab) <= 40:
                        out.append({"type": "label", "label": lab, "relation": rel, "vtag": el.name, "ltag": getattr(lel, "name", None), "convert": conv,
                                    "desc": f'"{lab}" ({txt[:30]}) as {conv}', "_score": 0.9})
                    out.append({"type": "css", "selector": css_path(el), "index": 0, "convert": conv,
                                "desc": f'"{txt[:40]}" as {conv}', "_score": 0.8})
        return sorted(out, key=lambda c: -c["_score"])[:3]
    if month_of(sample) and len(clean(sample)) <= 12:
        conv = "month_short" if len(clean(sample)) <= 4 else "month_full"
        for el in leaf_elements(soup):
            txt = full_text(el)
            if len(txt) <= 40 and month_of(txt) == month_of(sample):
                lab, rel, lel = label_for(el)
                if lab and len(lab) <= 40:
                    out.append({"type": "label", "label": lab, "relation": rel, "vtag": el.name, "ltag": getattr(lel, "name", None), "convert": conv,
                                "desc": f'"{lab}" ({txt[:25]})', "_score": 0.85})
        return out[:3]
    if is_num and float(number(sample)) >= 100:
        # a price / fee that has changed since the sample was made: nearest amount within 25%, next to a label
        want = float(number(sample))
        near = []
        for el in leaf_elements(soup):
            txt = full_text(el)
            if len(txt) > 160:  # fee cells can list several awards: "£19,500 MSc £21,400 MSc with placement"
                continue
            n = number(txt)
            if n is None or n == want or not (0.75 * want <= float(n) <= 1.25 * want):
                continue
            lab, rel, lel = label_for(el)
            if lab and len(lab) <= 40:
                near.append((abs(float(n) - want) / want, lab, rel, txt, el.name, getattr(lel, "name", None)))
        for diff, lab, rel, txt, vtag, ltag in sorted(near, key=lambda x: x[0])[:2]:
            out.append({"type": "label", "label": lab, "relation": rel, "vtag": vtag, "ltag": ltag, "numeric": True,
                        "_score": 0.7 - diff,
                        "desc": f'Next to "{lab}" (page shows {txt[:25]}, your sample {clean(sample)})'})
        return out
    if not is_num and len(clean(sample)) >= 4:
        h1 = soup.find("h1")
        best = []
        for el in leaf_elements(soup):
            txt = full_text(el)
            if not (0.5 * len(sample) <= len(txt) <= 2 * len(sample) + 10):
                continue
            sc = similarity(sample, txt)
            if sc >= 0.6:
                is_h1 = h1 is not None and el is h1
                best.append((sc + (0.15 if is_h1 else 0), el, txt, is_h1))
        for sc, el, txt, is_h1 in sorted(best, key=lambda x: -x[0])[:2]:
            if is_h1:
                out.append({"type": "css", "selector": "h1", "index": 0, "desc": f'Main heading ("{txt[:40]}")', "_score": sc})
            else:
                lab, rel, lel = label_for(el)
                if lab and len(lab) <= 40:
                    out.append({"type": "label", "label": lab, "relation": rel, "vtag": el.name, "ltag": getattr(lel, "name", None), "desc": f'"{lab}" ("{txt[:40]}")', "_score": sc})
                else:
                    out.append({"type": "css", "selector": css_path(el), "index": 0, "desc": f'"{txt[:40]}"', "_score": sc})
    return out


MEANS = [("city", r"locality|town|city"), ("country", r"country"), ("region", r"region|state|county|province"),
         ("level", r"level|qualification|award"), ("price", r"price|cost|fee"), ("company", r"organi[sz]ation|employer|brand"),
         ("location", r"location|address|place|campus"), ("date", r"date|posted|published")]


def same_meaning(header, name):
    h, n = low(header), low(name)
    return any(re.search(word, h) and re.search(pat, n) for word, pat in MEANS)


UNI_HINTS = [r"course|programme|program", r"level|degree", r"intake|start", r"duration|length", r"fee|tuition",
             r"campus", r"scholarship", r"universit|institution", r"deposit|cost of living|credib"]


def looks_university(template):
    hs = " | ".join(c["header"].lower() for c in template["columns"])
    return sum(1 for p in UNI_HINTS if re.search(p, hs)) >= 5


def detected_fields(soup, url):
    """Everything the page offers that a column could be filled from (for the 'pick from page' menu)."""
    out = [{"rule": {"type": "page_url"}, "label": "Page address", "value": url}]
    for path, v in list(jsonld_flat(soup).items())[:80]:
        out.append({"rule": {"type": "jsonld", "path": path}, "label": f"Page data: {path}", "value": v[:120]})
    for key, v in list(meta_flat(soup).items())[:30]:
        out.append({"rule": {"type": "meta", "key": key}, "label": f"Page tag: {key}", "value": v[:120]})
    h1 = soup.find("h1")
    if h1 and full_text(h1):
        out.append({"rule": {"type": "css", "selector": "h1", "index": 0}, "label": "Main heading", "value": full_text(h1)[:120]})
    seen = set()
    for el in leaf_elements(soup):
        lab, rel, lel = label_for(el)
        val = full_text(el)
        if lab and 0 < len(lab) <= 40 and 0 < len(val) <= 200 and low(lab) != low(val) and low(lab) not in seen:
            seen.add(low(lab))
            out.append({"rule": {"type": "label", "label": lab, "relation": rel, "vtag": el.name, "ltag": getattr(lel, "name", None)}, "label": f'"{lab}"', "value": val[:120]})
        if len(out) > 220:
            break
    return out


# ---------------------------------------------------------------- learning
def url_pattern(urls):
    """Regex for 'pages like these' from the example addresses."""
    paths = [[s for s in urlparse(u).path.split("/") if s] for u in urls]
    if not paths:
        return ".*"
    n = max(len(p) for p in paths)
    same_len = all(len(p) == n for p in paths)
    segs = []
    for i in range(n):
        vals = {p[i] if i < len(p) else None for p in paths}
        v = next(iter(vals))
        variable = len(vals) > 1 or i == n - 1 or bool(re.search(r"\d", v or "")) and i >= n - 2
        segs.append("[^/]+" if variable else re.escape(v))
    tail = "/?$" if same_len else "(/.*)?$"
    return "^/" + "/".join(segs) + tail


def pattern_choices(urls):
    """From the examples' addresses: the strict pattern, then broader ones (one section level at a time)."""
    paths = [[x for x in urlparse(u).path.split("/") if x] for u in urls]
    n = max((len(p) for p in paths), default=0)
    if not n or any(len(p) != n for p in paths):
        return [{"pattern": url_pattern(urls), "label": "Pages like the examples"}]
    base = [p[0] if len({q[i] for q in paths}) == 1 and i < n - 1 else None for i, p in [(i, paths[0]) for i in range(n)]]
    base = [paths[0][i] if all(q[i] == paths[0][i] for q in paths) and i < n - 1 else None for i in range(n)]
    out = []
    literal = [i for i, v in enumerate(base) if v is not None]
    for keep in range(len(literal), 0, -1):
        segs = [re.escape(base[i]) if i in literal[:keep] else "[^/]+" for i in range(n)]
        shown = "/" + "/".join(base[i] if i in literal[:keep] else "..." for i in range(n))
        out.append({"pattern": "^/" + "/".join(segs) + "/?$", "label": shown})
    return out or [{"pattern": url_pattern(urls), "label": "Pages like the examples"}]


def learn(template, examples, fetch):
    """examples: [{"url":..., "row": index into template["rows"]}]; fetch(url) -> (status, final_url, html)."""
    cols = template["columns"]
    rows = template.get("rows") or []
    pages, warnings = [], []
    for ex in examples:
        st, fu, html = fetch(ex["url"])
        if not html:
            raise ValueError(f"Could not open {ex['url']}")
        fu = fu or ex["url"]
        if urlparse(fu).path.rstrip("/") != urlparse(ex["url"]).path.rstrip("/"):
            warnings.append({"row": ex.get("row", 0), "url": ex["url"], "now": fu,
                             "text": f"Row {ex.get('row', 0) + 1}: this link now opens a different page ({fu}). "
                                     "The website may have changed since your sample was made, so its values may not match."})
        pages.append({"url": fu, "soup": BeautifulSoup(html, "lxml"), "row": ex.get("row", 0)})
    result = []
    # columns with the same value in every sample row are the user's own values, not page data
    constant = {}
    if len(rows) >= 2:
        for ci in range(len(cols)):
            vals = {r[ci] for r in rows if ci < len(r)}
            v = next(iter(vals)) if len(vals) == 1 else ""
            # a site's own homepage link (same on every row) is a fixed value; item links are not
            if v and (not v.startswith("http") or urlparse(v).path.strip("/") == ""):
                constant[ci] = v
    for ci, c in enumerate(cols):
        if c.get("field") == "serial" or c.get("formula"):  # numbered / calculated like the sample, never read from pages
            result.append({"col": ci, "header": c["header"], "samples": [rows[0][ci] if rows else ""], "rule": None,
                           "values": [], "status": "formula" if c.get("formula") else "auto", "suggest": []})
            continue
        if ci in constant:
            # same in every sample row: page data only if it sits next to a label that matches the column name
            named = None
            for cand in candidates_for(constant[ci], pages[0]["soup"], pages[0]["url"]):
                name = cand.get("label") or cand.get("path") or cand.get("key") or ""  # e.g. CollegeOrUniversity.name
                if name and (similarity(c["header"], name) >= 0.5 or low(c["header"]) in low(name) or low(name) in low(c["header"])
                             or same_meaning(c["header"], name)) \
                        and all(same_value(constant[ci], apply_rule(cand, pg["soup"], pg["url"]) or "") for pg in pages):
                    named = {k: v for k, v in cand.items() if not k.startswith("_")}
                    break
            if named:
                if is_numberish(constant[ci]) and named["type"] in ("label", "css"):
                    named["numeric"] = True
                result.append({"col": ci, "header": c["header"], "samples": [constant[ci]], "rule": named,
                               "values": [apply_rule(named, pg["soup"], pg["url"]) for pg in pages],
                               "status": "found", "constant": constant[ci], "suggest": []})
            else:
                result.append({"col": ci, "header": c["header"], "samples": [constant[ci]], "rule": None, "values": [],
                               "status": "constant", "constant": constant[ci], "suggest": []})
            continue
        per_page = []
        for pg in pages:
            sample = rows[pg["row"]][ci] if pg["row"] < len(rows) and ci < len(rows[pg["row"]]) else ""
            per_page.append((sample, candidates_for(sample, pg["soup"], pg["url"]) if sample else []))
        rule = None
        found_somewhere = any(cands for _, cands in per_page)
        if found_somewhere:
            # a rule must read the right value on EVERY example page that has a sample value
            base = next(cands for _, cands in per_page if cands)
            for cand in base:
                ok = True
                for (sample, _), pg in zip(per_page, pages):
                    if sample and not same_value(sample, apply_rule(cand, pg["soup"], pg["url"]) or ""):
                        ok = False
                        break
                if ok:
                    rule = cand
                    break
        status = "found" if rule else None
        if rule is None and found_somewhere:
            # exact on some example pages, slightly different on others (a price or title that changed)
            def near(sample, got):
                if not sample or not got:
                    return not sample
                if is_numberish(sample):
                    a, b = number(sample), number(got)
                    return b is not None and a and abs(float(b) - float(a)) <= 0.25 * abs(float(a))
                return similarity(sample, got) >= 0.6
            for cand in base:
                if all(near(smp, apply_rule(cand, pg["soup"], pg["url"])) for (smp, _), pg in zip(per_page, pages)):
                    rule, status = cand, "close"
                    break
        if rule is None and any(per_page):
            # same thing written differently? (1 year vs 12, January vs Jan, renamed title)
            for (sample, _), pg in zip(per_page, pages):
                if not sample:
                    continue
                close = close_candidates(sample, c["header"], pg["soup"], pg["url"])
                if close:
                    rule = {k: v for k, v in close[0].items() if not k.startswith("_")}
                    status = "converted" if rule.get("convert") else "close"
                    break
        first_sample = next((smp for smp, _ in per_page if smp), "")
        if rule and rule["type"] in ("label", "css") and is_numberish(first_sample) and not rule.get("convert"):
            rule["numeric"] = True  # number columns only accept a number from the page
        values = [convert(rule, apply_rule(rule, pg["soup"], pg["url"]), "") if rule else None for pg in pages]
        alts = []
        if rule is None:
            # nothing matched the sample: suggest page fields whose label resembles the column name
            fields = detected_fields(pages[0]["soup"], pages[0]["url"])
            scored = sorted(fields, key=lambda f: -similarity(c["header"], f["label"].strip('"')))
            alts = [f for f in scored[:5] if similarity(c["header"], f["label"].strip('"')) > 0.45]
        samples = [s for s, _ in per_page]
        result.append({"col": ci, "header": c["header"], "samples": samples, "rule": rule, "values": values,
                       "status": status or ("empty_sample" if not any(samples) else "not_found"),
                       "suggest": alts})
    return {"columns": result, "pattern": url_pattern([p["url"] for p in pages]),
            "patterns": pattern_choices([p["url"] for p in pages]),
            "fields": detected_fields(pages[0]["soup"], pages[0]["url"]), "warnings": warnings,
            "university": looks_university(template), "domain": urlparse(pages[0]["url"]).netloc}


def plausible(v, sample):
    """A number column must get the same kind of number as the sample: not a year in a fee column
    ("2027/28 fees to be confirmed" -> 2027), not 100 times smaller or larger."""
    if not isinstance(v, (int, float)) or not is_numberish(sample):
        return v
    smp = float(number(sample))
    is_year = lambda x: float(x).is_integer() and 1990 <= x <= 2100
    if is_year(v) and not is_year(smp):
        return None
    if smp >= 100 and not (smp / 6 <= float(v) <= smp * 6):
        return None
    return v


def extract(profile, html, url):
    soup = BeautifulSoup(html, "lxml")
    row, got = {}, 0
    for c in profile["columns"]:
        r = c.get("rule")
        if not r:
            row[c["header"]] = None
            continue
        v = transform(convert(r, apply_rule(r, soup, url), c.get("sample", "")), c.get("sample", ""))
        v = plausible(v, c.get("sample", ""))
        row[c["header"]] = v
        if v not in (None, "") and r["type"] != "page_url":
            got += 1
    need = min(2, sum(1 for c in profile["columns"] if c.get("rule") and c["rule"]["type"] != "page_url"))
    return row, (got if got >= need else 0)


# ---------------------------------------------------------------- profiles (format + site)
def profile_id(template_id, domain):
    return re.sub(r"[^a-z0-9]+", "-", f"{template_id}-{domain}".lower()).strip("-")


def save_profile(p):
    os.makedirs(PROFILES, exist_ok=True)
    with open(os.path.join(PROFILES, f"{p['id']}.json"), "w") as f:
        json.dump(p, f, indent=1)


def load_profile(pid):
    with open(os.path.join(PROFILES, re.sub(r"[^a-z0-9-]", "", pid) + ".json")) as f:
        return json.load(f)


def list_profiles():
    out = []
    if os.path.isdir(PROFILES):
        for n in sorted(os.listdir(PROFILES)):
            if n.endswith(".json"):
                try:
                    p = json.load(open(os.path.join(PROFILES, n)))
                    out.append({"id": p["id"], "name": p.get("name", p["id"]), "domain": p["domain"],
                                "template": p["template"], "created": p.get("created", "")})
                except (OSError, ValueError, KeyError):
                    pass
    return out
