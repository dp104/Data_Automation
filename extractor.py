"""Generic course-page extractor. Works on any university page layout by combining
schema.org JSON-LD, label/value pairs, fee tables and free-text patterns."""
import json
import re

from bs4 import BeautifulSoup

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]
MONTH_RE = re.compile(r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
                      r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b\.?", re.I)
SEASON = {"autumn": "September", "fall": "September", "spring": "January", "summer": "May", "winter": "January"}
SEASON_RE = re.compile(r"\b(autumn|fall|spring|summer|winter)\b", re.I)

WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
           "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "eighteen": 18, "fifteen": 15,
           "half": 0.5, "a": 1, "an": 1}

# ----- award / level detection ------------------------------------------------------------
PG_AWARDS = r"(?:M\.?Sc|MSc\s?\(Res\)|M\.?A|MBA|EMBA|LL\.?M|M\.?Res|M\.?Phil|M\.?Ed|MFA|MPA|MPH|MArch|M\.?Litt|MSt|MMus|MDes|" \
            r"MFin|MMgt|MIM|MPP|MSW|MEM|MEd|M\.?S|M\.?Eng\s?\(?by research|MTh|MDiv|MClinRes|MNurs|MRes|MPharm?\s?Sci|" \
            r"Juris\s+Doctor|J\.?D|PG\s?Dip|PgDip|PG\s?Cert|PgCert|PGCE|PGDE|Postgraduate\s+(?:Diploma|Certificate)|Graduate\s+(?:Diploma|Certificate)|" \
            r"Master(?:'?s)?\s+of|Masters?\s+in|Master\s+in|Magister)"
UG_AWARDS = r"(?:B\.?Sc|B\.?A|B\.?Eng|M\.?Eng|MPharm|MPhys|MChem|MMath|MComp|MSci|MOccTh|MDiet|MDRad|MSLT|MPod|MOptom|MPhysio|MNutr|LL\.?B|B\.?Ed|" \
            r"BBA|B\.?Com|BMus|BN|BNurs|BMid|BDes|BFA|BArch|BVSc|BDS|MBChB|MBBS|BMedSci|B\.?S|B\.?Tech|B\.?E|" \
            r"HND|HNC|Foundation\s+Degree|Extended\s+Degree|Initial\s+Year|Integrated\s+Master'?s|CertHE|DipHE|FdA|FdSc|Bachelor(?:'?s)?\s+of|Bachelors?\s+in|Bachelor\s+in|Associate\s+(?:of|Degree))"
DOC_AWARDS = r"(?:Masters?\s+by\s+Research|Master\s+of\s+Research|Ph\.?D|D\.?Phil|MD\s?\(Res\)|Ed\.?D|DProf|DClinPsy|DNP|DPT|Doctor\s+of\s+(?!Business\s+Administration|Medicine|Dental|Pharmacy|Physiotherapy|Optometry)\w+|" \
             r"Professional\s+Doctorate|Doctorate)"
DBA_AWARDS = r"(?:DBA|Doctor\s+of\s+Business\s+Administration)"
FOUNDATION = r"(?:International\s+Foundation|Foundation\s+(?:Year|Programme|Program|Certificate|Course)|" \
             r"International\s+Year\s+One|Pre-?Master'?s|Pre-?sessional|Access\s+to\s+(?:HE|Higher)|Pre-?university|" \
             r"(?:International|Academic|Undergraduate|Postgraduate)\s+Pathway)"
COLLEGE = r"(?:Advanced\s+Diploma|Ontario\s+College\s+(?:Diploma|Certificate|Graduate\s+Certificate)|" \
          r"Diploma(?:\s+(?:of|in))?|Certificate(?:\s+(?:of|in|IV|III))?)"

AWARD_ANY = re.compile(rf"(?<![\w-])(?:{DBA_AWARDS}|{DOC_AWARDS}|{PG_AWARDS}|{UG_AWARDS}|{FOUNDATION}|{COLLEGE})(?![\w-])"
                       r"(?:\s?\((?:Hons|Honours|Res|Ext)\))?", re.I)

LEVEL_ORDER = ["Undergraduate", "Postgraduate", "Research", "DBA", "Foundation", "Diploma/Certificate", "Short course/Other"]


def classify_level(title, context=""):
    """Level from the award in the title first; then URL/breadcrumb/page context."""
    t = title or ""
    tests = [("DBA", DBA_AWARDS), ("Research", DOC_AWARDS)]
    if not re.search(rf"(?<![\w-]){UG_AWARDS}", t, re.I):  # "BA (Hons) X with Foundation Year" is a degree
        tests.append(("Foundation", FOUNDATION))
    for lvl, pat in tests:
        if re.search(rf"(?<![\w-]){pat}(?![\w-])", t, re.I):
            return lvl
    # MSc/MRes by research
    if re.search(r"\bby research\b|\bMPhil\b", t, re.I):
        return "Research"
    ug = re.search(rf"(?<![\w-]){UG_AWARDS}(?![\w-])", t, re.I)
    pg = re.search(rf"(?<![\w-]){PG_AWARDS}(?![\w-])", t, re.I)
    ctx = (context or "").lower()
    ctx_pg = re.search(r"/postgraduate|/pg/|/graduate/|/masters?/|postgraduate taught|/taught/", ctx)
    ctx_ug = re.search(r"/undergraduate|/ug/|/bachelors?/", ctx)
    if ug and not pg and ctx_pg and re.match(r"(?i)m", ug.group(0)):  # MEng/MSci/MPharm listed under postgraduate
        return "Postgraduate"
    if pg and ug:  # e.g. "BSc / Master of Architecture": first award wins
        return "Postgraduate" if pg.start() < ug.start() else "Undergraduate"
    if pg:
        return "Postgraduate"
    if ug:
        return "Undergraduate"
    if re.search(rf"(?<![\w-]){COLLEGE}(?![\w-])", t, re.I):
        c = context.lower()
        if "postgraduate" in c or "graduate certificate" in t.lower():
            return "Postgraduate"
        return "Diploma/Certificate"
    c = (context or "").lower()
    if re.search(r"\bdoctor(al|ate)|\bphd\b|research[\s-]degree|/research/|postgraduate[\s-]research", c):
        return "Research"
    if re.search(r"postgraduate|\bmasters?\b|/pg/|/graduate/|taught", c):
        return "Postgraduate"
    if re.search(r"undergraduate|bachelor|/ug/", c):
        return "Undergraduate"
    return "Short course/Other"


# ----- text helpers -----------------------------------------------------------------------
def page_lines(soup):
    for t in soup(["script", "style", "noscript", "svg", "iframe", "template"]):
        t.decompose()
    for sel in ["header nav", "footer", "[role=navigation]", ".breadcrumb nav", "[id*=cookie i]", "[class*=cookie i]",
                "[id*=consent i]", "[class*=consent i]", "[id*=onetrust i]", "[id*=Cybot]", "[aria-label*=cookie i]"]:
        try:
            for el in soup.select(sel):
                el.decompose()
        except Exception:  # noqa: BLE001
            pass
    txt = soup.get_text("\n")
    out = []
    for l in txt.splitlines():
        l = re.sub(r"[\s ​]+", " ", l).strip()
        if l:
            out.append(l)
    return out


def jsonld(soup):
    items = []
    for s in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        raw = s.string or s.get_text() or ""
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            try:
                data = json.loads(re.sub(r"[\x00-\x1f]", " ", raw))
            except Exception:  # noqa: BLE001
                continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, dict):
                items.append(d)
                for k in ("@graph", "hasCourseInstance", "offers", "mainEntity"):
                    v = d.get(k)
                    if isinstance(v, list):
                        stack.extend(v)
                    elif isinstance(v, dict):
                        stack.append(v)
            elif isinstance(d, list):
                stack.extend(d)
    return items


def ld_type(d):
    t = d.get("@type", "")
    return " ".join(t) if isinstance(t, list) else str(t)


NUMDATE_RE = re.compile(r"\b(20\d\d)-(\d{1,2})-\d{1,2}\b|\b(\d{1,2})[/.](\d{1,2})[/.](20\d\d|\d\d)\b")
DAY_FIRST = True  # dd/mm/yyyy (UK, EU, AU, IN); set False for US sites


def numeric_months(text):
    out = []
    for m in NUMDATE_RE.finditer(text or ""):
        if m.group(1):
            mo = int(m.group(2))
        else:
            a, b = int(m.group(3)), int(m.group(4))
            mo = b if (DAY_FIRST and b <= 12) or a > 12 else a
        if 1 <= mo <= 12 and MONTHS[mo - 1] not in out:
            out.append(MONTHS[mo - 1])
    return out


# academic terms -> start month; southern-hemisphere sites are switched by set_region()
TERM_MONTHS = {"tri": {"1": "March", "2": "July", "3": "November"},
               "sem": {"1": "September", "2": "January", "a": "September", "b": "January", "c": "May"}}


def set_region(tld):
    global DAY_FIRST
    if tld in ("au", "nz"):
        TERM_MONTHS["tri"] = {"1": "March", "2": "July", "3": "November"}
        TERM_MONTHS["sem"] = {"1": "February", "2": "July"}
    DAY_FIRST = tld not in ("edu", "us")


TERM_RE = re.compile(r"\b(tri|sem)m?esters?\s*((?:[1-3a-c](?:\s*(?:,|&|and|/|or)\s*)?)+)\b", re.I)


def term_months(text):
    out = []
    for m in TERM_RE.finditer(text or ""):
        table = TERM_MONTHS[m.group(1).lower()]
        for k in re.findall(r"[1-3a-c]", m.group(2).lower()):
            mo = table.get(k)
            if mo and mo not in out:
                out.append(mo)
    return out


def months_in(text, numeric=False):
    found = numeric_months(text) if numeric else []
    if numeric:
        found += [m for m in term_months(text) if m not in found]
    for m in MONTH_RE.finditer(text or ""):
        w = m.group(1).lower()
        if w == "may" and not re.search(r"\bmay\s+(20\d\d|\d{1,2}\b|intake|start|entry)|\b\d{1,2}(st|nd|rd|th)?\s+may\b|,\s*may\b|\bmay\s*(,|and|or|$)",
                                        text[max(0, m.start() - 8): m.end() + 12], re.I):
            continue  # "you may ..." is not a month
        if w == "mar" and not m.group(0)[0].isupper():
            continue
        name = next(x for x in MONTHS if x.lower().startswith(w[:3]))
        if name not in found:
            found.append(name)
    for m in SEASON_RE.finditer(text or ""):
        name = SEASON[m.group(1).lower()]
        if name not in found:
            found.append(name)
    return found


def to_months(num, unit):
    unit = unit.lower()
    if unit.startswith("y"):
        return round(num * 12)
    if unit.startswith("m"):
        return round(num)
    if unit.startswith("w"):
        return max(1, round(num / 4.345))
    if unit.startswith(("sem", "term")):
        return round(num * 6) if unit.startswith("sem") else round(num * 4)
    if unit.startswith("tri"):
        return round(num * 4)
    return None


DUR_RE = re.compile(r"(\d+(?:\.\d+)?|\d+\s?½|one|two|three|four|five|six|seven|eight|twelve|eighteen|fifteen|half|an?)"
                    r"(?:\s?(?:-|to|–)\s?\d+(?:\.\d+)?)?\s?(?:\+\s?\d+\s)?"
                    r"\s?-?(years?|yrs?|months?|mths?|weeks?|wks?|semesters?|terms?|trimesters?)\b", re.I)


def parse_durations(text):
    """Return list of (months, is_part_time, is_placement) found in text."""
    res = []
    if not text:
        return res
    segs = re.split(r"[;|\n]|,(?!\d)|\bor\b|/(?=\s*\d)", text)
    for seg in segs:
        pt = bool(re.search(r"part[\s-]?time|\bPT\b", seg, re.I))
        ft = bool(re.search(r"full[\s-]?time|\bFT\b", seg, re.I))
        for m in DUR_RE.finditer(seg):
            n = m.group(1).lower().replace(" ", "")
            if "½" in n:
                num = float(n.replace("½", "") or 0) + 0.5
            else:
                num = WORDNUM.get(n)
                if num is None:
                    try:
                        num = float(n)
                    except ValueError:
                        continue
            months = to_months(num, m.group(2))
            extra = re.match(r"\s*\+\s*(\d+|one|two)\s+(?:\w+\s+)?(years?|months?)", seg[m.end():], re.I)
            if months and extra:  # "5 years + 1 recommended year in practice"
                months += to_months(WORDNUM.get(extra.group(1).lower()) or float(extra.group(1)), extra.group(2))
            if not months or months > 120:
                continue
            after = seg[m.end(): m.end() + 45].lower()
            before = seg[max(0, m.start() - 30): m.start()].lower()
            placement = bool(re.search(r"placement|sandwich|industr|work experience|internship|year abroad|study abroad|with a year", after + " " + before))
            is_pt = pt and not (ft and seg.lower().find("full") < seg.lower().find("part"))
            res.append((months, is_pt, placement))
    return res


CUR = r"(?:£|€|US\$|USD|A\$|AU\$|AUD|C\$|CA\$|CAD|NZ\$|NZD|S\$|SGD|HK\$|RM|MYR|AED|CHF|SEK|DKK|NOK|PLN|CZK|HUF|GBP|EUR|\$|₹|INR)"
AMT = r"(\d{1,3}(?:[,.\s ]\d{3})+(?:[.,]\d{1,2})?|\d{4,6}(?:[.,]\d{1,2})?)"
MONEY_RE = re.compile(rf"{CUR}\s?{AMT}|{AMT}\s?(?:{CUR}|euros?|EUR|pounds)", re.I)
INTL_RE = re.compile(r"international|overseas|non[\s-]?eu\b|non[\s-]?uk|rest of (?:the )?world|outside (?:the )?(?:uk|eu|eea)|"
                     r"foreign|non[\s-]?resident|out[\s-]of[\s-]state|global students|third[\s-]country|non-domestic|"
                     r"islands?,? eu and international|eu/international|eu and international", re.I)
HOME_RE = re.compile(r"^(?:home|uk|scottish|scotland|welsh|domestic|local|resident|in[\s-]state|eu|rest of uk|england|"
                     r"northern ireland|irish|ruk|australian|canadian|new zealand|us citizen)\b", re.I)
BAD_FEE_CTX = re.compile(r"deposit|application fee|scholarship|bursary|discount|award of|off your|reduction|"
                         r"per credit|per module|per unit|living|accommodation|rent|salary|earn|loan|placement fee|"
                         r"bench fee|additional cost|resit|graduation|visa|ihs|health surcharge|placement|sandwich year|year abroad|"
                         r"field trips?|per trip|equipment|materials|uniform", re.I)


def parse_amount(s):
    s = s.strip().replace(" ", " ")
    s = re.sub(r"[.,](\d{1,2})$", "", s)  # drop pence/cents
    s = re.sub(r"[,.\s]", "", s)
    try:
        v = int(s)
    except ValueError:
        return None
    return v


def money(text):
    out = []
    for m in MONEY_RE.finditer(text):
        raw = m.group(1) or m.group(2)
        v = parse_amount(raw)
        if v and 1000 <= v <= 150000 and not (2000 <= v <= 2035 and not re.search(r"[£€$,.]", m.group(0))):
            out.append((v, m.start(), m.end()))
    return out


def fee_from_tables(soup):
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        grid = [[re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in r.find_all(["th", "td"])] for r in rows]
        # column header layout
        for hi, header in enumerate(grid[:3]):
            col = next((i for i, h in enumerate(header) if INTL_RE.search(h)), None)
            if col is None:
                continue
            for row in grid[hi + 1:]:
                if col < len(row):
                    if re.search(r"part[\s-]?time", " ".join(row[:1]), re.I):
                        continue
                    m = money(row[col])
                    if m:
                        return m[0][0]
        # row header layout; columns may be terms of one year ("Fall 2026 | Winter 2027") -> sum them
        term_cols = [i for i, h in enumerate(grid[0]) if i and re.search(r"fall|autumn|winter|spring|summer|term|semester|trimester", h, re.I)]
        for row in grid:
            if row and INTL_RE.search(row[0]) and not re.search(r"part[\s-]?time", row[0], re.I):
                if len(term_cols) >= 2:
                    vals = [money(row[i])[0][0] for i in term_cols if i < len(row) and money(row[i])]
                    if vals:
                        return sum(vals)
                for c in row[1:]:
                    m = money(c)
                    if m:
                        return m[0][0]
    return None


def fee_from_columns(lines):
    """Div-based grids flattened to lines: 'Canadian / International / Tuition / $6,747 / $16,933'."""
    n = len(lines)
    for i in range(n - 3):
        run = []
        for j in range(i, min(n, i + 4)):
            if len(lines[j]) < 45 and (INTL_RE.search(lines[j]) or HOME_RE.search(lines[j])) and not money(lines[j]):
                run.append(lines[j])
            else:
                break
        if len(run) < 2 or not any(INTL_RE.search(r) and not HOME_RE.search(r) for r in run) \
                or not any(HOME_RE.search(r) and not INTL_RE.search(r) for r in run):
            continue
        col = next(k for k, r in enumerate(run) if INTL_RE.search(r) and not HOME_RE.search(r))
        vals = []
        for j in range(i + len(run), min(n, i + len(run) + 10)):
            m = money(lines[j])
            if m and len(lines[j]) < 40:
                vals.append(m[0][0])
                if len(vals) == len(run):
                    return vals[col]
            elif vals:
                break
    return None


def fee_from_lines(lines):
    """First fee after an 'International' label, before the next home-student label."""
    n = len(lines)
    for i, l in enumerate(lines):
        if not INTL_RE.search(l) or len(l) > 220:
            continue
        # the label line itself, e.g. "International students: £18,940 per year"
        chunk = [l]
        for j in range(i + 1, min(n, i + 16)):
            nxt = lines[j]
            if len(nxt) < 60 and HOME_RE.search(nxt) and not INTL_RE.search(nxt):
                break
            if j > i + 1 and len(nxt) < 80 and INTL_RE.search(nxt) and not money(nxt):
                break
            chunk.append(nxt)
        # "International/EU  |  Not currently available" -> this block has no international fee
        first = next((c for c in chunk[1:4] if c.strip()), "")
        if not money(l) and re.match(r"(?i)\s*(not (currently )?(available|offered)|n/?a\b|-+$|tbc|to be confirmed)", first):
            continue
        pt_block = False
        for k, c in enumerate(chunk):
            if len(c) > 160:  # prose ("...placement year will pay £1,575...") is never the course fee
                continue
            if re.search(r"part[\s-]?time", c, re.I) and not re.search(r"full[\s-]?time", c, re.I) and len(c) < 60:
                pt_block = True
            if re.search(r"full[\s-]?time", c, re.I) and len(c) < 60:
                pt_block = False
            if pt_block:
                continue
            for v, s, e in money(c):
                ctx = c[max(0, s - 60): e + 40]
                if BAD_FEE_CTX.search(ctx) and not re.search(r"tuition|course fee|annual fee", ctx, re.I):
                    continue
                return v
    return None


FEE_LABEL = re.compile(r"^(?:(?:estimated|annual|yearly|course|programme|program|international|tuition|total)\s+)*"
                       r"(?:fees?|tuition(?: fees?)?|cost)(?:\s+per year|\s+\(per year\))?"
                       r"(?:\s*[-–:]\s*[A-Za-z]{3,9}\.?\s*\d{0,4})?\s*:?$", re.I)  # "YEARLY FEES - SEPTEMBER 26"


BARE_AMT = re.compile(r"^\s*(\d{1,3}(?:,\d{3})+|\d{4,6})(?:\.\d{1,2})?\b")


def fee_from_label(lines):
    for i, l in enumerate(lines):
        if len(l) < 40 and FEE_LABEL.match(l):
            for nxt in lines[i + 1: i + 3]:
                if len(nxt) >= 80 or BAD_FEE_CTX.search(nxt):
                    continue
                m = money(nxt)
                if m:
                    return m[0][0]
                b = BARE_AMT.match(nxt)  # "64,557 - Year 1" (currency only implied)
                if b:
                    v = parse_amount(b.group(1))
                    if v and 1000 <= v <= 150000:
                        return v
    return None


def fee_fallback(lines):
    best = None
    for l in lines:
        if re.search(r"tuition|course fee|fees?\b", l, re.I) and not BAD_FEE_CTX.search(l):
            for v, _, _ in money(l):
                best = max(best or 0, v)
    return best


LABELS = {
    "intake": re.compile(r"^(?:next\s+)?(?:start(?:ing)?\s*(?:dates?|months?|terms?)?|starts?|intakes?|entry\s*(?:dates?|points?|terms?|months?)|"
                         r"course\s+start(?:s|\s+dates?)?|commencement(?:\s+dates?)?|when (?:can|do) (?:i|you) start\??|"
                         r"available intakes?|intake months?|semester intakes?|study periods?|entry|enrolment dates?|"
                         r"start (?:date|dates) and duration|course dates?)\s*:?$", re.I),
    "duration": re.compile(r"^(?:course\s+)?(?:duration|length|course length|length of (?:course|study|programme)|"
                           r"programme duration|program duration|study duration|how long.*|time to complete|"
                           r"duration and delivery|years?|full[\s-]time duration)\s*:?$", re.I),
    "mode": re.compile(r"^(?:mode(?:s)? of (?:study|delivery|learning)|study modes?|delivery(?: mode| method)?|"
                       r"study options?|mode|method of study|study method|learning mode|format|delivery type|study type|"
                       r"course type|campus or online)\s*:?$", re.I),
    "attendance": re.compile(r"^(?:mode of attendance|attendance(?: mode| type| pattern)?|study pattern|study load|full[\s-]?time\s*/\s*part[\s-]?time|"
                             r"pace of study|study intensity|enrolment type)\s*:?$", re.I),
    "location": re.compile(r"^(?:locations?|campus(?:es)?|study locations?|where (?:you(?:'ll)? )?(?:will )?study|delivery location|"
                           r"location of study|venue|study at|campus location)\s*:?$", re.I),
    "level": re.compile(r"^(?:level|study level|qualification(?: type)?|award|course level|degree type|award type|"
                        r"qualification awarded|programme type|program type|degree level)\s*:?$", re.I),
}


def label_values(lines, soup):
    """Collect values for known labels from 'Label\\nValue' lines, 'Label: value' lines and dt/dd/th pairs."""
    vals = {k: [] for k in LABELS}
    n = len(lines)
    for i, l in enumerate(lines):
        if len(l) > 60:
            m = re.match(r"^([^:]{2,45}):\s*(.+)$", l)
            if m:
                for k, pat in LABELS.items():
                    if pat.match(m.group(1).strip()):
                        vals[k].append(m.group(2))
            continue
        cand = [l]
        m = re.match(r"^([^:]{2,45}):\s*(.*)$", l)
        if m:
            cand.append(m.group(1).strip())
        for k, pat in LABELS.items():
            if any(pat.match(c) for c in cand):
                if m and m.group(2).strip():
                    vals[k].append(m.group(2).strip())
                else:
                    vals[k].append(" | ".join(lines[i + 1: i + (13 if k == "intake" else 4)]))
    for dt in soup.find_all(["dt", "th"]):
        lab = dt.get_text(" ", strip=True)
        for k, pat in LABELS.items():
            if pat.match(lab):
                dd = dt.find_next_sibling(["dd", "td"])
                if dd:
                    vals[k].insert(0, dd.get_text(" | ", strip=True))
    return vals


OPT_KEYS = ("intake", "duration", "mode", "attendance")

CAMPUS_JUNK = re.compile(r"^(fees?|tuition|overview|entry requirements|modules|careers|locations?|campus(es)?|link|links?|employability|apply( now| online)?|year|start date|end date|"
                         r"map|campus map|select (a )?location|all locations|view( all)?|more|find out more|study mode|"
                         r"mode of study|duration|course code|ucas code|how to apply|book an open day|none|n/a|tbc|"
                         r"full[\s-]?time|part[\s-]?time|online|distance learning|on campus)$", re.I)


def campus_like(v):
    v = v.strip(" ,;:-")
    return (2 < len(v) < 60 and not CAMPUS_JUNK.match(v) and not label_at(v) and not MONTH_RE.search(v)
            and not money(v) and not re.search(r"\d+\s*(years?|months?|weeks?)|\d{1,2}/\d{1,2}|@|https?:", v, re.I))


def campuses_from(values):
    """All campus names in 'Location' values ('Campus A | Campus B | <next label>'), in page order."""
    out = []
    for v in values:
        for part in v.split(" | "):
            part = part.strip(" ,;:-")
            if re.fullmatch(r"(?i)and|or|&|plus|also|,", part):
                continue  # "CU Coventry | and | Coventry University"
            if not campus_like(part):
                break  # stop at the next label / non-campus text
            if part not in out:
                out.append(part)
    return out


def label_at(line):
    """(key, inline_value) if the line is a known label, else None."""
    if len(line) > 70:
        m = re.match(r"^([^:]{2,45}):\s*(.+)$", line)
        if not m:
            return None
        for k in OPT_KEYS:
            if LABELS[k].match(m.group(1).strip()):
                return k, m.group(2).strip()
        return None
    m = re.match(r"^([^:]{2,45}):\s*(.*)$", line)
    for k in OPT_KEYS:
        if LABELS[k].match(line):
            return k, ""
        if m and LABELS[k].match(m.group(1).strip()):
            return k, m.group(2).strip()
    return None


def option_groups(lines, soup):
    """Split repeated label blocks ('Mode of Study / Attendance / Start Date / Course Length' x N)
    into one dict per study option."""
    groups, cur = [], {}
    n = len(lines)
    i = 0
    while i < n:
        hit = label_at(lines[i])
        if not hit:
            i += 1
            continue
        key, val = hit
        j = i + 1
        if not val:
            parts = []
            # a value may span several lines: every start month on its own line, or
            # "3 years full-time" / "4 years sandwich" on consecutive lines
            while j < n and len(parts) < 12 and not label_at(lines[j]) and len(lines[j]) < 120:
                parts.append(lines[j])
                j += 1
                more = j < n and len(lines[j]) < 60 and not label_at(lines[j])
                if key == "intake":
                    more = more and bool(MONTH_RE.match(lines[j]) or NUMDATE_RE.match(lines[j]) or TERM_RE.match(lines[j]))
                elif key == "duration":
                    more = more and len(parts) < 4 and bool(DUR_RE.search(lines[j]))
                else:
                    more = False
                if not more:
                    break
            val = " | ".join(parts)
        if key in cur:
            groups.append(cur)
            cur = {}
        cur[key] = val
        i = j
    if cur:
        groups.append(cur)
    groups = [g for g in groups if len(g) >= 2 and ("intake" in g or "duration" in g)]
    # table layout: header row with Start / Duration / Mode columns
    for table in soup.find_all("table"):
        rows = [[c.get_text(" ", strip=True) for c in r.find_all(["th", "td"])] for r in table.find_all("tr")]
        if len(rows) < 2:
            continue
        cols = {}
        for ci, h in enumerate(rows[0]):
            for k in OPT_KEYS + ("location",):
                if LABELS[k].match(h) and k not in cols:
                    cols[k] = ci
        if len(cols) >= 2 and ("intake" in cols or "duration" in cols):
            for r in rows[1:]:
                g = {k: r[ci] for k, ci in cols.items() if ci < len(r)}
                if "attendance" not in g:
                    att = " ".join(c for c in r if re.search(r"full[\s-]?time|part[\s-]?time", c, re.I))
                    if att:
                        g["attendance"] = att
                if g:
                    groups.append(g)
    return groups


def part_time_only_months(text):
    """Months marked as part-time intakes, e.g. 'January (PT)' or 'April (part-time only)'."""
    out = []
    for seg in re.split(r"[,;|/]|\band\b", text or ""):
        if re.search(r"\(\s*(?:PT|part[\s-]?time(?: only)?)\s*\)|part[\s-]?time only", seg, re.I) \
                and not re.search(r"\bFT\b|full[\s-]?time", seg, re.I):
            out += [m for m in months_in(seg) if m not in out]
    return out


def build_options(groups, page_intakes, page_durs, title, page_mode_vals):
    opts = []
    for g in groups:
        att = g.get("attendance", "")
        dur_txt = g.get("duration", "")
        durs = parse_durations(dur_txt)
        pt = bool(re.search(r"part[\s-]?time", att, re.I)) and not re.search(r"full[\s-]?time", att, re.I)
        if not att and durs and all(d[1] for d in durs):
            pt = True
        months = months_in(g.get("intake", ""), numeric=True) or list(page_intakes)
        pt_only = part_time_only_months(g.get("intake", ""))  # "JANUARY (PT), APRIL (PT), SEPTEMBER"
        if pt_only and not pt:
            ft_months = [m for m in months if m not in pt_only]
            pt_dur = next((d[0] for d in durs if d[1]), None)
            if ft_months:
                months = ft_months
            opts.append({"months": pt_only, "duration": pt_dur, "mode": detect_mode(title, [g["mode"]] if g.get("mode")
                         else page_mode_vals, []), "part_time": True, "placement": False})
        mode = detect_mode(title, [g["mode"]] if g.get("mode") else page_mode_vals, [])
        ocampus = g.get("location", "").strip() if campus_like(g.get("location", "")) else ""
        if not durs:
            durs = [(d[0], d[1], d[2]) for d in page_durs if not d[1]][:1]
        ft_durs = [d for d in durs if not d[1]] or durs
        placement_txt = bool(re.search(r"placement|sandwich|industry|internship|work experience|extended|year abroad",
                                       dur_txt + " " + g.get("mode", ""), re.I))
        if not ft_durs:
            opts.append({"months": months, "duration": None, "mode": mode, "part_time": pt, "placement": False})
            continue
        base = ft_durs[0]
        extra = [d for d in ft_durs[1:] if d[2] and d[0] > base[0]]  # "3 years (4 with placement)"
        opts.append({"months": months, "duration": base[0], "mode": mode, "part_time": pt,
                     "placement": placement_txt and not extra, "campus": ocampus})
        for d in extra:
            opts.append({"months": months, "duration": d[0], "mode": mode, "part_time": pt, "placement": True,
                         "campus": ocampus})
    return opts


def intakes_from_text(lines):
    found = []
    ctx = re.compile(r"\b(start(?:s|ing)?|intakes?|entry|commenc\w*|begin\w*|enrol(?:l)?ment)\b", re.I)
    bad = re.compile(r"deadline|apply by|closing|close[sd]?\b|open day|webinar|results|published|updated|last reviewed|"
                     r"event|clearing|application window|decision|offer", re.I)
    for l in lines:
        if len(l) > 400 or not ctx.search(l) or bad.search(l):
            continue
        for m in re.finditer(ctx, l):
            window = l[m.start(): m.end() + 90]
            for mo in months_in(window):
                if mo not in found:
                    found.append(mo)
    return found


def explicit_intakes(lines):
    """Months written as '<Month> intake/start/entry' or 'intake: <Month>' anywhere on the page."""
    found = []
    bad = re.compile(r"deadline|apply by|closing|open day|webinar|event|clearing|decision", re.I)
    pat = re.compile(r"\b([A-Z][a-z]+)\s+(?:20\d\d\s+)?(?i:intake|start|entry|cohort)s?\b|"
                     r"\b(?i:intakes?|start dates?|entry points?)\s*(?i:is|are|in|:)\s*([A-Z][a-z]+(?:\s*(?:,|and|or|&)\s*[A-Z][a-z]+)*)")
    for l in lines:
        if len(l) > 300 or bad.search(l):
            continue
        for m in pat.finditer(l):
            for mo in months_in(m.group(1) or m.group(2) or ""):
                if mo not in found:
                    found.append(mo)
    return found


def split_awards(title):
    """'PgCert, PgDip, MSc Project Management' -> ['MSc Project Management'];
    'MSc/LLM Oil and Gas Law' -> ['MSc Oil and Gas Law', 'LLM Oil and Gas Law']."""
    t = re.sub(r"\s+", " ", title).strip()
    m = re.match(rf"^((?:(?:{PG_AWARDS}|{UG_AWARDS}|{DOC_AWARDS})(?:\s?\((?:Hons|Res)\))?\s*(?:/|,|&|and|or)\s*)+"
                 rf"(?:{PG_AWARDS}|{UG_AWARDS}|{DOC_AWARDS})(?:\s?\((?:Hons|Res)\))?)\s+(.+)$", t, re.I)
    if not m:
        return [t]
    awards = [a.strip() for a in re.split(r"\s*(?:/|,|&|\band\b|\bor\b)\s*", m.group(1)) if a.strip()]
    subject = m.group(2)
    main = [a for a in awards if not re.match(r"(pg\s?cert|pgcert|pg\s?dip|pgdip|postgraduate (cert|dip)|graduate (cert|dip))", a, re.I)]
    return [f"{a} {subject}" for a in (main or awards[-1:])]


def clean_title(t, site_name="", split=True):
    if not t:
        return ""
    t = re.sub(r"\s+", " ", t).strip()
    if split:  # "<title>" style strings carry " | Site | Section" suffixes
        parts = re.split(r"\s+[|–—]\s+|\s+-\s+(?=[A-Z][\w ]*$)", t)
        t = parts[0].strip() if parts else t
    if site_name and t.lower().endswith(site_name.lower()):
        t = t[: -len(site_name)].strip(" -|")
    t = re.sub(r"\s*\((?:[A-Z]{1,4}\d{2,4}|\d{3,5})\)\s*$", "", t)  # trailing course code
    t = re.sub(r"^(course|programme|program|study)\s*:\s*", "", t, flags=re.I)
    t = re.sub(r"^[A-Z]{1,4}\d{2,5}\s*[-–:]\s*", "", t)  # leading course code "DC599 - "
    t = re.sub(r"^(explore|discover|study)\s+(the\s+)?", "", t, flags=re.I)
    return t.strip(" -|:")


NOT_COURSE_TITLE = re.compile(r"\b(scholarships?|bursar(y|ies)|eligibility|funding opportunit\w*|open days?|webinars?|"
                              r"how to apply|applying to|entry requirements|tuition fees|studentships?|vacanc(y|ies)|"
                              r"alumni|guide to|faqs?|find a course|course search|a-z|clearing)\b|^(our |all |browse )?"
                              r"(undergraduate|postgraduate|masters?|research|online|taught)?\s*(courses|programmes|programs|degrees)$", re.I)


def pick_title(soup, lds, site_name):
    cands = []
    for d in lds:
        if re.search(r"Course|EducationalOccupationalProgram|Program", ld_type(d)) and d.get("name"):
            name = str(d["name"]).strip()
            award = str(d.get("educationalCredentialAwarded") or d.get("award") or "").strip()
            if isinstance(d.get("educationalCredentialAwarded"), dict):
                award = str(d["educationalCredentialAwarded"].get("name", ""))
            award = re.sub(r"\s*[|;]\s*", ", ", award)
            if award and award.lower() not in name.lower() and len(award) < 40:
                cands.append(clean_title(f"{award} {name}", site_name, split=False))
            cands.append(clean_title(name, site_name, split=False))
    h1 = soup.find("h1")
    if h1:
        cands.append(clean_title(h1.get_text(" ", strip=True), site_name, split=False))
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        cands.append(clean_title(og["content"], site_name))
    if soup.title and soup.title.string:
        cands.append(clean_title(soup.title.string, site_name))
    cands = [c for c in cands if c and 3 < len(c) < 200]
    for c in cands:
        if AWARD_ANY.search(c):
            return c, True
    return (cands[0] if cands else ""), False


def site_name_of(soup, lds):
    for d in lds:
        prov = d.get("provider") or d.get("publisher")
        if isinstance(prov, dict) and prov.get("name"):
            return str(prov["name"])
    for d in lds:
        if re.search(r"CollegeOrUniversity|EducationalOrganization|Organization", ld_type(d)) and d.get("name"):
            return str(d["name"])
    og = soup.find("meta", property="og:site_name")
    if og and og.get("content"):
        return og["content"].strip()
    return ""


def country_of(lds):
    for d in lds:
        for key in ("provider", "publisher"):
            prov = d.get(key)
            if isinstance(prov, dict) and isinstance(prov.get("address"), dict):
                c = prov["address"].get("addressCountry")
                loc = prov["address"].get("addressLocality")
                return (c.get("name") if isinstance(c, dict) else c), loc
        if isinstance(d.get("address"), dict):
            c = d["address"].get("addressCountry")
            return (c.get("name") if isinstance(c, dict) else c), d["address"].get("addressLocality")
    return None, None


def detect_mode(title, mode_vals, lines):
    blob = " ".join([title] + mode_vals).lower()
    if re.search(r"\bonline\b|distance learning|distance-learning|by distance|e-learning", title.lower()):
        return "Online"
    if mode_vals:
        mv = " ".join(mode_vals).lower()
        on = re.search(r"on[\s-]?campus|in[\s-]person|face[\s-]to[\s-]face|classroom|full[\s-]?time", mv)
        onl = re.search(r"\bonline\b|distance|remote", mv)
        if re.search(r"blended|hybrid", mv) and not on:
            return "Blended"
        if re.search(r"blended|hybrid", mv) and on and not re.search(r"on[\s-]?campus|in[\s-]person", mv):
            return "Blended"
        if onl and not on:
            return "Online"
    return "On campus"


def is_part_time_only(mode_vals, durations):
    mv = " ".join(mode_vals).lower()
    if mv and re.search(r"part[\s-]?time", mv) and not re.search(r"full[\s-]?time", mv):
        return True
    if durations and all(d[1] for d in durations):
        return True
    return False


INTL_CLOSED_RE = re.compile(r"not (?:currently )?(?:open|available|offered) to (?:applicants from )?[^.]{0,50}\binternational\b|"
                            r"international (?:students|applicants) (?:are|is)? ?not (?:eligible|accepted)|"
                            r"(?:only|solely) (?:open|available) to (?:uk|home|scottish|domestic|eu)|"
                            r"cannot accept (?:applications from )?international|no international (?:places|applications)|"
                            r"(?:unable|not able) to (?:accept|consider) (?:applications from )?international", re.I)


def fee_only(html, url=""):
    soup = BeautifulSoup(html, "lxml")
    tab = fee_from_tables(soup)
    lines = page_lines(soup)
    fee = fee_from_columns(lines) or tab or fee_from_lines(lines)
    if fee is None and re.search(r"international", url, re.I):
        fee = fee_from_label(lines)
    return fee


def fee_links(soup, url):
    from urllib.parse import urljoin, urlparse
    host = urlparse(url).netloc
    path = urlparse(url).path.rstrip("/")
    out = []
    for a in soup.find_all("a", href=True):
        txt = a.get_text(" ", strip=True)
        if not re.search(r"\b(fees?|tuition|costs?)\b", txt, re.I) or len(txt) > 60:
            continue
        if re.search(r"funding|scholarship|loan|accommodation|living", txt, re.I):
            continue
        h = urljoin(url, a["href"]).split("#")[0]
        p = urlparse(h)
        if not h.startswith("http") or re.search(r"\.(pdf|docx?)$", p.path, re.I) or h.rstrip("/") == url.rstrip("/"):
            continue
        if p.netloc.split(".", 1)[-1] != host.split(".", 1)[-1]:
            continue
        score = 0 if p.path.startswith(path) else (1 if path.rsplit("/", 1)[-1] in h else 2)
        out.append((score, h))
    return [h for sc, h in sorted(set(out)) if sc <= 1][:3]  # general fees pages give every course the same number


def extract(url, html):
    """Return list of course records (dicts) or [] when the page is not a course detail page."""
    soup = BeautifulSoup(html, "lxml")
    lds = jsonld(soup)
    site = site_name_of(soup, lds)
    has_ld_course = any(re.search(r"\bCourse\b|EducationalOccupationalProgram", ld_type(d)) for d in lds)
    title, title_has_award = pick_title(soup, lds, site)
    if re.search(r"type=foundation|with-foundation|foundation-year", url, re.I) and "foundation" not in title.lower():
        title += " with Foundation Year"
    crumbs = " ".join(a.get_text(" ", strip=True) for a in soup.select("[class*=breadcrumb] a, nav[aria-label*=readcrumb] a"))
    fee_tab = fee_from_tables(soup)
    flinks = fee_links(soup, url)
    lines = page_lines(soup)
    text = "\n".join(lines)
    low = text.lower()

    signals = sum(bool(re.search(p, low)) for p in [
        r"entry requirements?|admission requirements?|requirements for entry",
        r"duration|course length|years? full[\s-]?time|months? full[\s-]?time",
        r"tuition|fees? (?:and|&) funding|course fees?|international fees?",
        r"start dates?|intakes?|starting in|entry points?",
        r"ielts|english language",
        r"modules?|units? of study|curriculum|what you will study|course structure|subjects?",
        r"apply now|how to apply|apply online",
    ])
    if not (has_ld_course and signals >= 2) and not (title_has_award and signals >= 3):
        return []
    if NOT_COURSE_TITLE.search(title):
        return []
    # listing / subject pages link to lots of courses and rarely have fees for one course
    if not has_ld_course and len(re.findall(r"\bapply now\b", low)) > 8:
        return []

    vals = label_values(lines, soup)

    # --- intakes
    intakes = []
    for d in lds:
        for inst in (d.get("hasCourseInstance") or []) if isinstance(d.get("hasCourseInstance"), list) else [d.get("hasCourseInstance")]:
            if isinstance(inst, dict) and inst.get("startDate"):
                intakes += months_in(re.sub(r"\d{4}-(\d{2})-\d{2}", lambda m: MONTHS[int(m.group(1)) - 1], str(inst["startDate"])))
    for v in vals["intake"]:
        intakes += months_in(v, numeric=True)
    if not intakes:
        intakes = intakes_from_text(lines)
    intakes = list(dict.fromkeys(intakes))

    # --- duration(s)
    durs = []
    for v in vals["duration"]:
        durs += parse_durations(v)
    for d in lds:
        tw = d.get("timeRequired")
        if isinstance(tw, str):
            m = re.match(r"P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?", tw)
            if m and any(m.groups()):
                y, mo, w = (int(x or 0) for x in m.groups())
                durs.append((y * 12 + mo + round(w / 4.345), False, False))
    if not durs:
        for l in lines:
            if len(l) < 250 and re.search(r"full[\s-]?time|duration|course length|years?\s+(?:full|study)|months?\s+(?:full|study)", l, re.I):
                d = parse_durations(l)
                if d:
                    durs += d
                    if len(durs) >= 2:
                        break
    groups = option_groups(lines, soup)
    if groups:
        options = build_options(groups, intakes, durs, title, vals["mode"])
    else:
        ft = [d for d in durs if not d[1]] or durs
        base = next((d[0] for d in ft if not d[2]), ft[0][0] if ft else None)
        placement = next((d[0] for d in ft if d[2] and base and d[0] > base), None)
        mode0 = detect_mode(title, vals["mode"], lines)
        pt0 = is_part_time_only(vals["mode"] + vals["attendance"], durs)
        options = [{"months": intakes, "duration": base, "mode": mode0, "part_time": pt0, "placement": False}]
        if placement:
            options.append({"months": intakes, "duration": placement, "mode": mode0, "part_time": pt0, "placement": True})
    # months announced elsewhere on the page ("January Intake - International Applicants Only")
    have = {m for o in options for m in o["months"]}
    base_opts = [o for o in options if not o["part_time"] and o["mode"] == "On campus" and not o["placement"]] or options[:1]
    for mo in explicit_intakes(lines):
        if have and mo not in have and base_opts:
            options.append(dict(base_opts[0], months=[mo]))
    # options without a month are leftovers when other options carry the start months
    if any(o["months"] for o in options):
        options = [o for o in options if o["months"]]
    # an option without a duration is redundant when the same months/attendance already have one
    have_dur = {(tuple(o["months"]), o["part_time"], o["mode"]) for o in options if o["duration"]}
    options = [o for o in options if o["duration"] or (tuple(o["months"]), o["part_time"], o["mode"]) not in have_dur]
    # drop exact duplicates
    seen, uniq = set(), []
    for o in options:
        k = (tuple(o["months"]), o["duration"], o["mode"], o["part_time"], o["placement"], o.get("campus", ""))
        if k not in seen:
            seen.add(k)
            uniq.append(o)
    options = uniq

    # --- fee
    fee_guess, fee_source = False, ""
    fee = fee_from_columns(lines)
    if fee:
        fee_source = "international column on course page"
    elif fee_tab:
        fee, fee_source = fee_tab, "international row/column of fee table"
    else:
        fee = fee_from_lines(lines)
        if fee:
            fee_source = "international fee text on course page"
    if fee is None:
        fee = fee_from_label(lines)
        if fee:
            # a plain "Fees: x" (no Home/International split) - same fee for everyone or unclear
            fee_guess = not re.search(r"international", url, re.I)
            fee_source = "single fee on course page (no international label)" if fee_guess else "fee on international course page"

    intl_closed = bool(re.search(r"apprenticeship", title, re.I))
    for m in INTL_CLOSED_RE.finditer(text):
        sent = text[max(0, text.rfind(".", 0, m.start()) + 1): m.end()]
        # "This degree WITH FOUNDATION YEAR is not available to international students" on the main degree page
        # is about the other version, not this course
        other = [w for w in ("foundation year", "placement", "top-up", "part-time", "part time", "online", "apprenticeship")
                 if w in sent.lower() and w not in title.lower()]
        if not other:
            intl_closed = True
            break

    # --- other fields
    level = classify_level(title, " ".join([url, crumbs] + vals["level"]))
    campuses = campuses_from(vals["location"])
    for o in options:  # campuses named in start-date tables / study-option blocks
        if o.get("campus") and o["campus"] not in campuses:
            campuses.append(o["campus"])
    campus = campuses[0] if campuses else ""
    country, city = country_of(lds)

    canon = soup.find("link", rel="canonical")
    canonical = canon.get("href") if canon and canon.get("href", "").startswith("http") else ""
    records = []
    for name in split_awards(title):
        lvl = classify_level(name, " ".join([url, crumbs] + vals["level"])) if name != title else level
        records.append({
            "course": name, "url": url, "level": lvl, "fee": fee, "fee_is_guess": fee_guess, "fee_source": fee_source,
            "options": options, "canonical": canonical, "fee_links": flinks, "campuses": campuses, "intl_closed": intl_closed, "campus": campus, "city": city or "", "country": country or "", "site_name": site,
        })
    return records
