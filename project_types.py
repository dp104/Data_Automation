"""Project types: each has a standard output format (columns) and knows where such values usually live
on websites, so pages can be read without teaching (schema.org data, page tags, typical labels).

Per field:
  type     text | number | money | url | date | int
  jsonld   regexes over flattened schema.org keys, e.g. r"\\.offers(\\[\\d\\])?\\.price$"
  meta     page tag names
  labels   words used next to the value on pages ("Price", "Bedrooms")
  special  heading | page_url | computed
"""

PRODUCT = {
    "examples": ["https://www.brand-store.com/collections/headphones", "https://shop.example.com/category/laptops"],
    "id": "product", "name": "Product price tracking", "icon": "ph-tag",
    "blurb": "Product names, current prices, discounts and links from shops that allow it. Re-check later to see price changes.",
    "item_types": ["Product", "IndividualProduct", "ProductModel", "Book"],
    "tracking": True,
    "fields": [
        {"key": "serial", "header": "Sl.No", "type": "int", "special": "serial"},
        {"key": "name", "header": "Product Name", "type": "text", "special": "heading",
         "jsonld": [r"(^|\.)(Product|IndividualProduct|ProductModel|Book)\.name$"], "meta": ["og:title", "twitter:title"]},
        {"key": "brand", "header": "Brand", "type": "text",
         "jsonld": [r"\.brand\.name$", r"\.brand$", r"\.manufacturer\.name$"], "meta": ["product:brand", "og:brand"],
         "labels": ["brand", "manufacturer", "make"]},
        {"key": "price", "header": "Current Price", "type": "money",
         "jsonld": [r"\.offers(\[\d\])?\.price$", r"\.offers\.lowPrice$", r"\.offers(\[\d\])?\.priceSpecification\.price$"],
         "meta": ["product:price:amount", "og:price:amount", "price"],
         "labels": ["price", "our price", "sale price", "now", "offer price", "special price", "price (incl. tax)", "deal price"],
         "css": ["[itemprop=price]", ".price ins .amount", ".price .amount", ".price", ".product-price", ".price_color"]},
        {"key": "mrp", "header": "Original Price (MRP)", "type": "money",
         "jsonld": [r"\.offers\.highPrice$"],
         "labels": ["mrp", "list price", "was", "original price", "rrp", "regular price", "m.r.p."],
         "css": ["del .amount", ".price del", "s.price", ".was-price", ".compare-at-price", ".old-price"]},
        {"key": "discount", "header": "Discount %", "type": "number", "special": "computed"},
        {"key": "currency", "header": "Currency", "type": "text",
         "jsonld": [r"\.offers(\[\d\])?\.priceCurrency$", r"\.priceSpecification\.priceCurrency$"],
         "meta": ["product:price:currency", "og:price:currency"]},
        {"key": "availability", "header": "Availability", "type": "text",
         "jsonld": [r"\.offers(\[\d\])?\.availability$"], "labels": ["availability", "stock", "stock status"],
         "css": [".availability", ".stock"]},
        {"key": "rating", "header": "Rating", "type": "number",
         "jsonld": [r"\.aggregateRating\.ratingValue$"], "labels": ["rating"]},
        {"key": "reviews", "header": "Reviews", "type": "int",
         "jsonld": [r"\.aggregateRating\.(reviewCount|ratingCount)$"], "labels": ["reviews", "number of reviews", "ratings"]},
        {"key": "sku", "header": "SKU", "type": "text", "jsonld": [r"\.(sku|mpn|gtin13|gtin)$"], "labels": ["sku", "upc", "product code", "model"]},
        {"key": "seller", "header": "Shop", "type": "text", "special": "site"},
        {"key": "url", "header": "Product URL", "type": "url", "special": "page_url"},
        {"key": "checked", "header": "Last Checked", "type": "date", "special": "checked"},
        {"key": "change", "header": "Price Change", "type": "text", "special": "change"},
    ],
}

REAL_ESTATE = {
    "examples": ["https://www.property-portal.com/for-sale/london", "https://agency.example.com/properties?type=apartment"],
    "id": "realestate", "name": "Real estate price comparison", "icon": "ph-house-line",
    "blurb": "Property prices, locations, sizes and configurations from portals that allow it.",
    "item_types": ["RealEstateListing", "Residence", "House", "Apartment", "SingleFamilyResidence", "Accommodation", "Place", "Offer"],
    "fields": [
        {"key": "serial", "header": "Sl.No", "type": "int", "special": "serial"},
        {"key": "title", "header": "Property Title", "type": "text", "special": "heading",
         "jsonld": [r"(^|\.)(RealEstateListing|Residence|House|Apartment|SingleFamilyResidence|Accommodation)\.name$"], "meta": ["og:title"]},
        {"key": "ptype", "header": "Property Type", "type": "text", "labels": ["property type", "type", "category"],
         "jsonld": [r"\.(accommodationCategory|additionalType)$"]},
        {"key": "price", "header": "Price", "type": "money",
         "jsonld": [r"\.offers(\[\d\])?\.price$", r"\.price$"], "meta": ["og:price:amount", "product:price:amount"],
         "labels": ["price", "asking price", "rent", "sale price", "guide price", "offers over", "monthly rent"],
         "css": [".price", "[itemprop=price]", ".property-price", ".listing-price"]},
        {"key": "currency", "header": "Currency", "type": "text", "jsonld": [r"\.priceCurrency$"]},
        {"key": "location", "header": "Location", "type": "text",
         "jsonld": [r"\.address\.streetAddress$", r"\.address$"], "labels": ["address", "location", "locality"],
         "css": ["[itemprop=address]", ".address", ".location"]},
        {"key": "city", "header": "City", "type": "text", "jsonld": [r"\.address\.addressLocality$"], "labels": ["city", "town"]},
        {"key": "size", "header": "Size (sq ft)", "type": "number",
         "jsonld": [r"\.floorSize\.value$"], "labels": ["size", "area", "floor area", "carpet area", "built-up area", "super area", "sq ft", "sqft"]},
        {"key": "beds", "header": "Bedrooms", "type": "int",
         "jsonld": [r"\.(numberOfBedrooms|numberOfRooms)$"], "labels": ["bedrooms", "beds", "bhk", "bedroom"]},
        {"key": "baths", "header": "Bathrooms", "type": "int",
         "jsonld": [r"\.(numberOfBathroomsTotal|numberOfFullBathrooms)$"], "labels": ["bathrooms", "baths", "bathroom"]},
        {"key": "config", "header": "Configuration", "type": "text", "labels": ["configuration", "layout", "furnishing", "furnished"]},
        {"key": "agent", "header": "Agent / Seller", "type": "text",
         "jsonld": [r"\.(offers\.)?seller\.name$", r"\.(broker|agent|provider)\.name$"], "labels": ["agent", "agency", "seller", "listed by", "marketed by"]},
        {"key": "listed", "header": "Date Listed", "type": "date",
         "jsonld": [r"\.datePosted$", r"\.datePublished$"], "labels": ["listed", "added", "posted", "date listed"]},
        {"key": "url", "header": "Listing URL", "type": "url", "special": "page_url"},
    ],
}

JOBS = {
    "examples": ["https://jobs.lever.co/company-name", "https://job-boards.greenhouse.io/company-name", "https://www.company.com/careers"],
    "id": "jobs", "name": "Job listings", "icon": "ph-briefcase",
    "blurb": "Job titles, companies, locations, salaries, experience and apply links from career pages and permitted job boards.",
    "item_types": ["JobPosting"],
    "fields": [
        {"key": "serial", "header": "Sl.No", "type": "int", "special": "serial"},
        {"key": "title", "header": "Job Title", "type": "text", "special": "heading", "jsonld": [r"(^|\.)JobPosting\.title$"], "meta": ["og:title"]},
        {"key": "company", "header": "Company", "type": "text",
         "jsonld": [r"\.hiringOrganization\.name$", r"\.hiringOrganization$"], "meta": ["og:site_name"], "labels": ["company", "employer"]},
        {"key": "location", "header": "Location", "type": "text",
         "jsonld": [r"\.jobLocation(\[\d\])?\.address\.addressLocality$", r"\.jobLocation(\[\d\])?\.address\.addressRegion$",
                    r"\.jobLocationType$", r"\.applicantLocationRequirements\.name$"],
         "labels": ["location", "job location", "city", "office"], "css": [".location", "[class*=location]"]},
        {"key": "country", "header": "Country", "type": "text", "jsonld": [r"\.jobLocation(\[\d\])?\.address\.addressCountry(\.name)?$"]},
        {"key": "etype", "header": "Employment Type", "type": "text",
         "jsonld": [r"\.employmentType$"], "labels": ["employment type", "job type", "type", "commitment"]},
        {"key": "smin", "header": "Salary Min", "type": "money",
         "jsonld": [r"\.baseSalary\.value\.minValue$", r"\.baseSalary\.value\.value$", r"\.baseSalary\.minValue$"]},
        {"key": "smax", "header": "Salary Max", "type": "money", "jsonld": [r"\.baseSalary\.value\.maxValue$", r"\.baseSalary\.maxValue$"]},
        {"key": "scur", "header": "Salary Currency", "type": "text", "jsonld": [r"\.baseSalary\.currency$", r"\.salaryCurrency$"]},
        {"key": "salary", "header": "Salary (as shown)", "type": "text", "labels": ["salary", "pay", "compensation", "ctc", "pay range"]},
        {"key": "exp", "header": "Experience Required", "type": "text",
         "jsonld": [r"\.experienceRequirements(\.monthsOfExperience)?$"], "labels": ["experience", "experience required", "years of experience"]},
        {"key": "posted", "header": "Date Posted", "type": "date", "jsonld": [r"\.datePosted$"], "labels": ["posted", "date posted", "published"]},
        {"key": "deadline", "header": "Apply By", "type": "date", "jsonld": [r"\.validThrough$"], "labels": ["closing date", "apply by", "deadline"]},
        {"key": "url", "header": "Apply Link", "type": "url", "special": "page_url"},
        {"key": "source", "header": "Source", "type": "text", "special": "site"},
    ],
}

FLIGHTS = {
    "examples": ["https://permitted-travel-site.com/flights?from=DXB&to=BOM&date=2026-11-10"],
    "id": "flights", "name": "Flight price tracking", "icon": "ph-airplane-tilt",
    "blurb": "Fares, routes, dates, baggage rules and booking links from travel sites you have permission to use.",
    "item_types": ["Flight", "FlightReservation"], "tracking": True, "listing": True,
    "fields": [
        {"key": "serial", "header": "Sl.No", "type": "int", "special": "serial"},
        {"key": "airline", "header": "Airline", "type": "text", "jsonld": [r"\.(provider|airline)\.name$"], "labels": ["airline", "carrier", "operated by"],
         "css": [".airline", "[class*=airline]", "[class*=carrier]", "[data-airline]"]},
        {"key": "flight", "header": "Flight Number", "type": "text", "jsonld": [r"\.flightNumber$"], "labels": ["flight", "flight no", "flight number"]},
        {"key": "from", "header": "From", "type": "text", "jsonld": [r"\.departureAirport\.(iataCode|name)$"], "labels": ["from", "origin", "departure"]},
        {"key": "to", "header": "To", "type": "text", "jsonld": [r"\.arrivalAirport\.(iataCode|name)$"], "labels": ["to", "destination", "arrival"]},
        {"key": "date", "header": "Departure Date", "type": "date", "jsonld": [r"\.departureTime$"], "labels": ["date", "departure date", "travel date"]},
        {"key": "dep", "header": "Departure Time", "type": "text", "labels": ["departs", "departure time"]},
        {"key": "arr", "header": "Arrival Time", "type": "text", "jsonld": [r"\.arrivalTime$"], "labels": ["arrives", "arrival time"]},
        {"key": "duration", "header": "Duration", "type": "text", "labels": ["duration", "travel time"]},
        {"key": "stops", "header": "Stops", "type": "text", "labels": ["stops", "non-stop", "nonstop", "direct"]},
        {"key": "fare", "header": "Fare", "type": "money", "jsonld": [r"\.offers(\[\d\])?\.price$", r"\.totalPrice$"],
         "labels": ["fare", "price", "total", "from"], "css": [".price", ".fare", "[class*=price]"]},
        {"key": "currency", "header": "Currency", "type": "text", "jsonld": [r"\.priceCurrency$"]},
        {"key": "baggage", "header": "Baggage", "type": "text", "labels": ["baggage", "check-in", "cabin", "luggage"]},
        {"key": "url", "header": "Booking Link", "type": "url", "special": "page_url"},
        {"key": "checked", "header": "Last Checked", "type": "date", "special": "checked"},
        {"key": "change", "header": "Price Change", "type": "text", "special": "change"},
    ],
}

HOTELS = {
    "examples": ["https://permitted-hotel-site.com/search?city=Goa&checkin=2026-11-10&checkout=2026-11-12"],
    "id": "hotels", "name": "Hotel price comparison", "icon": "ph-bed",
    "blurb": "Room rates, ratings, amenities, cancellation terms and availability from hotel sites you have permission to use.",
    "item_types": ["Hotel", "LodgingBusiness", "Resort", "Motel", "Hostel", "BedAndBreakfast", "HotelRoom"], "tracking": True,
    "fields": [
        {"key": "serial", "header": "Sl.No", "type": "int", "special": "serial"},
        {"key": "name", "header": "Hotel Name", "type": "text", "special": "heading",
         "jsonld": [r"(^|\.)(Hotel|LodgingBusiness|Resort|Motel|Hostel|BedAndBreakfast)\.name$"], "meta": ["og:title"]},
        {"key": "location", "header": "Location", "type": "text",
         "jsonld": [r"\.address\.streetAddress$", r"\.address\.addressLocality$"], "labels": ["address", "location"]},
        {"key": "city", "header": "City", "type": "text", "jsonld": [r"\.address\.addressLocality$"]},
        {"key": "room", "header": "Room Type", "type": "text", "jsonld": [r"\.containsPlace\.name$"], "labels": ["room", "room type"]},
        {"key": "night", "header": "Price per Night", "type": "money",
         "jsonld": [r"\.offers(\[\d\])?\.price$", r"\.makesOffer(\[\d\])?\.price$"], "labels": ["per night", "price per night", "nightly", "price", "from"],
         "css": [".price", "[class*=price]"]},
        {"key": "total", "header": "Total Price", "type": "money", "labels": ["total", "total price", "total cost"]},
        {"key": "currency", "header": "Currency", "type": "text", "jsonld": [r"\.priceCurrency$", r"\.currenciesAccepted$"]},
        {"key": "rating", "header": "Rating", "type": "number",
         "jsonld": [r"\.aggregateRating\.ratingValue$", r"\.starRating\.ratingValue$"], "labels": ["rating", "score", "stars"]},
        {"key": "reviews", "header": "Reviews", "type": "int", "jsonld": [r"\.aggregateRating\.(reviewCount|ratingCount)$"], "labels": ["reviews"]},
        {"key": "amenities", "header": "Amenities", "type": "text", "join": True, "jsonld": [r"\.amenityFeature(\[\d\])?\.name$"], "labels": ["amenities", "facilities"]},
        {"key": "cancel", "header": "Cancellation", "type": "text", "labels": ["cancellation", "cancellation policy", "free cancellation", "refundable"]},
        {"key": "avail", "header": "Availability", "type": "text", "jsonld": [r"\.offers\.availability$"], "labels": ["availability", "rooms left"]},
        {"key": "checkin", "header": "Check-in", "type": "text", "jsonld": [r"\.checkinTime$"], "labels": ["check-in", "check in"]},
        {"key": "checkout", "header": "Check-out", "type": "text", "jsonld": [r"\.checkoutTime$"], "labels": ["check-out", "check out"]},
        {"key": "url", "header": "Booking Link", "type": "url", "special": "page_url"},
        {"key": "checked", "header": "Last Checked", "type": "date", "special": "checked"},
        {"key": "change", "header": "Price Change", "type": "text", "special": "change"},
    ],
}

UNIVERSITY = {
    "examples": ["https://www.rgu.ac.uk/", "https://www.coventry.ac.uk/london/"],
    "id": "university", "name": "University and course data", "icon": "ph-graduation-cap",
    "blurb": "Courses, levels, international tuition fees, intakes, durations and campuses from every course page of a university website, in the standard university sheet or your own.",
    "columns_preview": ["Course", "Level", "Mode of Education", "Duration", "First Year fee", "Intake", "Campus", "Scholarship", "Cost of living", "Minimum Deposit"],
    "page": "/university",
}

TYPES = {t["id"]: t for t in (UNIVERSITY, PRODUCT, REAL_ESTATE, JOBS, FLIGHTS, HOTELS)}
ORDER = ["university", "product", "realestate", "jobs", "flights", "hotels"]


def public(t):
    """What the page needs to show a type."""
    return {k: t.get(k) for k in ("id", "name", "icon", "blurb", "page", "tracking", "listing", "examples", "columns_preview")} | \
        {"columns": [f["header"] for f in t.get("fields", [])]}
