"""FastAPI service for detecting thesis and dissertation records in an Alma SRU catalogue."""

from __future__ import annotations

import copy
import json
import logging
import math
import os
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.concurrency import run_in_threadpool
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, create_model, model_validator

load_dotenv()


ALMA_SRU_ENDPOINT = os.getenv(
    "ALMA_SRU_ENDPOINT", "https://eu.alma.exlibrisgroup.com/view/sru/33CCP_INST"
)
ALMA_SRU_VERSION = os.getenv("ALMA_SRU_VERSION", "1.2")
ALMA_RECORD_SCHEMA = os.getenv("ALMA_RECORD_SCHEMA", "unimarcxml")
USER_AGENT = os.getenv("ALMA_USER_AGENT", "humatheque-alma-check-api/0.1")
API_KEY = os.getenv("ALMA_API_KEY", os.getenv("API_KEY", ""))
RETRIED_STATUS = {429, 500, 502, 503, 504}

DEFAULT_TIMEOUT = float(os.getenv("ALMA_HTTP_TIMEOUT", "30.0"))
DEFAULT_RETRIES = int(os.getenv("ALMA_MAX_RETRIES", "2"))
DEFAULT_BACKOFF = float(os.getenv("ALMA_BACKOFF_BASE", "1.0"))
DEFAULT_MAX_RECORDS_PER_QUERY = int(os.getenv("ALMA_MAX_RECORDS_PER_QUERY", "10"))
DEFAULT_MAX_CANDIDATES = int(os.getenv("ALMA_MAX_CANDIDATES", "20"))
DEFAULT_MATCH_THRESHOLD = float(os.getenv("ALMA_MATCH_THRESHOLD", "0.78"))
DEFAULT_AMBIGUOUS_THRESHOLD = float(os.getenv("ALMA_AMBIGUOUS_THRESHOLD", "0.62"))
# Pivot extraction schemas, served by the humatheque-schemas API as {url}/{kind}.
EXTRACTION_SCHEMAS_URL = os.getenv(
    "EXTRACTION_SCHEMAS_URL", "https://humatheque-schemas.smartbiblia.fr/schemas"
).rstrip("/")

logger = logging.getLogger("humatheque-alma-check-api")

SRW_NS = {"srw": "http://www.loc.gov/zing/srw/"}

THESIS_NOTE_KEYWORDS = ("these", "theses", "thesis", "doctorat", "dissertation")
# The dissertation pivot schema lists the HDR among dissertations, not theses.
DISSERTATION_NOTE_KEYWORDS = ("memoire", "memoires", "master", "maitrise", "dea", "dess", "habilitation")
ACADEMIC_SUBJECT_KEYWORD = "theses et ecrits academiques"

SCORE_WEIGHTS = {
    "title": 0.40,
    "author": 0.24,
    "academic": 0.22,
    "year": 0.06,
    "language": 0.04,
    "context_or_advisor": 0.04,
}


@dataclass(frozen=True)
class DocumentProfile:
    """Strategy bundle that adapts the generic checker to a kind of academic document.

    Alma SRU exposes no document-type predicate comparable to Sudoc `tdo=y`, so the
    academic nature of each hit is deduced after parsing (see `classify_academic`) and
    profiles only declare which deduced `kind` values count as a match.

    `accepted_kinds` are the `academic.kind` values that count for this profile.
    Candidates with any other kind are still returned and scored, but they are reported
    as non-academic matches instead of feeding the decision. `academic_subject_only`
    (UNIMARC 608 "Thèses et écrits académiques" without a usable 328) is accepted by
    every profile because such records cannot be narrowed to thesis vs mémoire.
    """

    name: str
    description: str
    accepted_kinds: tuple[str, ...]


PROFILES: dict[str, DocumentProfile] = {
    "thesis": DocumentProfile(
        name="thesis",
        description=(
            "Doctoral thesis records: UNIMARC 328 mentions a thesis or doctorate, or the "
            "record only carries the 608 'Thèses et écrits académiques' subject."
        ),
        accepted_kinds=("thesis", "academic_subject_only"),
    ),
    "dissertation": DocumentProfile(
        name="dissertation",
        description=(
            "Master's mémoires, other dissertations and HDR: UNIMARC 328 mentions a mémoire, "
            "master, maîtrise or habilitation, or the record only carries the 608 'Thèses et écrits "
            "académiques' subject."
        ),
        accepted_kinds=("dissertation", "academic_subject_only"),
    ),
    "academic": DocumentProfile(
        name="academic",
        description="Any academic writing: thesis, dissertation, or 608-only evidence.",
        accepted_kinds=("thesis", "dissertation", "academic_subject_only"),
    ),
}


app = FastAPI(
    title="Humatheque Alma Check API",
    version="0.1.0",
    description=(
        "Search an institutional Alma SRU endpoint for academic records from "
        "VLM-extracted metadata, deduce from UNIMARC 328/608 whether each hit is a "
        "thesis or a dissertation, expose the holdings found in 930/995/AVA, and keep "
        "the Sudoc PPN as first-class information for the cataloguing decision. "
        "Document-kind specifics are encapsulated in `DocumentProfile` strategies so the "
        "same pipeline can serve theses, mémoires, and further profiles."
    ),
)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(api_key: str | None = Security(api_key_header)) -> None:
    if API_KEY and api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


class AlmaCheckRequest(BaseModel):
    # Extraction fields follow the pivot schemas (EXTRACTION_SCHEMAS_URL). Types are
    # deliberately more lenient than the schemas: nulls become defaults (see
    # `coerce_extraction`), list fields accept "A; B" strings, degree_type is free text.
    title: str = Field(..., description="Extracted main title.")
    subtitle: str | None = Field("", description="Extracted subtitle.")
    author: str | None = Field("", description="Extracted author name.")
    degree_type: str | None = Field("", description="Extracted degree or document type.")
    discipline: str | None = Field("", description="Extracted discipline.")
    volume: str | None = Field("", description="Volume number as an Arabic numeral; echoed, not matched.")
    granting_institution: str | None = Field("", description="Extracted granting institution.")
    co_tutelle_institutions: list[str] = Field(default_factory=list)
    doctoral_school: str | None = Field("", description="Extracted doctoral school.")
    defense_year: int | str | None = Field(None, description="Extracted defense year, yyyy.")
    advisor: list[str] = Field(default_factory=list, description="Extracted advisor names.")
    jury_president: str | None = Field("", description="Extracted jury president.")
    reviewers: list[str] = Field(default_factory=list, description="Extracted reviewer names.")
    committee_members: list[str] = Field(default_factory=list, description="Extracted committee member names.")
    language: str | None = Field("", description="ISO 639 three-letter language code.")
    confidence: float | None = Field(None, ge=0.0, le=1.0)

    profile: str = Field(
        "academic",
        description=(
            "Document profile key. Selects which deduced academic kinds count as a "
            "match. The `/check/thesis`, `/check/dissertation` and `/check/academic` "
            "routes force the matching profile regardless of this field."
        ),
    )

    max_records_per_query: int = Field(DEFAULT_MAX_RECORDS_PER_QUERY, ge=1, le=100)
    max_candidates: int = Field(DEFAULT_MAX_CANDIDATES, ge=1, le=100)
    match_threshold: float = Field(DEFAULT_MATCH_THRESHOLD, ge=0.0, le=1.0)
    ambiguous_threshold: float = Field(DEFAULT_AMBIGUOUS_THRESHOLD, ge=0.0, le=1.0)
    timeout: float = Field(DEFAULT_TIMEOUT, gt=0.0, le=120.0)
    retries: int = Field(DEFAULT_RETRIES, ge=0, le=10)
    backoff: float = Field(DEFAULT_BACKOFF, ge=0.0, le=30.0)
    include_unimarc_xml: bool = Field(False, description="Include raw record XML in candidate output.")

    @model_validator(mode="before")
    @classmethod
    def coerce_extraction(cls, data: Any) -> Any:
        # VLM extractions send null for missing values; a plain string is still accepted
        # for list fields ("A; B" or "A | B").
        if isinstance(data, dict):
            data = {key: value for key, value in data.items() if value is not None}
            for key in ("co_tutelle_institutions", "advisor", "reviewers", "committee_members"):
                if isinstance(data.get(key), str):
                    data[key] = split_people(data[key])
            if isinstance(data.get("volume"), int):
                data["volume"] = str(data["volume"])
        return data


def fetch_extraction_schemas() -> dict[str, dict[str, Any]]:
    """Load the pivot schemas once, at startup.

    They only document the request bodies (validation relies on AlmaCheckRequest), so
    an unreachable schemas API degrades the docs instead of preventing startup.
    """
    schemas = {}
    for kind in ("thesis", "dissertation"):
        url = f"{EXTRACTION_SCHEMAS_URL}/{kind}"
        try:
            with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=10) as response:
                schemas[kind] = json.load(response)
        except (OSError, ValueError) as exc:
            logger.warning("Extraction schema %s unavailable (%s): generic field docs used.", url, exc)
    return schemas


EXTRACTION_SCHEMAS = fetch_extraction_schemas()


def profile_request_model(kind: str) -> type[AlmaCheckRequest]:
    """AlmaCheckRequest documented with the field descriptions of a pivot schema."""
    fields: dict[str, Any] = {}
    properties = EXTRACTION_SCHEMAS.get(kind, {}).get("properties", {})
    for name, prop in properties.items():
        if name not in AlmaCheckRequest.model_fields:
            continue  # a field added upstream is ignored until the API maps it
        info = copy.copy(AlmaCheckRequest.model_fields[name])
        info.description = prop["description"]
        if "enum" in prop:
            info.examples = [value for value in prop["enum"] if value]
        fields[name] = (info.annotation, info)
    return create_model(f"{kind.capitalize()}CheckRequest", __base__=AlmaCheckRequest, **fields)


ThesisCheckRequest = profile_request_model("thesis")
DissertationCheckRequest = profile_request_model("dissertation")


@dataclass
class AlmaRecord:
    mms_id: str
    ppn: str | None = None
    ppn_source: str | None = None
    title: str = ""
    subtitle: str = ""
    authors: list[str] = field(default_factory=list)
    contributors: list[dict[str, str]] = field(default_factory=list)
    institutions: list[str] = field(default_factory=list)
    year: str | None = None
    language: str | None = None
    thesis: dict[str, Any] | None = None
    subjects: list[str] = field(default_factory=list)
    academic: dict[str, Any] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    identifiers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    unimarc_xml: str | None = None
    matched_queries: list[str] = field(default_factory=list)
    score: dict[str, float] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)


def normalize_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\x98", " ").replace("\x9c", " ")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def compact_text(value: Any) -> str:
    text = "" if value is None else str(value)
    # \x98/\x9c and <<...>> are UNIMARC non-filing markers; Alma serves both spellings.
    text = text.replace("\x98", "").replace("\x9c", "").replace("<<", "").replace(">>", "")
    return re.sub(r"\s+", " ", text).strip(" /,.;:")


def token_set(value: Any, min_len: int = 3) -> list[str]:
    seen = set()
    tokens = []
    for token in normalize_text(value).split():
        if len(token) < min_len or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def name_similarity(left: Any, right: Any) -> float:
    left_norm = normalize_text(left)
    right_norm = normalize_text(right)
    if not left_norm and not right_norm:
        return 1.0
    if not left_norm or not right_norm:
        return 0.0
    left_tokens = set(left_norm.split())
    right_tokens = set(right_norm.split())
    overlap = left_tokens & right_tokens
    token_f1 = 2 * len(overlap) / (len(left_tokens) + len(right_tokens)) if left_tokens and right_tokens else 0.0
    char_ratio = SequenceMatcher(None, left_norm, right_norm).ratio()
    compact_ratio = SequenceMatcher(None, left_norm.replace(" ", ""), right_norm.replace(" ", "")).ratio()
    return max(0.65 * token_f1 + 0.35 * char_ratio, compact_ratio)


def text_vector(value: str) -> dict[str, float]:
    vector: dict[str, float] = {}
    for token in token_set(value):
        vector[token] = vector.get(token, 0.0) + 1.0
    return vector


def cosine_similarity(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    common = set(left) & set(right)
    dot = sum(left[token] * right[token] for token in common)
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def lexical_similarity(left: str, right: str) -> float:
    left_norm = normalize_text(left)
    right_norm = normalize_text(right)
    if not left_norm or not right_norm:
        return 0.0
    cosine = cosine_similarity(text_vector(left_norm), text_vector(right_norm))
    ratio = SequenceMatcher(None, left_norm, right_norm).ratio()
    containment = 0.0
    if left_norm in right_norm or right_norm in left_norm:
        containment = min(len(left_norm), len(right_norm)) / max(len(left_norm), len(right_norm))
    return max(cosine, 0.65 * ratio + 0.35 * containment)


def split_people(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw_items = [str(item) for item in value]
    else:
        raw_items = re.split(r"[|;]", str(value))
    return [compact_text(item) for item in raw_items if compact_text(item)]


def build_context(payload: AlmaCheckRequest) -> str:
    people = [payload.author, payload.jury_president, *payload.advisor, *payload.reviewers, *payload.committee_members]
    parts: list[Any] = [
        payload.title,
        payload.subtitle,
        payload.degree_type,
        payload.discipline,
        payload.granting_institution,
        payload.doctoral_school,
        payload.defense_year,
    ]
    parts.extend(payload.co_tutelle_institutions)
    parts.extend(people)
    return " ".join(str(part) for part in parts if part)


def sru_url(query: str, maximum_records: int, start_record: int = 1) -> str:
    params = {
        "version": ALMA_SRU_VERSION,
        "operation": "searchRetrieve",
        "recordSchema": ALMA_RECORD_SCHEMA,
        "startRecord": str(start_record),
        "maximumRecords": str(maximum_records),
        "query": query,
    }
    return f"{ALMA_SRU_ENDPOINT}?{urlencode(params)}"


def request_xml(url: str, timeout: float, retries: int, backoff: float) -> tuple[ET.Element | None, str | None]:
    last_error = None
    for attempt in range(retries + 1):
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/xml"})
            with urlopen(request, timeout=timeout) as response:
                payload = response.read()
            return ET.fromstring(payload), None
        except HTTPError as exc:
            last_error = f"HTTP {exc.code}: {exc.reason}"
            if exc.code not in RETRIED_STATUS:
                break
        except URLError as exc:
            last_error = f"URL error: {exc.reason}"
        except ET.ParseError as exc:
            last_error = f"XML parse error: {exc}"
            break
        except Exception as exc:
            last_error = str(exc)
        if attempt < retries:
            time.sleep(backoff * (2**attempt))
    return None, last_error


def datafields(record: ET.Element, tag: str) -> list[ET.Element]:
    # Alma serves unimarcxml in the info:srw/schema/8/unimarcxml-v0.1 namespace, hence {*}.
    return [item for item in record.findall("{*}datafield") if item.attrib.get("tag") == tag]


def control(record: ET.Element, tag: str) -> str | None:
    item = next((node for node in record.findall("{*}controlfield") if node.attrib.get("tag") == tag), None)
    return compact_text(item.text) if item is not None and item.text else None


def subfields(datafield: ET.Element, code: str | None = None) -> list[str]:
    values = []
    for subfield in datafield.findall("{*}subfield"):
        if code is None or subfield.attrib.get("code") == code:
            if subfield.text and compact_text(subfield.text):
                values.append(compact_text(subfield.text))
    return values


def subfield_map(datafield: ET.Element) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for subfield in datafield.findall("{*}subfield"):
        code = subfield.attrib.get("code")
        value = compact_text(subfield.text) if subfield.text else ""
        if code and value and code not in mapping:
            mapping[code] = value
    return mapping


def first_subfield(datafield: ET.Element, code: str) -> str | None:
    values = subfields(datafield, code)
    return values[0] if values else None


def person_label(datafield: ET.Element) -> str | None:
    last = first_subfield(datafield, "a")
    first = first_subfield(datafield, "b")
    if last and first:
        return f"{first} {last}"
    return last or first


def parse_ppn(record: ET.Element) -> tuple[str | None, str | None]:
    """Extract the Sudoc PPN, which Alma keeps in 035$a as `(PPN)xxxxxxxxx`.

    The PPN never feeds the similarity score. It is the key a cataloguer needs to
    decide what to do with a hit, so it is reported on its own.
    """
    for item in datafields(record, "035"):
        for value in subfields(item, "a"):
            match = re.match(r"\(PPN\)\s*(\w+)", value, flags=re.IGNORECASE)
            if match:
                return match.group(1), "035$a (PPN) prefix"
    control_003 = control(record, "003")
    if control_003:
        match = re.search(r"sudoc\.fr/(\w+)", control_003, flags=re.IGNORECASE)
        if match:
            return match.group(1), "003 Sudoc URI"
    for item in datafields(record, "035"):
        for value in subfields(item, "a"):
            match = re.search(r"sudoc\.fr/(\w+)", value, flags=re.IGNORECASE)
            if match:
                return match.group(1), "035$a Sudoc URI"
    return None, None


def parse_year(record: ET.Element, thesis: dict[str, Any] | None) -> str | None:
    if thesis and thesis.get("year"):
        match = re.search(r"\b(18|19|20)\d{2}\b", str(thesis["year"]))
        if match:
            return match.group(0)
    for tag in ("214", "210"):
        for item in datafields(record, tag):
            date = first_subfield(item, "d")
            if date:
                match = re.search(r"\b(18|19|20)\d{2}\b", date)
                if match:
                    return match.group(0)
    coded = first_subfield(datafields(record, "100")[0], "a") if datafields(record, "100") else None
    if coded:
        match = re.search(r"[defghijklmnpqrstu](18|19|20)\d{2}", coded)
        if match:
            return match.group(0)[1:]
    return None


def parse_thesis(record: ET.Element) -> dict[str, Any] | None:
    for item in datafields(record, "328"):
        thesis = {
            "type": first_subfield(item, "b"),
            "discipline": first_subfield(item, "c"),
            "institution": first_subfield(item, "e"),
            "year": first_subfield(item, "d"),
            "raw": " ; ".join(subfields(item)),
        }
        if any(value for key, value in thesis.items() if key != "raw"):
            return thesis
    return None


def classify_academic(record: ET.Element, thesis: dict[str, Any] | None, subjects: list[str]) -> dict[str, Any]:
    """Deduce whether a hit is a thesis or a dissertation.

    Alma SRU has no document-type index, so the deduction replaces Sudoc's `tdo=y`
    predicate: UNIMARC 328 gives the degree wording, UNIMARC 608 the RAMEAU subject
    "Thèses et écrits académiques". A 608-only record stays `academic_subject_only`
    because nothing in it separates a doctoral thesis from a master's mémoire.
    """
    evidence: list[str] = []
    note_text = normalize_text(thesis.get("raw", "")) if thesis else ""
    note_type = normalize_text(thesis.get("type", "")) if thesis else ""

    has_thesis_word = any(word in note_text for word in THESIS_NOTE_KEYWORDS)
    has_dissertation_word = any(word in note_text for word in DISSERTATION_NOTE_KEYWORDS)
    academic_subjects = [value for value in subjects if ACADEMIC_SUBJECT_KEYWORD in normalize_text(value)]
    has_academic_subject = bool(academic_subjects)

    if thesis and (has_thesis_word or has_dissertation_word):
        evidence.append(f"328$b: {thesis.get('type') or thesis.get('raw')}")
    for subject in academic_subjects:
        evidence.append(f"608$a: {subject}")

    # A note reading "Mémoire de thèse" is rare; thesis wording wins when both appear,
    # except when the degree type itself is a mémoire.
    if has_dissertation_word and (not has_thesis_word or any(
        word in note_type for word in DISSERTATION_NOTE_KEYWORDS
    )):
        kind = "dissertation"
        confidence = 1.0
    elif has_thesis_word:
        kind = "thesis"
        confidence = 1.0
    elif has_academic_subject:
        kind = "academic_subject_only"
        confidence = 0.6
    else:
        kind = "not_academic"
        confidence = 0.0

    return {
        "is_academic": kind != "not_academic",
        "kind": kind,
        "confidence": confidence,
        "evidence": evidence,
        "has_thesis_note": bool(thesis),
        "has_academic_subject": has_academic_subject,
    }


ITEM_FIELD_MAPS = {
    # UNIMARC 930: Sudoc-style holding, $b library RCR, $a call number, $e collection, $j status.
    "930": {"library": "b", "call_number": "a", "location": "e", "status": "j", "item_id": "5"},
    # UNIMARC 995: local item, $B institution, $C library, $E/$M location, $K call number,
    # $F barcode, $R document type, $O circulation note.
    "995": {
        "library": "C",
        "institution": "B",
        "location": "E",
        "call_number": "K",
        "barcode": "F",
        "document_type": "R",
        "note": "O",
    },
    # Alma AVA: $q library name, $c location, $d call number, $e availability, $f/$g copies.
    "AVA": {
        "library": "q",
        "institution": "a",
        "location": "c",
        "call_number": "d",
        "availability": "e",
        "holding_id": "8",
    },
}


def parse_items(record: ET.Element) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for tag, mapping in ITEM_FIELD_MAPS.items():
        for datafield in datafields(record, tag):
            raw = subfield_map(datafield)
            item: dict[str, Any] = {"field": tag}
            for key, code in mapping.items():
                item[key] = raw.get(code)
            if tag == "AVA":
                item["copies"] = raw.get("f")
                item["unavailable_copies"] = raw.get("g")
            item["raw"] = raw
            if any(value for key, value in item.items() if key not in {"field", "raw"}):
                items.append(item)
    return items


def parse_record(record: ET.Element, include_unimarc_xml: bool = False) -> AlmaRecord:
    mms_id = control(record, "001") or ""
    ppn, ppn_source = parse_ppn(record)
    thesis = parse_thesis(record)
    title_field = datafields(record, "200")[0] if datafields(record, "200") else None
    title = first_subfield(title_field, "a") if title_field is not None else ""
    subtitle = first_subfield(title_field, "e") if title_field is not None else ""
    statement = subfields(title_field, "f") + subfields(title_field, "g") if title_field is not None else []
    authors = [label for item in datafields(record, "700") for label in [person_label(item)] if label]
    contributors = []
    for item in datafields(record, "701") + datafields(record, "702"):
        label = person_label(item)
        if label:
            contributors.append({"name": label, "role": first_subfield(item, "4") or ""})
    institutions = []
    for item in datafields(record, "711") + datafields(record, "712"):
        institution = first_subfield(item, "a")
        if institution:
            institutions.append(institution)

    subjects = [value for tag in ("606", "608") for item in datafields(record, tag) for value in subfields(item, "a")]
    urls = [url for item in datafields(record, "856") for url in subfields(item, "u")]
    identifiers = [value for tag in ("010", "017", "035") for item in datafields(record, tag) for value in subfields(item, "a")]
    notes = statement + [
        value for tag in ("300", "304", "314", "320", "330") for item in datafields(record, tag) for value in subfields(item, "a")
    ]
    lang_values = [value for item in datafields(record, "101") for value in subfields(item, "a")]

    return AlmaRecord(
        mms_id=mms_id,
        ppn=ppn,
        ppn_source=ppn_source,
        title=title or "",
        subtitle=subtitle or "",
        authors=authors,
        contributors=contributors,
        institutions=institutions,
        year=parse_year(record, thesis),
        language=lang_values[0] if lang_values else None,
        thesis=thesis,
        subjects=subjects,
        academic=classify_academic(record, thesis, subjects),
        items=parse_items(record),
        urls=urls,
        identifiers=identifiers,
        notes=notes,
        unimarc_xml=ET.tostring(record, encoding="unicode") if include_unimarc_xml else None,
    )


def response_records(root: ET.Element, include_unimarc_xml: bool) -> tuple[int, list[AlmaRecord], list[str]]:
    total_text = root.findtext("srw:numberOfRecords", namespaces=SRW_NS) or "0"
    diagnostics = [
        compact_text(" ".join(node.itertext()))
        for node in root.findall(".//{http://www.loc.gov/zing/srw/diagnostic/}diagnostic")
    ]
    records = []
    for data in root.findall(".//srw:recordData", SRW_NS):
        record = data.find("{*}record")
        if record is not None:
            parsed = parse_record(record, include_unimarc_xml=include_unimarc_xml)
            if parsed.mms_id:
                records.append(parsed)
    return int(total_text) if total_text.isdigit() else 0, records, diagnostics


def title_terms(title: str, max_terms: int = 7) -> str:
    stop = {
        "une",
        "des",
        "les",
        "aux",
        "dans",
        "pour",
        "avec",
        "sans",
        "sur",
        "sous",
        "question",
        "these",
        "doctorat",
        "memoire",
    }
    terms = [token for token in token_set(title, min_len=4) if token not in stop]
    return " ".join(terms[:max_terms])


def author_terms(author: str) -> str:
    tokens = token_set(author, min_len=3)
    if len(tokens) <= 2:
        return " ".join(tokens)
    return " ".join([tokens[0], tokens[-1]])


def cql_clause(index: str, terms: str) -> str:
    # Alma indexes support the `all` relation, which ANDs the words of the quoted term.
    return f'{index} all "{terms}"'


def build_queries(payload: AlmaCheckRequest, profile: DocumentProfile) -> list[str]:
    main_title = title_terms(" ".join([payload.title, payload.subtitle]))
    short_title = title_terms(payload.title, max_terms=4)
    author = author_terms(payload.author)
    context_terms = " ".join(
        token_set(
            " ".join(
                [
                    payload.discipline,
                    payload.granting_institution,
                    payload.doctoral_school,
                    str(payload.defense_year or ""),
                ]
            ),
            min_len=4,
        )[:6]
    )

    title_index = "alma.title"
    creator_index = "alma.creator"
    keyword_index = "alma.all_for_ui"

    queries = []
    if main_title and author:
        queries.append(f"{cql_clause(title_index, main_title)} and {cql_clause(creator_index, author)}")
    if short_title and author:
        queries.append(f"{cql_clause(title_index, short_title)} and {cql_clause(creator_index, author)}")
    if main_title:
        queries.append(cql_clause(title_index, main_title))
    if short_title and context_terms:
        queries.append(f"{cql_clause(title_index, short_title)} and {cql_clause(keyword_index, context_terms)}")
    if author and short_title:
        queries.append(f"{cql_clause(creator_index, author)} and {cql_clause(keyword_index, short_title)}")
    if short_title:
        queries.append(cql_clause(keyword_index, " ".join([short_title, author]).strip()))

    deduped = []
    seen = set()
    for query in queries:
        if query and query not in seen:
            seen.add(query)
            deduped.append(query)
    return deduped


def matches_profile(record: AlmaRecord, profile: DocumentProfile) -> bool:
    return record.academic.get("kind") in profile.accepted_kinds


def search_alma(
    query: str,
    max_records: int,
    timeout: float,
    retries: int,
    backoff: float,
    include_unimarc_xml: bool,
) -> dict[str, Any]:
    url = sru_url(query, max_records)
    root, error = request_xml(url, timeout, retries, backoff)
    if error or root is None:
        return {"query": query, "url": url, "total_found": 0, "records": [], "error": error, "diagnostics": []}
    total, records, diagnostics = response_records(root, include_unimarc_xml)
    for record in records:
        record.matched_queries.append(query)
    return {
        "query": query,
        "url": url,
        "total_found": total,
        "records": records,
        "error": None,
        "diagnostics": diagnostics,
    }


def score_candidate(payload: AlmaCheckRequest, record: AlmaRecord) -> None:
    expected_title = " : ".join(part for part in [payload.title, payload.subtitle] if part)
    record_title = " : ".join(part for part in [record.title, record.subtitle] if part)
    title_score = lexical_similarity(expected_title, record_title)

    author_score = max((name_similarity(payload.author, author) for author in record.authors), default=0.0)
    people = [item["name"] for item in record.contributors]
    advisor_score = max((name_similarity(advisor, person) for advisor in payload.advisor for person in people), default=0.0)

    academic_score = float(record.academic.get("confidence", 0.0))

    expected_year = str(payload.defense_year or "")
    year_score = 1.0 if expected_year and record.year == expected_year else 0.0
    language_score = 1.0 if payload.language and record.language == payload.language else 0.0

    thesis_text = " ".join(
        [
            record.thesis.get("raw", "") if record.thesis else "",
            " ".join(record.institutions),
            " ".join(record.subjects),
            " ".join(record.notes),
        ]
    )
    context_score = lexical_similarity(build_context(payload), " ".join([record_title, thesis_text, " ".join(people)]))

    final = (
        SCORE_WEIGHTS["title"] * title_score
        + SCORE_WEIGHTS["author"] * author_score
        + SCORE_WEIGHTS["academic"] * academic_score
        + SCORE_WEIGHTS["year"] * year_score
        + SCORE_WEIGHTS["language"] * language_score
        + SCORE_WEIGHTS["context_or_advisor"] * max(context_score, advisor_score)
    )
    bibliographic = (final - SCORE_WEIGHTS["academic"] * academic_score) / (1.0 - SCORE_WEIGHTS["academic"])
    record.score = {
        "final": round(final, 4),
        "bibliographic": round(bibliographic, 4),
        "title": round(title_score, 4),
        "author": round(author_score, 4),
        "academic": round(academic_score, 4),
        "year": round(year_score, 4),
        "language": round(language_score, 4),
        "context": round(context_score, 4),
        "advisor": round(advisor_score, 4),
    }
    record.evidence = {
        "record_title": record_title,
        "authors": record.authors,
        "contributors": record.contributors,
        "institutions": record.institutions,
        "thesis": record.thesis,
        "academic_evidence": record.academic.get("evidence", []),
        "ppn_source": record.ppn_source,
        "matched_queries": record.matched_queries,
    }


def candidate_to_json(record: AlmaRecord, include_unimarc_xml: bool, profile: DocumentProfile) -> dict[str, Any]:
    payload = {
        "source": "alma",
        "mms_id": record.mms_id,
        "ppn": record.ppn,
        "ppn_source": record.ppn_source,
        "has_ppn": bool(record.ppn),
        "sudoc_url": f"https://www.sudoc.fr/{record.ppn}" if record.ppn else None,
        "title": " : ".join(part for part in [record.title, record.subtitle] if part) or None,
        "authors": record.authors,
        "contributors": record.contributors,
        "institutions": record.institutions,
        "year": record.year,
        "language": record.language,
        "thesis": record.thesis,
        "subjects": record.subjects,
        "academic": record.academic,
        "counts_for_profile": matches_profile(record, profile),
        "items": record.items,
        "items_count": len(record.items),
        "has_items": bool(record.items),
        "item_fields_present": sorted({item["field"] for item in record.items}),
        "urls": record.urls,
        "identifiers": record.identifiers,
        "score": record.score,
        "evidence": record.evidence,
    }
    if include_unimarc_xml:
        payload["unimarc_xml"] = record.unimarc_xml
    return payload


def status_for_candidates(
    ranked: list[AlmaRecord],
    profile: DocumentProfile,
    match_threshold: float,
    ambiguous_threshold: float,
) -> tuple[str, AlmaRecord | None, float]:
    """Decide on the in-profile candidates, then report off-profile evidence.

    Off-profile candidates are judged on `score["bibliographic"]`, the title/author
    proximity without the academic component, because their academic component is 0 by
    construction and would otherwise keep them under every threshold.
    """
    in_profile = [record for record in ranked if matches_profile(record, profile)]
    off_profile = [record for record in ranked if not matches_profile(record, profile)]
    if in_profile and in_profile[0].score["final"] >= match_threshold:
        return "academic_record_found", in_profile[0], in_profile[0].score["final"]
    if in_profile and in_profile[0].score["final"] >= ambiguous_threshold:
        return "ambiguous_academic_candidate", in_profile[0], in_profile[0].score["final"]
    if off_profile and off_profile[0].score["bibliographic"] >= match_threshold:
        return "off_profile_match_only", None, 0.0
    return "no_academic_record_found", None, in_profile[0].score["final"] if in_profile else 0.0


def decision_support(ranked: list[AlmaRecord], profile: DocumentProfile, best: AlmaRecord | None) -> dict[str, Any]:
    """PPN view of the result set, kept out of the score but first in the decision.

    Whether a matching Alma record already carries a Sudoc PPN decides what the
    cataloguer does next, so it is reported separately from the similarity ranking.
    """
    in_profile = [record for record in ranked if matches_profile(record, profile)]
    with_ppn = [record for record in in_profile if record.ppn]
    return {
        "best_candidate_ppn": best.ppn if best else None,
        "best_candidate_has_ppn": bool(best and best.ppn),
        "academic_candidates_with_ppn": len(with_ppn),
        "academic_candidates_without_ppn": len(in_profile) - len(with_ppn),
        "ppns": [
            {
                "ppn": record.ppn,
                "mms_id": record.mms_id,
                "sudoc_url": f"https://www.sudoc.fr/{record.ppn}",
                "ppn_source": record.ppn_source,
                "score": record.score.get("final"),
                "academic_kind": record.academic.get("kind"),
                "items_count": len(record.items),
            }
            for record in with_ppn
        ],
    }


def check_alma(payload: AlmaCheckRequest) -> dict[str, Any]:
    profile = PROFILES.get(payload.profile)
    if profile is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown profile {payload.profile!r}. Available profiles: {sorted(PROFILES)}.",
        )

    queries = build_queries(payload, profile)
    if not queries:
        raise HTTPException(status_code=400, detail="Unable to build Alma query from submitted metadata.")

    searches = []
    candidates_by_id: dict[str, AlmaRecord] = {}
    for query in queries:
        result = search_alma(
            query,
            payload.max_records_per_query,
            payload.timeout,
            payload.retries,
            payload.backoff,
            payload.include_unimarc_xml,
        )
        searches.append({key: value for key, value in result.items() if key != "records"})
        for record in result["records"]:
            existing = candidates_by_id.get(record.mms_id)
            if existing:
                existing.matched_queries.extend(q for q in record.matched_queries if q not in existing.matched_queries)
                continue
            candidates_by_id[record.mms_id] = record

    for record in candidates_by_id.values():
        score_candidate(payload, record)

    ranked = sorted(candidates_by_id.values(), key=lambda item: item.score["final"], reverse=True)
    ranked = ranked[: payload.max_candidates]
    status, best_candidate, match_score = status_for_candidates(
        ranked,
        profile,
        payload.match_threshold,
        payload.ambiguous_threshold,
    )
    off_profile = [record for record in ranked if not matches_profile(record, profile)]

    return {
        "source": "alma_sru_academic_check",
        "profile": {
            "name": profile.name,
            "description": profile.description,
            "accepted_kinds": list(profile.accepted_kinds),
        },
        "query": {
            "title": payload.title,
            "subtitle": payload.subtitle,
            "author": payload.author,
            "degree_type": payload.degree_type,
            "discipline": payload.discipline,
            "granting_institution": payload.granting_institution,
            "doctoral_school": payload.doctoral_school,
            "defense_year": payload.defense_year,
            "volume": payload.volume,
            "advisor": payload.advisor,
            "language": payload.language,
        },
        "sru": {
            "endpoint": ALMA_SRU_ENDPOINT,
            "version": ALMA_SRU_VERSION,
            "record_schema": ALMA_RECORD_SCHEMA,
            "indexes": ["alma.title", "alma.creator", "alma.all_for_ui"],
            "queries": searches,
            "off_profile_candidates": len(off_profile),
        },
        "score_weights": dict(SCORE_WEIGHTS),
        "status": status,
        "match_score": round(match_score, 4),
        "decision_support": decision_support(ranked, profile, best_candidate),
        "best_academic_candidate": candidate_to_json(best_candidate, payload.include_unimarc_xml, profile)
        if best_candidate
        else None,
        "best_off_profile_candidate": candidate_to_json(off_profile[0], payload.include_unimarc_xml, profile)
        if off_profile
        else None,
        "candidates": [candidate_to_json(record, payload.include_unimarc_xml, profile) for record in ranked],
    }


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "humatheque-alma-check-api",
        "version": app.version,
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True}


@app.get("/sru/search")
async def sru_search_endpoint(
    query: str = Query(..., description='Raw Alma CQL query, for example alma.title all "hygiene indes".'),
    max_records: int = Query(DEFAULT_MAX_RECORDS_PER_QUERY, ge=1, le=100),
    timeout: float = Query(DEFAULT_TIMEOUT, gt=0.0, le=120.0),
    retries: int = Query(DEFAULT_RETRIES, ge=0, le=10),
    backoff: float = Query(DEFAULT_BACKOFF, ge=0.0, le=30.0),
    include_unimarc_xml: bool = Query(False),
    _: None = Depends(require_api_key),
) -> dict[str, Any]:
    if "alma." not in query:
        # Alma SRU rejects bare words (diagnostic 200812): wrap them as a keyword search.
        query = cql_clause("alma.all_for_ui", query.replace('"', " ").strip())
    result = await run_in_threadpool(search_alma, query, max_records, timeout, retries, backoff, include_unimarc_xml)
    profile = PROFILES["academic"]
    return {
        "source": "alma_sru",
        "query": query,
        "url": result["url"],
        "total_found": result["total_found"],
        "returned": len(result["records"]),
        "results": [candidate_to_json(record, include_unimarc_xml, profile) for record in result["records"]],
        "diagnostics": result["diagnostics"],
        "error": result["error"],
    }


@app.get("/profiles")
def list_profiles() -> dict[str, Any]:
    return {
        "profiles": [
            {
                "name": profile.name,
                "description": profile.description,
                "accepted_kinds": list(profile.accepted_kinds),
            }
            for profile in PROFILES.values()
        ]
    }


@app.post("/check/thesis")
async def check_thesis_endpoint(
    payload: ThesisCheckRequest,
    _: None = Depends(require_api_key),
) -> dict[str, Any]:
    payload.profile = "thesis"
    return await run_in_threadpool(check_alma, payload)


@app.post("/check/dissertation")
async def check_dissertation_endpoint(
    payload: DissertationCheckRequest,
    _: None = Depends(require_api_key),
) -> dict[str, Any]:
    payload.profile = "dissertation"
    return await run_in_threadpool(check_alma, payload)


@app.post("/check/academic")
async def check_academic_endpoint(
    payload: AlmaCheckRequest,
    _: None = Depends(require_api_key),
) -> dict[str, Any]:
    payload.profile = "academic"
    return await run_in_threadpool(check_alma, payload)


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
