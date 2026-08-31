#!/usr/bin/env python3
"""
Builds index.html for the Woodward Academy Counseling Directory from
data/directory.csv (the synced, public-safe export of the "Public Ready"
Google Sheet tab).

Usage: python3 scripts/build_directory.py
Reads:  data/directory.csv, index_template.html
Writes: index.html
"""
import csv
import json
import re
import urllib.parse
from pathlib import Path
from datetime import date

REPO_ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = REPO_ROOT / "data" / "directory.csv"
TEMPLATE_PATH = REPO_ROOT / "index_template.html"
OUTPUT_PATH = REPO_ROOT / "index.html"

# ---- Specialty tag normalization -------------------------------------
# Maps a raw "Specialty(ies) List" token (as it appears in the sheet) to
# one or more display tags used on the public site. Tokens not found here
# pass through unchanged (title-cased as-is).
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
    "Residential": ["Residential"],
    "Bi-Lingual": ["Bi-Lingual"],
    "Art Therapy": ["Art Therapy"],
    "Summer Camps & Programs": ["Summer Camps & Programs"],
    "Play Therapy": ["Play Therapy"],
    "IOP": ["IOP"],
    "PHP": ["PHP"],
}

# ---- Provider type inference -------------------------------------
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


def normalize_specialties(raw):
    if not raw:
        return []
    out = []
    for token in [t.strip() for t in raw.split(",") if t.strip()]:
        out.extend(SPECIALTY_MAP.get(token, [token]))
    return out


def parse_virtual_inperson(text):
    text = (text or "").strip().lower()
    virtual = "virtual" in text
    inperson = "in person" in text
    return virtual, inperson


AGE_CUTOFF_RE = re.compile(r"(\d+)\s*(?:and older|and up|\+)", re.I)


def parse_ages_buckets(ages_text):
    t = (ages_text or "").strip().lower()
    if not t:
        return []
    if "all age" in t:
        return ["Children", "Teens", "Adults"]
    if "teens and adult" in t:
        return ["Teens", "Adults"]
    if t == "teens":
        return ["Teens"]
    if t == "adults":
        return ["Adults"]
    if t == "children":
        return ["Children"]
    m = AGE_CUTOFF_RE.search(t)
    if m:
        n = int(m.group(1))
        if n <= 11:
            return ["Children"]
        if n <= 17:
            return ["Teens"]
        return ["Adults"]
    # Unparseable / data-quality issue (typos, sheet-mangled dates, etc.)
    # Default to visible under every age filter rather than hiding the
    # provider entirely.
    return ["Children", "Teens", "Adults"]


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


def build_maps_url(address):
    if not address:
        return None
    query = address.replace("\n", ", ").replace("\t", " ")
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote_plus(query, safe="/")


def build_record(row):
    name = (row.get("Clinician/Group Name") or "").strip()
    if not name:
        return None
    address = (row.get("Address") or "").strip() or None
    area_text = (row.get("City/Area") or "").strip()
    cities = [c.strip() for c in area_text.split("\n") if c.strip()]
    city = cities[0] if cities else None
    insurance = (row.get("Insurance List") or "").strip() or None
    specialties_raw = row.get("Specialty(ies) List") or ""
    virtual, inperson = parse_virtual_inperson(row.get("Virtual Option"))
    ages = (row.get("Ages Served") or "").strip() or None
    website = (row.get("Website") or "").strip() or None
    if website and not website.startswith(("http://", "https://")):
        website = "https://" + website
    phone = (row.get("Phone") or "").strip() or None
    approaches = (row.get("Therapeutic Approaches") or "").strip() or None

    return {
        "name": name,
        "credentials": None,
        "providerType": infer_provider_type(name, insurance, specialties_raw),
        "specialties": normalize_specialties(specialties_raw),
        "insurance": insurance,
        "virtual": virtual,
        "inPerson": inperson,
        "city": city,
        "cities": cities,
        "area": area_text or None,
        "address": address,
        "website": website,
        "phone": phone,
        "ages": ages,
        "agesBuckets": parse_ages_buckets(ages),
        "approaches": approaches,
        "gender": parse_gender(row.get("Gender")),
        "mapsUrl": build_maps_url(address),
    }


def main():
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        records = [r for r in (build_record(row) for row in reader) if r]

    records.sort(key=lambda r: r["name"].lower())

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    data_json = json.dumps(records, ensure_ascii=False)
    output = template.replace("__DIRECTORY_DATA_JSON__", data_json)
    output = output.replace(
        'id="last-updated">August 2026',
        f'id="last-updated">{date.today().strftime("%B %Y")}',
    )

    OUTPUT_PATH.write_text(output, encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH} with {len(records)} providers.")


if __name__ == "__main__":
    main()
