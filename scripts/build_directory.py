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
    "LGBTQ+ Affirming": ["LGBTQ+ Affirming"],
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
    ("South metro (near main campus)", ["College Park", "East Point",
