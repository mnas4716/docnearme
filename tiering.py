#!/usr/bin/env python3
"""
Apply the IMMEX prospect-tiering scheme to the expansion (net-new) records.

score_row / tier_for / PROCEDURAL / WEIGHTS are copied verbatim from the
Curbside IMMEX pipeline (pipeline/s4_tier.py) so expansion records are tiered on
exactly the same rules as the Healthshare base. The only added piece is
derive_recency(), which reconstructs the pipeline's early-career 'recency' flag
(Confirmed / Likely / Established / Unknown) from the fields the adapters
captured (fellowship year + bio evidence), since the expansion rows don't pass
through the pipeline's own profile extractor.
"""
from __future__ import annotations
import datetime as _dt
import re

CURRENT_YEAR = _dt.date.today().year
EARLY_CAREER_WINDOW = 10  # fellowship within N years counts as early-career

# --- verbatim from IMMEX pipeline/s4_tier.py --------------------------------
PROCEDURAL = {
    "Orthopaedic Surgeon", "Neurosurgeon", "Cardiothoracic Surgeon", "Vascular Surgeon",
    "General Surgeon", "Colorectal Surgeon", "Urologist", "Ophthalmologist (Eye Specialist)",
    "Otolaryngologist (ENT) - Head and Neck Surgeon", "Plastic Surgeon", "Breast Surgeon",
    "Bariatric (Obesity) Surgeon", "Endocrine Surgeon", "Hand Surgeon",
    "Oral and Maxillofacial Surgeon", "Laparoscopic Surgeon", "Gastroenterologist",
    "Cardiologist", "Interventional Radiologist", "Gynaecological Oncologist",
    "Obstetrician", "Paediatric Surgeon", "Upper GI Surgeon", "Thoracic Surgeon",
}
WEIGHTS = {
    "recency_confirmed": 50.0, "recency_likely": 25.0, "recency_unknown": 8.0,
    "years_since_fellowship": -3.0, "procedural": 8.0, "private_hospital": 6.0,
    "multi_practice": 4.0, "gap_scheme": 0.4, "enhanced_listing": 5.0,
    "has_fees": 3.0, "has_email": 10.0, "retired": -100.0,
}


def score_row(r: dict) -> tuple[float, dict]:
    s = 0.0
    sig: dict[str, float] = {}

    def add(k, v):
        nonlocal s
        if v:
            sig[k] = round(v, 1); s += v

    rec = r.get("recency") or "Unknown"
    if rec == "Confirmed":
        add("early_career_confirmed", WEIGHTS["recency_confirmed"])
    elif rec == "Likely":
        add("early_career_likely", WEIGHTS["recency_likely"])
    elif rec == "Unknown":
        add("unclaimed_listing_unknown", WEIGHTS["recency_unknown"])
    fy = r.get("fellowship_year")
    if fy:
        years = max(0, CURRENT_YEAR - fy)
        add("fellowship_recency", WEIGHTS["years_since_fellowship"] * min(years, 20))
    if (r.get("specialty_name") or "") in PROCEDURAL:
        add("procedural_specialty", WEIGHTS["procedural"])
    add("private_hospital", WEIGHTS["private_hospital"] * min(r.get("n_private_hosp") or 0, 3))
    add("multi_location", WEIGHTS["multi_practice"] * min(max((r.get("n_practices") or 1) - 1, 0), 4))
    add("gap_schemes", WEIGHTS["gap_scheme"] * min(r.get("gap_scheme_count") or 0, 20))
    if r.get("is_enhanced"):
        add("enhanced_listing", WEIGHTS["enhanced_listing"])
    if r.get("has_fees"):
        add("publishes_fees", WEIGHTS["has_fees"])
    if r.get("email"):
        add("contactable_by_email", WEIGHTS["has_email"])
    if r.get("is_retired"):
        add("retired", WEIGHTS["retired"])
    return s, sig


def tier_for(r: dict, score: float) -> str:
    if r.get("is_retired"):
        return "X"
    rec = r.get("recency") or "Unknown"
    if rec == "Confirmed":
        return "A" if r.get("email") else "B"
    if rec == "Likely":
        return "B" if r.get("email") else "C"
    if rec == "Unknown":
        return "C"
    return "D"
# --- end verbatim -----------------------------------------------------------

_SOFT = re.compile(r"\b(recently|newly|just (completed|finished)|commenced (practice|consulting)|"
                   r"new consultant|early[- ]career)\b", re.I)


def derive_recency(fellowship_year, evidence: str) -> str:
    """Reconstruct the pipeline's recency flag from captured fields."""
    if fellowship_year:
        return "Confirmed" if fellowship_year >= CURRENT_YEAR - EARLY_CAREER_WINDOW else "Established"
    if _SOFT.search(evidence or ""):
        return "Likely"
    return "Unknown"


def _count(field: str) -> int:
    field = (field or "").strip()
    return len([x for x in field.split(";") if x.strip()]) if field else 0


def tier_record(rec: dict) -> dict:
    """Set Tier / Score / Recency on one expansion record (23-col schema dict)."""
    fy = (rec.get("Fellowship year") or "").strip()
    fy = int(fy) if fy.isdigit() else None
    recency = derive_recency(fy, rec.get("Early-career evidence", ""))
    signal = {
        "recency": recency,
        "fellowship_year": fy,
        "specialty_name": (rec.get("Specialty") or "").strip(),
        "email": (rec.get("Published email") or "").strip(),
        "n_private_hosp": _count(rec.get("Private hospitals") or rec.get("Hospital affiliations")),
        "n_practices": 1 + _count(rec.get("Other locations")),
        "gap_scheme_count": 0, "is_enhanced": False, "has_fees": False, "is_retired": False,
    }
    score, _ = score_row(signal)
    rec["Recency"] = recency
    rec["Tier"] = tier_for(signal, score)
    rec["Score"] = round(score, 1)
    return rec
