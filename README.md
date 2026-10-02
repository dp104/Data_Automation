# University Course Scraper → Flyurdream Excel format

Give it any university website. It scans the whole site, finds every course page, then asks
which **education levels**, **modes** and **intakes** you want, and writes an Excel file in the
same format as `Robert Gordon University.xlsx` (one sheet per intake: Sep, Jan, Feb, ...).

## Project types (home page)

The home page (`/`) lists project types; each has its own standard sheet format (or use your own sheet):

| Type | Reads from | Notes |
|---|---|---|
| University and course data | `/university` (dedicated engine) | your university sheet formats |
| Product price tracking | schema.org Product data, price tags, "Price" labels, result cards | price history + changes on re-check |
| Real estate | RealEstateListing / Residence data, "Bedrooms / Area / Price" labels, cards | |
| Job listings | JobPosting data, career pages (Lever, Greenhouse...), cards | |
| Flight price tracking | Flight data, result cards with Departs/Arrives/Fare labels | permitted sites only |
| Hotel price comparison | Hotel / LodgingBusiness data, room rate labels, cards | permitted sites only |

Collection: listing page -> every result card -> "next page" (links or page buttons) -> each item's own page
(item data beats card data). Respects robots.txt (Disallow + Crawl-delay), 4 parallel requests, backs off
and reports when a site asks to slow down (429/403); never works around a refusal.
Code: `project_types.py` (formats + where values live), `type_engine.py` (reading), `projects.py`
(runs, price history in `cache/history.db`, export), pages `web/home.html`, `web/project.html`.

## Two modes

The page header has a switch: **University courses** (built-in rules for course, fee, intake,
campus data) and **Any website** (`/any`): products, jobs, listings, directories...

### Any website mode (no AI, no APIs)
1. Upload a sample sheet with a few real rows copied from the site.
2. Paste the page each sample row came from (1 is enough, 2 is more reliable).
3. The tool finds each sample value on those pages and remembers *where* it is, preferring the most
   stable source: built-in page data (schema.org, e.g. `Product.offers.price`), page tags (og:...),
   the value next to a label ("Price" -> "£20"), the main heading, a CSS element, or the page address.
   A rule is kept only if it reads the right value on every example page. You can pick a different
   spot from everything found on the page, type a fixed value, or leave the column blank.
4. It crawls the site for pages whose address looks like the examples (editable pattern) and shows
   rows live. 5. Download in exactly your format (+ review list of pages with missing values).

Site setups are saved in `profiles/`, collected rows in `cache/any/`.
Tested: books.toscrape.com (190/200 pages, all columns right on unseen pages), a WooCommerce shop
(price/SKU/category from built-in product data).

## Output formats (your own column layout)

Step 1 on the web page is **Output format**. Pick a saved format, or upload a **sample .xlsx** in the
layout you want (header row + a few example rows). The tool learns from it:

- the exact columns, their order and header text (even typos/trailing spaces), header styling, widths
- what each column is filled from (course, level, fee, intake, campus, ... or a fixed value / blank)
- how values are written: intake `September` / `Sep` / `September 2027` / `Sep-27`, duration `12` /
  `1 Year` / `12 Months`, level words such as `UG` / `PG`, formulas such as `=M2*0.34`
- one sheet per intake (sheets named like months) or a single sheet

You confirm the column mapping, then save the format by name. Exports use only those columns.
Anything the website cannot provide stays **blank** (or the fixed value you type); sample values are
never copied into real data. Formats live in `templates/` (JSON + the sample file for styling).

## Easiest: the web page

Double-click **University Scraper.command** on the Desktop (or run `./uniscrape-web`).
Your browser opens **http://localhost:8765** — paste the university link, press *Scan website*,
tick levels / modes / intakes, check the university details and press *Create Excel file*.
Keep the Terminal window open while using it; close it to stop the tool.

## Command line

```bash
cd ~/projects/University_Scraper
./uniscrape https://www.rgu.ac.uk/
```

It will:
1. read the site's sitemaps and crawl the site (course pages first)
2. extract, per course: name, level, mode, duration (months), international first-year fee, start months
3. show what it found and ask you to pick:
   - education levels (Undergraduate, Postgraduate, Research, DBA, Foundation, Diploma/Certificate, Short course/Other)
   - whether to include part-time options
   - mode (On campus / Online / Blended)
   - intakes (January ... December, plus `Unknown` for pages with no start month)
4. ask the fixed university values (name, country, campus, city, region, cost of living,
   deposit %, credibility interview, application fee, scholarship per level) — remembered per
   university in `configs/<domain>.json`, so next time just press Enter
5. save `~/Downloads/<University Name>.xlsx` and `<University Name> - review.csv` listing rows
   whose fee/duration/intake could not be read (check these by hand). An existing file is never
   overwritten — a new one is saved as `<University Name> - generated <date>.xlsx`.

The scan is cached in `cache/<domain>.json`; re-running with different choices is instant.
Use `--rescan` to fetch the site again.

## Options

| Option | Meaning |
|---|---|
| `--rescan` | ignore the cached scan |
| `--max-pages 8000` | crawl budget (raise for very large sites) |
| `--render auto\|always\|never` | headless Chrome for JavaScript pages. `auto` renders only pages that look empty |
| `--levels "Postgraduate,Undergraduate"` | skip the level question (`all` = everything) |
| `--intakes "September,January"` | skip the intake question |
| `--modes "On campus"` | skip the mode question |
| `--include-part-time` | include part-time study options |
| `--include-closed` | keep courses not open to international students |
| `--yes` | use saved/default university details without asking |
| `--out DIR` | output folder (default `~/Downloads`) |

## How it copes with different university site layouts

- **Discovery:** robots.txt + sitemaps + a priority crawl that follows course-looking links first.
- **Bot protection (Cloudflare etc.):** detected automatically; the tool switches to headless Chrome
  (one browser, several tabs in parallel, hard per-page timeout, auto-relaunch if it crashes).
- **Stops early** when 300 pages in a row yield no new course and only low-value links remain.
- **Home-only courses** ("not open to international students", apprenticeships) are excluded unless
  you say yes / pass `--include-closed`.
- **Terms → months:** Trimester 1/2/3 (AU: Mar/Jul/Nov), Semester A/B/C (UK: Sep/Jan/May), numeric
  start dates (21/09/2026) and "January intake" headings are all read.
- **Course pages:** detected by schema.org `Course` data or an award in the title (MSc, BA (Hons),
  LLM, PhD, Diploma...) together with course signals (entry requirements, fees, duration, modules).
- **Study options:** repeated blocks like *Mode / Attendance / Start date / Length* and start-date
  tables are split into separate options, so each intake gets its own duration
  (e.g. MSc Project Management: Jan = 16 months, Sep = 12, Sep with placement = 24).
- **International fee:** fee tables (International column/row), Home/International grids, and
  "International students: £x" text; the newest academic year is taken (first listed).
  Term fees (Fall + Winter) are summed to a yearly fee. If the course page has no fee it follows
  the page's *Fees* link.
- **Titles:** "PgCert, PgDip, MSc X" becomes "MSc X"; "MSc/LLM X" becomes two rows.
- **Placements:** "3 years (4 with placement)" gives an extra "(with placement)" row.

## Validation (Oct 2026)

| Site | Result vs hand-made sheet |
|---|---|
| Robert Gordon (rgu.ac.uk) | fee 100%, duration 98%, level 99% of rows still on the live site |
| Hertfordshire (herts.ac.uk, Cloudflare) | fee 99% (293/296), duration 97%, level 97% |
| Coventry, Conestoga (CA), DCU (IE), Deakin (AU) | spot-checked: courses, fees, intakes extracted |

## Limits

- Data is only as good as the site: some universities don't publish international fees or start
  months on course pages. Those rows go to the review CSV.
- Course search pages that load results only through a search API (no links, no sitemap) may need
  the listing URL passed directly: `./uniscrape https://uni.edu/course-search`.
- US universities often publish tuition centrally (per credit), not per programme — expect many
  review rows there.
- Bot-protected sites are slower (~50 pages/min): a large site can take 30–60 min. Re-runs use the cache.
- Minimum Deposit is written as `=M<row>*0.34` (change the fraction when asked).

## Install (new computer)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
./uniscrape-web          # opens http://localhost:8765
```
Team data (scans, settings, uploaded sheets, exports) lives in `cache/`, `configs/`, `templates/`,
`profiles/`, `projects/`, `outputs/` and is not committed (see `.gitignore`).
