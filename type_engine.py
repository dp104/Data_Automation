"""Read product / job / property / hotel / flight data from pages using what such pages normally contain:
schema.org data, page tags, labels ("Price", "Bedrooms"), headings and repeated result cards.
Every value comes from the page; a field the page does not show stays empty."""
import html
import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

import generic_engine as ge
from generic_engine import clean, full_text, low, number

ISO_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
AVAIL = {"instock": "In stock", "outofstock": "Out of stock", "preorder": "Pre-order", "limitedavailability": "Limited stock",
         "soldout": "Sold out", "onlineonly": "Online only", "backorder": "Back order", "discontinued": "Discontinued"}


# ---------------------------------------------------------------- schema.org items
def jsonld_objects(soup):
    out = []
    for s in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        try:
            data = json.loads(s.string or s.get_text() or "")
        except ValueError:
            continue
        stack = [data]
        while stack:
            o = stack.pop()
            if isinstance(o, list):
                stack.extend(o)
            elif isinstance(o, dict):
                out.append(o)
                for k, v in o.items():
                    if k in ("@graph", "itemListElement", "item", "mainEntity", "about", "hasPart", "containsPlace", "subjectOf") \
                            or isinstance(v, (list, dict)) and k not in ("offers", "address", "aggregateRating", "brand"):
                        stack.append(v)
    return out


def types_of(o):
    t = o.get("@type", "")
    return set(t if isinstance(t, list) else [t])


def flatten(o, base):
    out = {}

    def walk(v, path):
        if isinstance(v, dict):
            for k, x in v.items():
                if not k.startswith("@"):
                    walk(x, f"{path}.{k}")
        elif isinstance(v, list):
            for i, x in enumerate(v[:8]):
                walk(x, path if i == 0 else f"{path}[{i}]")
            if v and all(isinstance(x, (str, int, float)) for x in v):  # ["Wifi","Pool"] -> "Wifi, Pool"
                out.setdefault(path, ", ".join(str(x) for x in v[:20]))
        elif v not in (None, ""):
            out.setdefault(path, clean(html.unescape(html.unescape(str(v)))))  # "EverCool&amp;trade;" -> "EverCool™"
    walk(o, base)
    return out


def items_on_page(soup, item_types):
    """schema.org objects of the wanted kind on this page, flattened."""
    objs = [o for o in jsonld_objects(soup) if types_of(o) & set(item_types)]
    seen, flat = set(), []
    for o in objs:
        key = json.dumps(o, sort_keys=True, default=str)[:4000]
        if key in seen:
            continue
        seen.add(key)
        base = sorted(types_of(o) & set(item_types))[0]
        flat.append(flatten(o, base))
    return flat


# ---------------------------------------------------------------- labels on the page
def label_pairs(root):
    """{label (lower): value} from dt/dd, th/td, 'Label: value' and label-before-value layouts."""
    pairs = {}
    for dt in root.find_all(["dt", "th"]):
        nxt = dt.find_next_sibling(["dd", "td"])
        if nxt is not None:
            pairs.setdefault(low(full_text(dt)).rstrip(":"), full_text(nxt))
    for el in ge.leaf_elements(root):
        txt = full_text(el)
        if len(txt) <= 140:
            m = re.match(r"^\s*([A-Za-z][^:]{1,38}?)\s*:\s*(.+)$", txt)
            if m:
                pairs.setdefault(low(m.group(1)), clean(m.group(2)))
        lab, rel, _ = ge.label_for(el)
        if lab and len(lab) <= 40 and 0 < len(txt) <= 200 and low(lab) != low(txt):
            pairs.setdefault(low(lab).rstrip(":"), txt)
    return pairs


def by_label(pairs, labels):
    for lab in labels:
        lab = low(lab)
        if lab in pairs:
            return pairs[lab]
    for lab in labels:  # "Price (incl. tax)" when looking for "price"
        lab = low(lab)
        for k, v in pairs.items():
            if k.startswith(lab + " ") or k.startswith(lab + "(") or k == lab + "s":
                return v
    return None


# ---------------------------------------------------------------- values
def typed(v, kind):
    if v in (None, ""):
        return None
    if kind in ("money", "number"):
        n = number(str(v).replace(" ", " "))
        return n
    if kind == "int":
        n = number(v)
        return int(n) if isinstance(n, (int, float)) else None
    if kind == "date":
        m = ISO_DATE.match(str(v))
        return m.group(1) if m else clean(v)[:40]
    if kind == "url":
        return clean(v)
    s = clean(v)
    tail = s.rstrip("/").rsplit("/", 1)[-1].lower()
    if s.startswith("http") and "schema.org" in s and tail in AVAIL:
        return AVAIL[tail]
    if re.fullmatch(r"(https?://schema\.org/)?[A-Z][A-Za-z]+", s) and s.split("/")[-1].lower() in AVAIL:
        return AVAIL[s.split("/")[-1].lower()]
    return s[:500]


def money_and_currency(t, row, raw):
    if "currency" in row and not row.get("currency"):
        for f in t["fields"]:
            if f["type"] == "money" and raw.get(f["key"]):
                c = currency_of(raw[f["key"]])
                if c:
                    row["currency"] = c
                    break
    return row


def field_value(f, flat, meta, pairs, soup, url, site):
    sp = f.get("special")
    if sp == "page_url":
        return ge.apply_rule({"type": "page_url"}, soup, url) if soup is not None else url
    if sp == "site":
        return site
    for pat in f.get("jsonld", []):
        rx = re.compile(pat)
        hits = [v for k, v in flat.items() if rx.search(k)]
        if hits:
            return ", ".join(dict.fromkeys(hits)) if f.get("join") else hits[0]
    for m in f.get("meta", []):
        if meta.get(m):
            return meta[m]
    v = by_label(pairs, f.get("labels", []))
    if v:
        return v
    if soup is not None:
        for sel in f.get("css", []):
            try:
                el = soup.select_one(sel)
            except Exception:  # noqa: BLE001
                el = None
            if el is not None and full_text(el):
                return full_text(el)
        if sp == "heading":
            h1 = soup.find("h1")
            if h1 is not None and full_text(h1):
                return full_text(h1)
    return None


CUR_SIGNS = [("£", "GBP"), ("€", "EUR"), ("₹", "INR"), ("Rs", "INR"), ("AED", "AED"), ("A$", "AUD"), ("AU$", "AUD"), ("C$", "CAD"),
             ("CA$", "CAD"), ("US$", "USD"), ("$", "USD"), ("¥", "JPY"), ("SGD", "SGD"), ("CHF", "CHF")]


def currency_of(text):
    s = str(text or "")
    for sign, code in CUR_SIGNS:
        if sign in s:
            return code
    m = re.search(r"\b(USD|EUR|GBP|INR|AED|AUD|CAD|SGD|NZD|JPY|CHF|SAR|QAR|MYR)\b", s)
    return m.group(1) if m else None


def title_company(soup):
    t = clean(soup.title.string) if soup is not None and soup.title and soup.title.string else ""
    m = re.search(r"\bat\s+([A-Z][\w&.,' -]{1,60}?)\s*(?:[|\-–]|$)", t)
    return m.group(1).strip() if m else None


def finish(t, row):
    if "discount" in row and row.get("price") and row.get("mrp"):
        try:
            p, m = float(row["price"]), float(row["mrp"])
            if m > p > 0:
                row["discount"] = round((m - p) / m * 100, 1)
        except (TypeError, ValueError):
            pass
    if row.get("mrp") and row.get("price") and row["mrp"] == row["price"]:
        row["mrp"] = None
    return row


def site_name(soup, url):
    og = soup.find("meta", property="og:site_name") if soup is not None else None
    return clean(og.get("content")) if og and og.get("content") else urlparse(url).netloc.removeprefix("www.")


KEY_FIELDS = {"product": ["price"], "realestate": ["price"], "hotels": ["night", "rating"], "flights": ["fare"], "jobs": ["company", "location", "posted"]}


def extract_page(t, html, url, expect_item=False):
    """Rows found on one page: one row for an item page; several for a results page. Also says if the page
    is an item page (so the crawler can prefer item data over listing cards)."""
    soup = BeautifulSoup(html, "lxml")
    site = site_name(soup, url)
    meta = ge.meta_flat(soup)
    if t["id"] == "jobs" and "og:site_name" not in meta:
        tc = title_company(soup)
        if tc:
            meta["og:site_name"] = tc
    items = items_on_page(soup, t["item_types"])
    rows = []
    if len(items) == 1 or (items and not t.get("listing") and len(items) <= 2):
        pairs = label_pairs(soup)
        raw = {f["key"]: field_value(f, items[0], meta, pairs, soup, url, site) for f in t["fields"]
               if f.get("special") not in ("serial", "checked", "change", "computed")}
        row = {k: typed(v, next(f["type"] for f in t["fields"] if f["key"] == k)) for k, v in raw.items()}
        rows.append(finish(t, money_and_currency(t, row, raw)))
        return rows, "item"
    if len(items) > 1:  # a results page with structured data per result
        for it in items:
            row = {}
            for f in t["fields"]:
                if f.get("special") in ("serial", "checked", "change", "computed"):
                    continue
                if f.get("special") == "page_url":
                    # prefer the item's own top-level url ("Product.url") over a nested one picked up
                    # from a sub-object like image/offers ("Product.image.url", "Product.offers.url")
                    link = next((v for k, v in it.items() if re.fullmatch(r"[^.]+\.url", k)), None) \
                        or next((v for k, v in it.items() if re.search(r"\.url$", k)), None)
                    row[f["key"]] = urljoin(url, link) if link else None
                    continue
                if f.get("special") == "site":
                    row[f["key"]] = site
                    continue
                v = None
                for pat in f.get("jsonld", []):
                    rx = re.compile(pat)
                    v = next((x for k, x in it.items() if rx.search(k)), None)
                    if v:
                        break
                row[f["key"]] = typed(v, f["type"])
            if any(row.get(k) for k in KEY_FIELDS.get(t["id"], [])) or row.get("url"):
                rows.append(finish(t, row))
        return rows, "list"
    # no structured data: an item page if the type's key values sit next to their labels / in typical places
    pairs = label_pairs(soup)
    raw = {f["key"]: field_value(f, {}, meta, pairs, soup, url, site) for f in t["fields"]
           if f.get("special") not in ("serial", "checked", "change", "computed")}
    row = money_and_currency(t, {k: typed(v, next(f["type"] for f in t["fields"] if f["key"] == k)) for k, v in raw.items()}, raw)
    keys = KEY_FIELDS.get(t["id"], [])
    found = sum(1 for f in t["fields"] if f.get("special") not in ("serial", "checked", "change", "computed", "page_url", "site", "heading")
                and row.get(f["key"]) not in (None, ""))
    has_title = bool(soup.find("h1")) or any(row.get(f["key"]) for f in t["fields"] if f.get("special") == "heading")
    if not expect_item:  # 3+ result cards that each carry the type's key value = a results page
        cards = card_rows(t, soup, url, site)
        keyed = [c for c in cards if any(c.get(k) not in (None, "") for k in keys)]
        if len(keyed) >= 3:
            return keyed, "list"
    if (keys and any(row.get(k) not in (None, "") for k in keys) and found >= 2 and not looks_listing(soup)) \
            or (expect_item and has_title):  # reached from a listing card: this is that item's page
        rows.append(finish(t, row))
        return rows, "item"
    cards = card_rows(t, soup, url, site)
    return cards, ("list" if cards else "none")


# ---------------------------------------------------------------- result cards (no structured data)
MONEY_RE = re.compile(r"(?:£|€|\$|₹|Rs\.?|AED|USD|EUR|GBP|INR|AUD|CAD)\s?\d|\d[\d,.]*\s?(?:£|€|\$|₹|AED|USD|EUR|GBP|INR)", re.I)


def looks_listing(soup):
    return len(find_cards(soup)) >= 4


def find_cards(soup):
    """Repeated blocks (same tag + class) that each contain a link and a price/heading: search results."""
    groups = {}
    for el in soup.find_all(["article", "li", "div", "tr", "section"]):
        # first stable class only: cards differ in extra classes ("instock" / "outofstock", category names)
        cls = next((c for c in el.get("class", []) if not re.search(r"\d|active|selected|first|last|odd|even|^js-", c)), "")
        if el.parent is None or (not cls and el.name not in ("li", "tr", "article")):
            continue
        key = (id(el.parent), el.name, cls)
        groups.setdefault(key, []).append(el)
    best = []
    for (_, name, cls), els in groups.items():
        if len(els) < 3:
            continue
        good = [e for e in els if e.find("a", href=True) and 15 <= len(full_text(e)) <= 1500]
        if len(good) >= 3 and len(good) >= 0.6 * len(els) and len(good) > len(best):
            best = good
    return best


def card_rows(t, soup, url, site):
    rows = []
    for card in find_cards(soup):
        pairs = label_pairs(card)
        row = {}
        heading = card.find(["h1", "h2", "h3", "h4", "h5"]) or card.find("a", title=True) or card.find("a", href=True)
        link = None
        a = heading.find("a", href=True) if isinstance(heading, Tag) and heading.name != "a" else heading
        if isinstance(a, Tag) and a.get("href"):
            link = urljoin(url, a["href"])
        if not link:
            a = card.find("a", href=True)
            link = urljoin(url, a["href"]) if a else None
        for f in t["fields"]:
            sp = f.get("special")
            if sp in ("serial", "checked", "change", "computed"):
                continue
            if sp == "page_url":
                row[f["key"]] = link
            elif sp == "site":
                row[f["key"]] = site
            elif sp == "heading":
                txt = None
                if isinstance(heading, Tag):
                    linked = heading if heading.name == "a" else heading.find("a", title=True)
                    txt = linked.get("title") if isinstance(linked, Tag) and linked.get("title") else None
                    if not txt:
                        blocks = [b for b in heading.find_all(True) if full_text(b) and not b.find(True)]
                        txt = full_text(blocks[0]) if heading.name == "a" and len(blocks) > 1 else full_text(heading)
                row[f["key"]] = typed(txt, "text")
            else:
                v = by_label(pairs, f.get("labels", []))
                if v is None:
                    for sel in f.get("css", []):
                        try:
                            el = card.select_one(sel)
                        except Exception:  # noqa: BLE001
                            el = None
                        if el is not None and full_text(el):
                            v = full_text(el)
                            break
                if v is None and f["type"] == "money" and f["key"] in KEY_FIELDS.get(t["id"], []):
                    m = next((full_text(e) for e in ge.leaf_elements(card) if MONEY_RE.search(full_text(e)) and len(full_text(e)) < 40), None)
                    v = m
                row[f["key"]] = typed(v, f["type"])
        if "currency" in row and not row.get("currency"):
            row["currency"] = currency_of(full_text(card))
        plain = {f["key"] for f in t["fields"] if f.get("special") in ("heading", "page_url", "site")} | {"currency"}
        if not any(v not in (None, "") for k, v in row.items() if k not in plain):
            continue  # just a link with a title (menus, filters, related links): not a result card
        if row.get("url") or any(row.get(k) for k in KEY_FIELDS.get(t["id"], [])):
            rows.append(finish(t, row))
    return rows


def next_page_links(soup, url):
    out = []
    for a in soup.find_all("a", href=True):
        txt = low(full_text(a))
        rel = " ".join(a.get("rel", [])).lower()
        if rel == "next" or txt in ("next", "next page", "next ›", "next »", "›", "»", ">") or "next" in " ".join(a.get("class", [])).lower():
            out.append(urljoin(url, a["href"]))
    for l in soup.find_all("link", rel="next"):
        if l.get("href"):
            out.append(urljoin(url, l["href"]))
    if not out:
        # "Next page" / "Go to page 2" buttons without links (JavaScript paging): try ?page=N+1
        btn = soup.find(lambda el: isinstance(el, Tag) and el.name in ("button", "a", "li", "span") and not el.get("href")
                        and re.search(r"next page|go to page \d|^next$", (el.get("aria-label") or full_text(el) or ""), re.I))
        if btn is not None:
            m = re.search(r"([?&])page=(\d+)", url)
            if m:
                out.append(url[:m.start()] + f"{m.group(1)}page={int(m.group(2)) + 1}" + url[m.end():])
            else:
                out.append(url + ("&" if "?" in url else "?") + "page=2")
    return out
