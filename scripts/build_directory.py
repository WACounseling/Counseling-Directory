#!/usr/bin/env python3
"""
Builds index.html for the Woodward Academy Counseling Directory from
data/directory.csv (the synced, public-safe export of the "Public Ready"
Google Sheet tab).

Usage: python3 scripts/build_directory.py
Reads:  data/directory.csv, index_template.html, index.html (previous build,
        used only for the shrinkage guard)
Writes: index.html, plus a data-quality report to the GitHub Actions run
        summary (when run inside Actions) and to the console.

Safety: if the new provider count is much smaller than the previously
published count, the build stops without writing anything. To publish a
deliberate large removal, run the workflow manually from the Actions tab
with "Allow a large drop in providers" checked (sets ALLOW_SHRINK=true).
"""
import csv
import html
import json
import os
import re
import sys
import urllib.parse
from collections import defaultdict
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = REPO_ROOT / "data" / "directory.csv"
PLACES_PATH = REPO_ROOT / "data" / "ga-places.json"
TEMPLATE_PATH = REPO_ROOT / "index_template.html"
OUTPUT_PATH = REPO_ROOT / "index.html"

# Stop if the provider count drops by more than this share (or this many
# records, whichever is larger) compared with the live site.
SHRINK_LIMIT_PCT = 0.10
SHRINK_LIMIT_MIN = 5

# ---- Specialty tag normalization -------------------------------------
# Maps a raw "Specialty(ies) List" token (as it appears in the sheet) to
# one or more display tags used on the public site. Tokens not found here
# pass through unchanged and are listed in the quality report.
SPECIALTY_MAP = {
    "Addiction/Substance Use": ["Substance Use"],
    "Anxiety": ["Anxiety"],
    "Depression": ["Depression"],
    "Eating Disorders/Body Image": ["Eating Disorders", "Body Image"],
    "Behavioral Challenges": ["Behavioral Concerns"],
    "Family Changes": ["Family Changes"],
    "Grief & Loss": ["Grief/Loss"],
    "Psychoeducational Testing": ["Psychoeducational Testing"],
    "Social Skill Development": ["Social Skills"],
    "Stress Management": ["Stress Management"],
    "Trauma or PTSD": ["Trauma"],
    "ADHD": ["ADHD"],
    "Ander Management": ["Anger Management"],  # source typo, kept as alias
    "Anger Management": ["Anger Management"],
    "Developmental (ASD)": ["Autism Spectrum"],
    "Identity Development": ["Identity Exploration"],
    "Learning Differences": ["Learning Differences"],
    "OCD": ["OCD"],
    "Academic Support": ["Academic Support"],
    "Family Therapy & Parenting Support": ["Parenting Support"],
    "Self-Esteem": ["Self-Esteem"],
    "Self-Harm": ["Self-Harm"],
    "Adoption": ["Adoption"],
    "Psychiatrist": ["Medication Management"],
    "Crisis Intervention": ["Crisis Intervention"],
    "Residential": ["Residential Treatment"],
    "Bi-Lingual": ["Bilingual Services"],
    "Art Therapy": ["Art Therapy"],
    "Summer Camps & Programs": ["Summer Camps & Programs"],
    "Play Therapy": ["Play Therapy"],
    "IOP": ["Intensive Outpatient (IOP)"],
    "PHP": ["Partial Hospitalization (PHP)"],
}

# Tags that describe the kind of care offered rather than the concern.
# These go in the "Type of care" filter; everything else is a concern.
FREE_LINE_TAG = "Free Crisis or Support Line"
CARE_TAGS = {
    "Psychoeducational Testing",
    "Medication Management",
    "Intensive Outpatient (IOP)",
    "Partial Hospitalization (PHP)",
    "Residential Treatment",
    "Crisis Intervention",
    "Play Therapy",
    "Art Therapy",
    "Bilingual Services",
    "Summer Camps & Programs",
    FREE_LINE_TAG,
}

# ---- Area / region normalization ---------------------------------------
CITY_ALIASES = {
    "john's creek": "Johns Creek",
    "midtown atlanta": "Midtown",
    "virginia highlands": "Virginia-Highland",
    "poncey highlands": "Poncey-Highland",
    "poncey-highlands": "Poncey-Highland",
}

# Order here is the order regions appear in the "General area" menu.
REGIONS = [
    ("Intown Atlanta", ["Atlanta", "Buckhead", "Midtown", "Poncey-Highland",
                        "Virginia-Highland", "Grant Park", "Kirkwood"]),
    ("South metro (near main campus)", ["College Park", "East Point", "Hapeville",
                                        "Fayetteville", "Peachtree City", "Newnan"]),
    ("North Fulton", ["Sandy Springs", "Roswell", "Alpharetta", "Johns Creek", "Milton"]),
    ("DeKalb", ["Decatur", "Tucker", "Dunwoody", "Chamblee", "Brookhaven", "Scottdale"]),
    ("Cobb & Cherokee", ["Marietta", "Smyrna", "Kennesaw", "Woodstock", "Vinings"]),
    ("Gwinnett & Forsyth", ["Lawrenceville", "Norcross", "Duluth", "Suwanee",
                            "Sugar Hill", "Cumming"]),
    ("Outside metro Atlanta", ["Athens"]),
    ("Statewide or nationwide", ["Georgia", "Nationwide"]),
]
OTHER_REGION = "Other areas"
CITY_TO_REGION = {c.lower(): r for r, cities in REGIONS for c in cities}

# ---- Insurance categories ----------------------------------------------
# (label, keywords). Matched case-insensitively against the full insurance
# text after removing "(not ...)" asides. Order is the menu order.
INSURANCE_CATEGORIES = [
    ("Aetna", ["aetna"]),
    ("Anthem / Blue Cross Blue Shield", ["anthem", "blue cross", "bluecross", "bcbs"]),
    ("Cigna", ["cigna"]),
    ("UnitedHealthcare / Optum", ["united health", "unitedhealth", "uhc", "optum",
                                  "umr", "oxford", "surest"]),
    ("Humana", ["humana"]),
    ("Kaiser Permanente", ["kaiser"]),
    ("Ambetter", ["ambetter"]),
    ("Medicaid / PeachCare", ["medicaid", "amerigroup", "peachstate", "peach state",
                              "caresource", "wellcare", "peachcare"]),
    ("Medicare", ["medicare"]),
    ("TRICARE", ["tricare"]),
    ("Out of network (superbill)", ["out-of-network", "out of network", "superbill",
                                    "reimbursement"]),
    ("Private pay", ["private pay", "self-pay", "self pay", "does not accept insurance"]),
    ("Sliding scale", ["sliding"]),
    ("Free", ["free"]),
]
MOST_PLANS_KEYWORDS = ["most major", "most insurance", "wide variety"]
MOST_PLANS_LABEL = "Most major plans (verify yours)"
# Carriers a "most major plans" practice should also appear under.
COMMERCIAL = {"Aetna", "Anthem / Blue Cross Blue Shield", "Cigna",
              "UnitedHealthcare / Optum", "Humana", "Kaiser Permanente", "Ambetter"}

NOT_ASIDE_RE = re.compile(r"\(\s*not\b[^)]*\)?", re.I)

# ---- Provider type inference -------------------------------------
TESTING_TAG = "Psychoeducational Testing"
TESTING_TYPE = "Testing/Evaluation"
MD_RE = re.compile(r"\bM\.?D\.?\b")
PHD_RE = re.compile(r"\bPh\.?D\.?\b", re.I)
PSYD_RE = re.compile(r"\bPsy\.?D\.?\b", re.I)


def infer_provider_type(name, insurance_text, specialties_raw):
    insurance_text = insurance_text or ""
    if MD_RE.search(name):
        return "Psychiatrist"
    if PHD_RE.search(name) or PSYD_RE.search(name):
        return "Psychologist"
    if "Educational Consultant" in insurance_text or "Educational/Transition Consulting" in insurance_text:
        # Only treat as Educational Consultant when no clinical credential matched above
        return "Educational Consultant"
    if "Assessment Practice" in insurance_text:
        return "Testing/Evaluation"
    return "Therapist"


UNKNOWN_SPECIALTIES = defaultdict(list)


def normalize_specialties(raw, name=""):
    if not raw:
        return []
    out = []
    for token in [t.strip().strip('"') for t in raw.split(",") if t.strip().strip('"')]:
        if token not in SPECIALTY_MAP:
            UNKNOWN_SPECIALTIES[token].append(name)
        for tag in SPECIALTY_MAP.get(token, [token]):
            if tag not in out:
                out.append(tag)
    return out


def parse_virtual_inperson(text):
    text = (text or "").strip().lower()
    virtual = "virtual" in text
    inperson = "in person" in text
    return virtual, inperson


AGE_CUTOFF_RE = re.compile(r"(\d+)\s*(?:and older|and up|\+)", re.I)
AGE_RANGE_RE = re.compile(r"(\d+)\s*(?:-|to|through)\s*(\d+)", re.I)
UNPARSED_AGES = []


def buckets_for_range(lo, hi):
    out = []
    if lo <= 11:
        out.append("Children")
    if lo <= 17 and hi >= 12:
        out.append("Teens")
    if hi >= 18:
        out.append("Adults")
    return out


def parse_ages_buckets(ages_text, name=""):
    t = (ages_text or "").strip().lower()
    if not t:
        return []
    if "all age" in t:
        return ["Children", "Teens", "Adults"]
    if re.search(r"\bte[ae]ns? and (adult|older)", t):
        return ["Teens", "Adults"]
    if t in ("teens", "teen", "teem", "teems", "adolescents"):
        return ["Teens"]
    if t in ("adults", "adult"):
        return ["Adults"]
    if t in ("children", "kids"):
        return ["Children"]
    m = AGE_RANGE_RE.search(t)
    if m:
        return buckets_for_range(int(m.group(1)), int(m.group(2)))
    words = []
    if re.search(r"\b(child|children|kids?)\b", t):
        words.append("Children")
    if re.search(r"\b(te[ae]ns?|adolescents?)\b", t):
        words.append("Teens")
    if re.search(r"\badults?\b", t):
        words.append("Adults")
    if words and not AGE_CUTOFF_RE.search(t):
        return words
    m = AGE_CUTOFF_RE.search(t)
    if m:
        # "6 and older" serves children, teens AND adults.
        return buckets_for_range(int(m.group(1)), 120)
    # Unparseable: show under every age filter rather than hide, and report it.
    UNPARSED_AGES.append((name, ages_text))
    return ["Children", "Teens", "Adults"]


def tidy_ages(text):
    if not text:
        return None
    t = text.strip()
    low = t.lower()
    if "all age" in low:
        return "All ages"
    if low in ("teem", "teems", "teen"):
        return "Teens"
    # Sentence case: "Teens and Adults" -> "Teens and adults"
    return t[:1].upper() + t[1:].lower()


GENDER_FIX = {
    "Non-Bianary": "Non-Binary",  # source typo
}


def parse_gender(text):
    if not text:
        return []
    out = []
    for g in [x.strip() for x in text.split(",") if x.strip()]:
        out.append(GENDER_FIX.get(g, g))
    return out


def clean_address(address):
    """Collapse sheet line breaks/tabs into one readable line."""
    if not address:
        return None
    parts = [p.strip(" ,\t") for p in re.split(r"[\r\n]+", address) if p.strip(" ,\t")]
    one = ", ".join(parts)
    one = re.sub(r"[ \t]+", " ", one)
    one = re.sub(r"\s+,", ",", one)
    return one or None


def build_maps_url(address):
    if not address:
        return None
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote_plus(address)


UNMAPPED_CITIES = defaultdict(list)


def split_cities(area_text, name=""):
    raw = [c.strip() for c in re.split(r"[\r\n,/]+", area_text or "") if c.strip()]
    cities = []
    for c in raw:
        c = CITY_ALIASES.get(c.lower(), c)
        if c not in cities:
            cities.append(c)
    regions = []
    for c in cities:
        r = CITY_TO_REGION.get(c.lower())
        if not r:
            UNMAPPED_CITIES[c].append(name)
            r = OTHER_REGION
        if r not in regions:
            regions.append(r)
    return cities, regions


def insurance_display(text):
    """Readable insurance text for the card, or None when we don't know."""
    if not text:
        return None
    tokens = [t.strip() for t in re.split(r"[\r\n]+|,(?![^()]*\))", text) if t.strip()]
    if not tokens:
        return None
    if all(t.lower() == "various" for t in tokens):
        return None
    tokens = [t for t in tokens if t.lower() != "various"]
    return ", ".join(tokens)


def insurance_categories(text):
    if not text:
        return []
    t = NOT_ASIDE_RE.sub(" ", text).lower()
    cats = [label for label, kws in INSURANCE_CATEGORIES if any(k in t for k in kws)]
    if any(k in t for k in MOST_PLANS_KEYWORDS):
        cats.append(MOST_PLANS_LABEL)
    return cats


# ---- Readable names ------------------------------------------------------
# The sheet lists people as "Last, First, Credentials". Cards show
# "First Last, Credentials"; sorting still uses the sheet's order (last name).
CREDENTIALS = {
    "lpc", "lcsw", "lmft", "lmsw", "msw", "apc", "ncc", "rpt", "rpt-s", "lapc", "lamft",
    "phd", "psyd", "md", "do", "edd", "eds", "eds", "ms", "ma", "med", "mmsc", "np",
    "pmhnp", "aprn", "bcba", "cpcs", "ncsp", "lcsw-c", "lpcc", "pc", "llc", "pllc",
}
BUSINESS_WORDS = re.compile(
    r"\b(services|center|centre|group|counseling|psychology|psychological|institute|clinic|"
    r"associates|collective|practice|health|llc|inc|academy|consulting|partners)\b", re.I)


def is_credential(token):
    t = token.strip().strip(".,").replace(".", "").lower()
    return t in CREDENTIALS or (len(t) >= 2 and token.replace(".", "").replace("-", "").isupper())


def display_name(name):
    parts = [p.strip() for p in name.split(",")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return name
    last, given, rest = parts[0], parts[1], [p for p in parts[2:] if p]
    if BUSINESS_WORDS.search(last) or is_credential(given) or len(last.split()) > 3:
        return name  # a business name that happens to contain a comma
    words = given.split()
    trailing = []
    while len(words) > 1 and is_credential(words[-1]):
        trailing.insert(0, words.pop())
    creds = trailing + rest
    shown = " ".join(words + [last])
    return shown + (", " + ", ".join(creds) if creds else "")


# ---- Approximate locations for "near me" sorting ------------------------
# Uses the ZIP code in the address (or, failing that, the city) and the
# Georgia ZIP/city centers in data/ga-places.json. No outside service needed.
PLACES = {"zips": {}, "cities": {}}
CITY_ZIP_FALLBACK = {
    "sandy springs": "30328", "johns creek": "30097", "dunwoody": "30338", "milton": "30004",
    "brookhaven": "30319", "buckhead": "30305", "midtown": "30309", "virginia-highland": "30306",
    "poncey-highland": "30306", "grant park": "30312", "kirkwood": "30317", "vinings": "30339",
}
ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
LOCATION_ISSUES = []


def locate(address, cities, name):
    if address:
        zips = [z for z in ZIP_RE.findall(address) if z in PLACES["zips"]]
        if zips:
            return PLACES["zips"][zips[-1]], False
        if re.search(r"\d", address) and re.search(r"\bGA\b|Georgia", address):
            LOCATION_ISSUES.append(f"{name}: no valid ZIP code in `{address}`")
    for c in cities or []:
        key = c.lower()
        if key in CITY_ZIP_FALLBACK and CITY_ZIP_FALLBACK[key] in PLACES["zips"]:
            return PLACES["zips"][CITY_ZIP_FALLBACK[key]], True
        if key in PLACES["cities"]:
            return PLACES["cities"][key], True
    return None, False


def build_record(row):
    name = re.sub(r"\s+", " ", (row.get("Clinician/Group Name") or "")).strip()
    if not name:
        return None
    address = clean_address(row.get("Address"))
    cities, regions = split_cities(row.get("City/Area"), name)
    insurance_raw = (row.get("Insurance List") or "").strip() or None
    specialties_raw = row.get("Specialty(ies) List") or ""
    virtual, inperson = parse_virtual_inperson(row.get("Virtual Option"))
    ages_raw = (row.get("Ages Served") or "").strip() or None
    website = (row.get("Website") or "").strip() or None
    if website and not website.startswith(("http://", "https://")):
        website = "https://" + website
    phone = (row.get("Phone") or "").strip() or None
    approaches = (row.get("Therapeutic Approaches") or "").strip() or None

    tags = normalize_specialties(specialties_raw, name)
    if insurance_raw and re.search(r"free (crisis|help|nonprofit)", insurance_raw, re.I):
        tags.append(FREE_LINE_TAG)

    # A provider can have more than one type: anyone who offers testing also
    # counts as Testing/Evaluation, alongside their main type.
    primary_type = infer_provider_type(name, insurance_raw, specialties_raw)
    provider_types = [primary_type]
    if TESTING_TAG in tags and TESTING_TYPE not in provider_types:
        provider_types.append(TESTING_TYPE)

    loc, loc_approx = locate(address, cities, name)

    return {
        "name": name,
        "displayName": display_name(name),
        "loc": loc,
        "locApprox": loc_approx,
        "providerType": primary_type,
        "providerTypes": provider_types,
        "concerns": [t for t in tags if t not in CARE_TAGS],
        "care": [t for t in tags if t in CARE_TAGS],
        "insurance": insurance_display(insurance_raw),
        "insuranceCats": insurance_categories(insurance_raw),
        "virtual": virtual,
        "inPerson": inperson,
        "cities": cities,
        "regions": regions,
        "address": address,
        "website": website,
        "phone": phone,
        "ages": tidy_ages(ages_raw),
        "agesBuckets": parse_ages_buckets(ages_raw, name),
        "approaches": approaches,
        "gender": parse_gender(row.get("Gender")),
        "mapsUrl": build_maps_url(address),
        "_insuranceRaw": insurance_raw,
    }


def previous_count():
    """Provider count currently published in index.html, or None."""
    if not OUTPUT_PATH.exists():
        return None
    html = OUTPUT_PATH.read_text(encoding="utf-8")
    m = re.search(r'<script id="directory-data" type="application/json">\s*(.*?)\s*</script>', html, re.S)
    if not m:
        return None
    try:
        return len(json.loads(m.group(1)))
    except ValueError:
        return None


# Shared booking/directory sites, not a sign of a duplicate listing.
SHARED_SITES = {"psychologytoday.com", "care.headway.co", "headway.co", "helloalma.com",
                "growtherapy.com", "simplepractice.com", "zencare.co"}


def normalize_site(url):
    if not url:
        return None
    u = re.sub(r"^https?://(www\.)?", "", url.lower()).split("/")[0]
    return None if not u or u in SHARED_SITES else u


def quality_report(records, prev):
    lines = ["## Directory data check", ""]
    lines.append(f"**Providers:** {len(records)}" + (f" (was {prev})" if prev is not None else ""))
    lines.append("")

    def section(title, items):
        if not items:
            return
        lines.append(f"### {title} ({len(items)})")
        for i in items:
            lines.append(f"- {i}")
        lines.append("")

    section("Specialty values not recognized (shown as-is on the site)",
            [f"`{k}`: {', '.join(v)}" for k, v in sorted(UNKNOWN_SPECIALTIES.items())])
    section("Cities not assigned to a region (shown under \"Other areas\")",
            [f"`{k}`: {', '.join(v)}" for k, v in sorted(UNMAPPED_CITIES.items())])
    section("Ages Served values the site couldn't read (shown under every age)",
            [f"{n}: `{a}`" for n, a in UNPARSED_AGES])
    section("Neither in-person nor virtual (hidden by the Setting filter)",
            [r["name"] for r in records if not r["inPerson"] and not r["virtual"]])
    section("No City/Area", [r["name"] for r in records if not r["cities"]])
    section("No insurance information (blank or only \"Various\")",
            [r["name"] for r in records if not r["insurance"]])
    section("No phone", [r["name"] for r in records if not r["phone"]])
    section("No website", [r["name"] for r in records if not r["website"]])
    section("No Ages Served", [r["name"] for r in records if not r["ages"]])

    dupes = []
    for key, label in (("phone", "phone"), ("website", "website")):
        seen = defaultdict(list)
        for r in records:
            v = r.get(key)
            v = re.sub(r"\D", "", v) if key == "phone" and v else normalize_site(v) if v else None
            if v:
                seen[v].append(r["name"])
        for v, names in seen.items():
            if len(names) > 1:
                dupes.append(f"Same {label} ({v}): {'; '.join(names)}")
    section("Possible duplicates to check (may be intentional for group practices)", dupes)
    section("Addresses without a usable ZIP code (distance is estimated from the city, or not shown)",
            LOCATION_ISSUES)
    lines.extend(coverage_report(records))
    return "\n".join(lines)


THIN = 3  # fewer than this many options is flagged


def coverage_report(records):
    """Where options are thin, to guide recruiting. Uses only public data."""
    out = ["## Where options are thin", "",
           f"Counts of providers on the site. Anything under {THIN} is marked with \u26a0\ufe0f.", ""]

    def mark(n):
        return f"{n} \u26a0\ufe0f" if n < THIN else str(n)

    concerns = sorted({c for r in records for c in r["concerns"]})
    out += ["### Concerns by age group", "", "| Concern | Children | Teens | In person, teens |", "|---|---|---|---|"]
    for c in concerns:
        has = [r for r in records if c in r["concerns"]]
        kids = sum("Children" in r["agesBuckets"] for r in has)
        teens = [r for r in has if "Teens" in r["agesBuckets"]]
        out.append(f"| {c} | {mark(kids)} | {mark(len(teens))} | {mark(sum(r['inPerson'] for r in teens))} |")
    out.append("")

    care = sorted({c for r in records for c in r["care"]})
    out += ["### Types of care", "", "| Type of care | Providers | Teens |", "|---|---|---|"]
    for c in care:
        has = [r for r in records if c in r["care"]]
        out.append(f"| {c} | {mark(len(has))} | {mark(sum('Teens' in r['agesBuckets'] for r in has))} |")
    out.append("")

    out += ["### Areas", "", "| Area | In person | Medicaid / PeachCare | Psychiatry | Testing |", "|---|---|---|---|---|"]
    for region, _ in REGIONS:
        here = [r for r in records if region in r["regions"]]
        if not here:
            out.append(f"| {region} | {mark(0)} | {mark(0)} | {mark(0)} | {mark(0)} |")
            continue
        out.append("| {} | {} | {} | {} | {} |".format(
            region,
            mark(sum(r["inPerson"] for r in here)),
            mark(sum("Medicaid / PeachCare" in r["insuranceCats"] for r in here)),
            mark(sum("Psychiatrist" in r["providerTypes"] for r in here)),
            mark(sum(TESTING_TYPE in r["providerTypes"] for r in here))))
    out.append("")
    return out


# ---- "Spreadsheet last checked" date -------------------------------------
# The daily sync workflow records when it checked the spreadsheet in
# data/last-checked.json, and the page reads that file. The date written into
# the page here is only a fallback, used if that file can't be loaded.
def fallback_checked_date():
    d = date.today()
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def main():
    if PLACES_PATH.exists():
        PLACES.update(json.loads(PLACES_PATH.read_text(encoding="utf-8")))
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    records = [r for r in (build_record(row) for row in rows) if r]

    prev = previous_count()
    report = quality_report(records, prev)
    print(report)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as s:
            s.write(report + "\n")

    allow_shrink = os.environ.get("ALLOW_SHRINK", "").lower() == "true"
    if prev and not allow_shrink:
        drop = prev - len(records)
        if drop > max(SHRINK_LIMIT_MIN, prev * SHRINK_LIMIT_PCT):
            msg = (f"Provider count dropped from {prev} to {len(records)}. The site was NOT updated. "
                   "If this is intentional, run the workflow manually with "
                   "'Allow a large drop in providers' checked.")
            print(f"::error::{msg}")
            sys.exit(1)

    for r in records:
        r.pop("_insuranceRaw", None)

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    # "</" is escaped so no provider text can ever close the <script> tag early.
    data_json = json.dumps(records, ensure_ascii=False).replace("</", "<\\/")
    meta_json = json.dumps({
        "regions": [r for r, _ in REGIONS] + [OTHER_REGION],
        "insurance": [l for l, _ in INSURANCE_CATEGORIES] + [MOST_PLANS_LABEL],
        "commercial": sorted(COMMERCIAL),
        "mostPlans": MOST_PLANS_LABEL,
        "freeLineTag": FREE_LINE_TAG,
        "zips": PLACES["zips"],
        "cityRegion": {c: CITY_TO_REGION.get(c.lower(), OTHER_REGION)
                       for r in records for c in r["cities"]},
    }, ensure_ascii=False)
    output = template.replace("__DIRECTORY_DATA_JSON__", data_json)
    output = output.replace("__DIRECTORY_META_JSON__", meta_json)
    output = output.replace("__LAST_CHECKED__", html.escape(fallback_checked_date()))

    OUTPUT_PATH.write_text(output, encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH} with {len(records)} providers.")


if __name__ == "__main__":
    main()
