from __future__ import annotations

from typing import Optional, Dict, Any, List, Tuple
import re
import httpx
import csv
import uuid
import io
from rapidfuzz import fuzz, process
from google.adk.agents import Agent

from fastapi import FastAPI
from ag_ui_adk import ADKAgent, add_adk_fastapi_endpoint
from google.adk import tools as adk_tools

NPI_API = "https://npiregistry.cms.hhs.gov/api/"
CMS_HOSPITAL_DATA_URL = "https://data.cms.gov/provider-data/sites/default/files/resources/893c372430d9d71a1c52737d01239d47_1753409109/Hospital_General_Information.csv"

_LABELS = [
    "general acute care hospital",
    "critical access hospital",
    "rehabilitation hospital",
    "psychiatric hospital",
    "ltac hospital",
    "ltc facility",
    "skilled nursing facility",
    "oncology clinic/center",
    "radiation oncology clinic/center",
]

_TAXONOMY_CODE_MAP: Dict[str, str] = {
    # Hospitals
    "282N00000X": "general acute care hospital",     # General Acute Care Hospital
    "282NC0060X": "critical access hospital",        # Critical Access Hospital
    "282E00000X": "ltac hospital",                   # Long Term Care Hospital (LTCH / LTAC)
    "283X00000X": "rehabilitation hospital",         # Rehabilitation Hospital
    "283Q00000X": "psychiatric hospital",            # Psychiatric Hospital
    "281P00000X": "general acute care hospital",     # Chronic Disease Hospital (often hospital class)

    # Helpful variants
    "282NR1301X": "general acute care hospital",     # Rural Acute Care Hospital
    "282NC2000X": "general acute care hospital",     # Children's Hospital
    "282NW0100X": "general acute care hospital",     # Women's Hospital

    # Long-term care facilities (non-hospital)
    "314000000X": "skilled nursing facility",        # SNF (also considered LTC)
    "313M00000X": "ltc facility",                    # Nursing Facility/Intermediate Care Facility
    "310400000X": "ltc facility",                    # Assisted Living Facility
    "310500000X": "ltc facility",                    # Alzheimer Center
    "311Z00000X": "ltc facility",                    # Custodial Care Facility

    # Oncology clinics/centers (not hospitals)
    "261QX0200X": "oncology clinic/center",          # Clinic/Center: Oncology
    "261QX0203X": "radiation oncology clinic/center" # Clinic/Center: Oncology, Radiation
}

# -------------------------------
# Fallback keyword mapping when codes are missing/unknown (order matters)
# -------------------------------
_TAXONOMY_KEYWORD_MAP: List[Tuple[re.Pattern, str]] = [
    # Oncology first so we don't miss it
    (re.compile(r"\bradiation\s+oncology\b", re.I), "radiation oncology clinic/center"),
    (re.compile(r"\boncology\b", re.I), "oncology clinic/center"),

    # SNF/LTC
    (re.compile(r"\bskilled\s+nursing\b", re.I), "skilled nursing facility"),
    (re.compile(r"\bnursing\s+facility\b", re.I), "ltc facility"),
    (re.compile(r"\bassisted\s+living\b", re.I), "ltc facility"),
    (re.compile(r"\bcustodial\s+care\b", re.I), "ltc facility"),

    # LTAC
    (re.compile(r"\blong[-\s]?term\s+care\s+hospital\b", re.I), "ltac hospital"),
    (re.compile(r"\blong[-\s]?term\s+acute\s+care\b", re.I), "ltac hospital"),

    # Other hospitals
    (re.compile(r"\bgeneral\s+acute\s+care\b", re.I), "general acute care hospital"),
    (re.compile(r"\bcritical\s+access\b", re.I), "critical access hospital"),
    (re.compile(r"\bpsychiatric\b", re.I), "psychiatric hospital"),
    (re.compile(r"\brehabilitation\b", re.I), "rehabilitation hospital"),
]

# -------------------------------
# Low-level helpers
# -------------------------------
def _nppes_by_name(hospital_name: str, state: Optional[str] = None) -> Dict[str, Any]:
    params = {
        "version": "2.1",
        "enumeration_type": "NPI-2",
        "organization_name": hospital_name,
        "limit": 200,
    }
    if state:
        params["state"] = state
    with httpx.Client(timeout=30) as client:
        r = client.get(NPI_API, params=params)
        r.raise_for_status()
        return r.json()

def _nppes_by_npi(npi: str) -> Dict[str, Any]:
    with httpx.Client(timeout=30) as client:
        r = client.get(NPI_API, params={"version": "2.1", "number": npi})
        r.raise_for_status()
        return r.json()

# -------------------------------
# CMS Hospital Data Helpers
# -------------------------------
_CMS_DATA_CACHE: List[Dict[str, Any]] = []

def _fetch_cms_hospital_data() -> List[Dict[str, Any]]:
    """Fetch CMS Hospital General Information CSV and return as list of dicts."""
    global _CMS_DATA_CACHE
    if _CMS_DATA_CACHE:
        return _CMS_DATA_CACHE

    try:
        with httpx.Client(timeout=60) as client:
            r = client.get(CMS_HOSPITAL_DATA_URL, follow_redirects=True)
            r.raise_for_status()

            # Parse CSV
            f = io.StringIO(r.text)
            reader = csv.DictReader(f)
            data = list(reader)

            if data:
                _CMS_DATA_CACHE = data
            return data
    except Exception as e:
        print(f"Error fetching CMS data: {e}")
        return []

def _search_hospital_by_name(hospital_name: str, state: Optional[str] = None, limit: int = 5) -> List[Dict[str, Any]]:
    """Search for hospitals by name using fuzzy matching."""
    data = _fetch_cms_hospital_data()
    if not data:
        return []

    # Filter by state first if provided (optimization)
    if state:
        state = state.upper().strip()
        data = [d for d in data if d.get("State") == state]

    if not data:
        return []

    names = [d.get("Facility Name", "") for d in data]

    # rapidfuzz.process.extract returns list of (match, score, index)
    results = process.extract(
        hospital_name.upper(),
        names,
        limit=limit,
        scorer=fuzz.token_set_ratio,
    )

    matches = []
    for match_name, score, idx in results:
        if score < 60:  # Minimum threshold
            continue

        record = data[idx]
        matches.append({
            "ccn": record.get("Facility ID"),
            "name": record.get("Facility Name"),
            "address": record.get("Address"),
            "city": record.get("City/Town"),
            "state": record.get("State"),
            "zip": record.get("ZIP Code"),
            "hospital_type": record.get("Hospital Type"),
            "match_score": score,
        })

    return matches

# -------------------------------
# Taxonomy classifier (fixes missing _classify_taxonomy / bad _is_hospital_code)
# -------------------------------
def _classify_taxonomy(entry: Dict[str, Any]) -> List[str]:
    """
    Given a single NPPES 'result' entry, return a list of human-readable
    facility kinds such as 'ltac hospital', 'ltc facility', 'skilled nursing facility',
    or 'oncology clinic/center'. Falls back to 'unknown' when nothing matches.
    """
    kinds: List[str] = []
    has_hospital = False
    has_onc = False

    for t in (entry.get("taxonomies") or []):
        code = (t.get("code") or "").upper()
        desc = (t.get("desc") or t.get("taxonomy_group") or "").strip()

        # 1) Exact code map wins
        mapped = _TAXONOMY_CODE_MAP.get(code)
        if mapped:
            if mapped not in kinds:
                kinds.append(mapped)
            if "oncology" in mapped:
                has_onc = True
            if "hospital" in mapped:
                has_hospital = True
            continue

        # 2) Keyword fallback on description/code text
        hay = f"{code} {desc}".strip()
        for rx, label in _TAXONOMY_KEYWORD_MAP:
            if rx.search(hay):
                if label not in kinds:
                    kinds.append(label)
                if "oncology" in label:
                    has_onc = True
                if "hospital" in label:
                    has_hospital = True
                break

    # If it looks like a hospital entity that also has oncology signals,
    # ensure we surface the oncology clinic tag (readable heuristic).
    if has_hospital and has_onc and "oncology clinic/center" not in kinds:
        kinds.append("oncology clinic/center")

    return kinds or ["unknown"]

def _pick_best_result_by_name(query_name: str, results: List[Dict[str, Any]]) -> Tuple[int, Dict[str, Any]]:
    """Pick the best NPPES org result by fuzzy name similarity."""
    if not results:
        raise ValueError("No results to choose from.")
    names = [(r.get("basic") or {}).get("legal_business_name", "") for r in results]
    if not names or all(not n for n in names):
        return 0, results[0]
    # RapidFuzz extractOne returns (match, score, index)
    _match, _score, best_idx = process.extractOne(query_name, names)
    if best_idx is None:
        best_idx = 0
    return best_idx, results[best_idx]

# -------------------------------
# Tool A: NPI -> facility type (+ basic info)
# -------------------------------
def get_facility_type_by_npi(npi: str) -> dict:
    """
    Returns facility type(s) for a 10-digit NPI using NPPES taxonomy.
    Response:
      { status, report, data?: { name, npi, kinds[], city, state } }
    """
    npi_clean = (npi or "").strip()
    if not re.fullmatch(r"\d{10}", npi_clean):
        return {"status": "error", "error_message": "Provide a valid 10-digit NPI."}

    try:
        data = _nppes_by_npi(npi_clean)
    except Exception as e:
        return {"status": "error", "error_message": f"NPPES lookup failed: {e}"}

    results = data.get("results") or []
    if not results:
        return {"status": "error", "error_message": f"No NPPES match for NPI {npi_clean}."}

    org = results[0]
    kinds = _classify_taxonomy(org)
    primary_addr = (org.get("addresses") or [{}])[0] or {}

    payload = {
        "name": (org.get("basic") or {}).get("legal_business_name"),
        "npi": org.get("number"),
        "kinds": kinds,  # ltac vs ltc, snf, oncology clinic distinctions
        "city": primary_addr.get("city"),
        "state": primary_addr.get("state"),
    }

    summary = (
        f"{payload['name']} (NPI {payload['npi']}) is classified as: "
        f"{', '.join(kinds)} in {payload['city']}, {payload['state']}."
    )
    return {"status": "success", "report": summary, "data": payload}

# -------------------------------
# Tool B: NPI -> related NPIs (siblings/subparts)
# -------------------------------
def get_related_npis(npi: str) -> dict:
    """
    Given a 10-digit NPI, returns a deduped list of related NPIs based on
    legal business name and parent organization LBN, with fuzzy match by name/city.
    Response:
      { status, report, data?: { query_npi, related_npis: [{npi,name,kinds[],city,is_subpart}] } }
    """
    npi_clean = (npi or "").strip()
    if not re.fullmatch(r"\d{10}", npi_clean):
        return {"status": "error", "error_message": "Provide a valid 10-digit NPI."}

    try:
        data = _nppes_by_npi(npi_clean)
    except Exception as e:
        return {"status": "error", "error_message": f"NPPES lookup failed: {e}"}

    results = data.get("results") or []
    if not results:
        return {"status": "error", "error_message": f"No NPPES match for NPI {npi_clean}."}

    org = results[0]
    basic = org.get("basic") or {}
    lbn = basic.get("legal_business_name")
    parent_lbn = basic.get("parent_organization_legal_business_name")
    target_city = ((org.get("addresses") or [{}])[0] or {}).get("city", "")

    # Expand by LBN and parent LBN
    candidates: List[Dict[str, Any]] = []
    for q in {lbn, parent_lbn} - {None, ""}:
        try:
            qdata = _nppes_by_name(q)
            candidates.extend(qdata.get("results") or [])
        except Exception:
            pass

    # Deduplicate by NPI
    seen, dedup = set(), []
    for r in candidates:
        n = r.get("number")
        if n and n not in seen:
            seen.add(n)
            dedup.append(r)

    # Fuzzy tighten (name + city) to keep close siblings/locations
    keep: List[Dict[str, Any]] = []
    for r in dedup:
        name_r = (r.get("basic") or {}).get("legal_business_name", "")
        score = fuzz.token_set_ratio(name_r, lbn or "")
        city_r = ((r.get("addresses") or [{}])[0] or {}).get("city", "")
        if target_city:
            score = (score + fuzz.partial_ratio(city_r or "", target_city or "")) / 2
        if score >= 70:
            keep.append(r)

    related = [
        {
            "npi": r.get("number"),
            "name": (r.get("basic") or {}).get("legal_business_name")
                    or (r.get("basic") or {}).get("organization_name"),
            "kinds": _classify_taxonomy(r),
            "city": ((r.get("addresses") or [{}])[0] or {}).get("city"),
            "is_subpart": (r.get("basic") or {}).get("organizational_subpart"),
        }
        for r in keep
    ]

    payload = {"query_npi": npi_clean, "related_npis": related}
    summary = f"Found {len(related)} related NPIs for {npi_clean}."
    return {"status": "success", "report": summary, "data": payload}

def get_ccn_by_hospital_name(hospital_name: str, state: Optional[str] = None) -> dict:
    """
    Look up hospital CCN (CMS Certification Number / Facility ID) by hospital name.
    Uses fuzzy matching to find the best matches.

    Response:
      { status, report, data?: { matches: [{ccn, name, city, state, hospital_type, match_score}] } }
    """
    if not hospital_name or not hospital_name.strip():
        return {"status": "error", "error_message": "Provide a hospital name."}

    try:
        matches = _search_hospital_by_name(hospital_name, state=state)
    except Exception as e:
        return {"status": "error", "error_message": f"Search failed: {e}"}

    if not matches:
        # Check if data is actually loaded
        if not _CMS_DATA_CACHE:
            # Since _search_hospital_by_name calls fetch, if cache is empty here, it failed.
            return {
                "status": "error",
                "error_message": "CMS Hospital Database could not be loaded. Please try again later.",
            }

        msg = f"No hospitals found matching '{hospital_name}'"
        if state:
            msg += f" in state {state}"
        return {"status": "success", "report": msg + ".", "data": {"matches": []}}

    top = matches[0]
    summary = (
        f"Found {len(matches)} matches. Top match: {top['name']} "
        f"(CCN: {top['ccn']}) in {top['city']}, {top['state']}."
    )
    return {"status": "success", "report": summary, "data": {"matches": matches}}

def get_facility_profile_by_npi(npi: str) -> dict:
    """
    One-call convenience wrapper:
    - Validates the NPI
    - Fetches classification/info (via get_facility_type_by_npi)
    - Fetches related NPIs (via get_related_npis)
    Response:
      {
        status,
        report,
        data?: {
          facility: { name, npi, kinds[], city, state },
          related:  { query_npi, related_npis: [...] }
        }
      }
    """
    npi_clean = (npi or "").strip()
    if not re.fullmatch(r"\d{10}", npi_clean):
        return {"status": "error", "error_message": "Provide a valid 10-digit NPI."}

    info = get_facility_type_by_npi(npi_clean)
    if info.get("status") != "success":
        return info  # bubble up the error

    rel = get_related_npis(npi_clean)
    # Even if related fails (rare), still return facility info
    data_out: Dict[str, Any] = {"facility": info.get("data")}
    if rel.get("status") == "success":
        data_out["related"] = rel.get("data")
        summary = (
            f"{info.get('report')} Related NPIs found: "
            f"{len(rel['data']['related_npis'])}."
        )
        return {"status": "success", "report": summary, "data": data_out}
    else:
        summary = f"{info.get('report')} No related NPIs returned."
        return {"status": "success", "report": summary, "data": data_out}

# -------------------------------
# Agent + FastAPI wiring
# -------------------------------
root_agent = Agent(
    name="hospital_npi_agent",
    model="gemini-2.5-flash",
    description=(
        "Given a 10-digit NPI, returns facility type via NPPES and related NPIs "
        "(siblings/subparts). Also supports CCN lookup by hospital name."
    ),
    instruction=(
        "If the user provides a 10-digit NPI and wants both facility info and related NPIs, "
        "call get_facility_profile_by_npi. "
        "If they only want the facility classification for that NPI, call get_facility_type_by_npi. "
        "If they only want related NPIs, call get_related_npis. "
        "If they want to find a hospital's CCN (CMS Certification Number) or Facility ID by name, "
        "call get_ccn_by_hospital_name. "
        "Prefer returning the tools' JSON (data field) along with a short summary. "
        "Surface whether a facility is an 'ltac hospital', an 'ltc facility', "
        "a 'skilled nursing facility', or includes 'oncology clinic/center' designations "
        "when applicable."
        "Always answer only the user's LATEST question."
        "Do NOT restate or list previous answers unless the user explicitly asks for it."
        "Keep answers focused on the current question."
        "If the user asks a completely new question, ignore earlier answers and respond fresh."
    ),
    tools=[get_facility_profile_by_npi, get_facility_type_by_npi, get_related_npis, get_ccn_by_hospital_name],
)

app = FastAPI(title="HLH Agent")

HLHAgent = ADKAgent(
    adk_agent=root_agent,
    app_name="hlh_app",
    user_id="demo users",
    session_timeout_seconds=3600,
    use_in_memory_services=True,
)

add_adk_fastapi_endpoint(app, HLHAgent, path="/")

if __name__ == "__main__":
    import os
    import uvicorn

    if not os.getenv("GOOGLE_API_KEY"):
        print("⚠️  Warning: GOOGLE_API_KEY environment variable not set!")
        print("   Set it with: export GOOGLE_API_KEY='your-key-here'")
        print("   Get a key from: https://makersuite.google.com/app/apikey")
        print()

    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
