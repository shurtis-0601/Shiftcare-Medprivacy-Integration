"""
De-identification engine built on the MedPrivacy detection logic.

Core PII detection (patterns, name variations, location extraction) is taken
directly from medprivacy.py (MedPrivacy v2.0.5 by medprivacy.com.au).

Pipeline-specific additions:
  - Known-participant substitution: replaces each participant's name with their
    specific reference code (PART-001) rather than a generic [NAME] tag.
  - Support-worker substitution: replaces known staff names with [STAFF_NAME].
  - Verification pass: after all substitutions, re-runs pattern detectors on the
    OUTPUT and quarantines the note if any NDIS numbers, phones, or emails remain.
  - Returns a structured DeidentifyResult rather than writing files.
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from collections import defaultdict
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — taken verbatim from medprivacy.py
# ---------------------------------------------------------------------------

GENERIC_TERMS = {
    'home', 'house', 'school', 'hospital', 'clinic', 'centre', 'center',
    'community', 'family', 'mother', 'father', 'parent', 'carer', 'mum', 'dad',
    'therapist', 'doctor', 'nurse', 'worker', 'coordinator', 'participant',
    'client', 'patient', 'assessment', 'therapy', 'treatment', 'support',
    'service', 'services', 'disability', 'health', 'care', 'medical',
    'january', 'february', 'march', 'april', 'may', 'june',
    'july', 'august', 'september', 'october', 'november', 'december',
    'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday',
    'australian', 'national', 'royal', 'general', 'main', 'primary',
    'street', 'road', 'avenue', 'drive', 'lane', 'court', 'place',
    'suburb', 'city', 'town', 'village',
}

_TITLES = {'Dr', 'Dr.', 'Mr', 'Mr.', 'Ms', 'Ms.', 'Mrs', 'Mrs.', 'Prof', 'Prof.'}

_NICKNAMES = {
    'David': 'Dave', 'Michael': 'Mike', 'William': 'Bill',
    'Robert': 'Bob', 'Elizabeth': 'Liz', 'Katherine': 'Kate',
    'Harrison': 'Harry', 'Christopher': 'Chris', 'Daniel': 'Dan',
}

# Priority during overlap resolution (lower number = higher priority)
_PRIORITY = {
    'PHONE': 1, 'EMAIL': 1, 'NDIS': 1, 'DOB': 1,
    'NAME': 2, 'ORGANIZATION': 3, 'LOCATION': 4,
}


# ---------------------------------------------------------------------------
# Name variation generator — taken from medprivacy.py PIIDatabase
# ---------------------------------------------------------------------------

def generate_name_variations(full_name: str) -> set[str]:
    """Return a set of surface forms for a given full name."""
    variations: set[str] = set()
    if not full_name or not full_name.strip():
        return variations

    cleaned = full_name.strip()
    for title in _TITLES:
        if cleaned.startswith(title + ' '):
            cleaned = cleaned[len(title):].strip()
            break

    parts = cleaned.split()
    if len(parts) < 2:
        if len(cleaned) > 2:
            variations.add(cleaned)
            variations.add(f"{cleaned}'s")
        return variations

    first, last = parts[0], parts[-1]

    for name in (full_name, cleaned):
        variations.add(name)
        variations.add(f"{name}'s")

    if len(first) > 2 and first not in _TITLES:
        variations.add(first)
        variations.add(f"{first}'s")

    if len(last) > 2:
        variations.add(last)
        variations.add(f"{last}'s")

    if first in _NICKNAMES:
        nick = _NICKNAMES[first]
        variations.add(nick)
        variations.add(f"{nick}'s")

    for formal, nick in _NICKNAMES.items():
        if first == nick:
            variations.add(formal)
            variations.add(f"{formal}'s")

    return variations


# ---------------------------------------------------------------------------
# PII pattern detectors — taken from medprivacy.py
# ---------------------------------------------------------------------------

def detect_ndis_numbers(text: str) -> set[str]:
    return set(re.findall(r'\b\d{9}\b', text))


def detect_phone_numbers(text: str) -> set[str]:
    patterns = [
        r'\+61[\s-]?[2-478][\s-]?\d{4}[\s-]?\d{4}',
        r'\+61[\s-]?4\d{2}[\s-]?\d{3}[\s-]?\d{3}',
        r'\b\(?0[2-478]\)?[\s-]?\d{4}[\s-]?\d{4}\b',
        r'\b04\d{2}[\s-]?\d{3}[\s-]?\d{3}\b',
    ]
    numbers: set[str] = set()
    for pat in patterns:
        numbers.update(re.findall(pat, text))
    return numbers


def detect_emails(text: str) -> set[str]:
    return set(re.findall(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', text))


def extract_locations_from_text(text: str) -> set[str]:
    """Conservative Australian location extractor — taken from medprivacy.py."""
    locations: set[str] = set()

    address_pattern = (
        r'\d+[/.-]?\d*\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}\s+'
        r'(?:Street|St|Road|Rd|Avenue|Ave|Drive|Dr|Lane|Ln|Court|Ct|Crescent|Cres|Boulevard|Blvd)'
    )
    for match in re.findall(address_pattern, text):
        if not any(t in match.lower() for t in ('hours', 'hrs', 'mins', 'score')):
            locations.add(match)

    states = [
        'VIC', 'NSW', 'QLD', 'WA', 'SA', 'TAS', 'ACT', 'NT',
        'Victoria', 'Queensland', 'New South Wales', 'Western Australia',
        'South Australia', 'Tasmania', 'Northern Territory',
    ]
    for state in states:
        if re.search(r'\b' + re.escape(state) + r'\b', text):
            locations.add(state)

    postcode_pattern = r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?),\s+(?:VIC|NSW|QLD|WA|SA|TAS|ACT|NT)?\s*(\d{4})\b'
    for suburb, postcode in re.findall(postcode_pattern, text):
        pc = int(postcode)
        if 200 <= pc <= 9999 and not (2020 <= pc <= 2030):
            locations.add(postcode)
            if suburb.lower() not in GENERIC_TERMS:
                locations.add(suburb)

    return locations


def _find_pii_generic(text: str, locations: set[str]) -> list[tuple[int, int, str, float]]:
    """
    Find NDIS, phone, email, DOB, and location findings in text.
    Returns (start, end, label, confidence) tuples.
    """
    findings: list[tuple[int, int, str, float]] = []

    for ndis in detect_ndis_numbers(text):
        for m in re.finditer(re.escape(ndis), text):
            findings.append((m.start(), m.end(), 'NDIS', 1.0))

    for phone in detect_phone_numbers(text):
        for m in re.finditer(re.escape(phone), text):
            findings.append((m.start(), m.end(), 'PHONE', 1.0))

    for email in detect_emails(text):
        for m in re.finditer(re.escape(email), text, re.IGNORECASE):
            findings.append((m.start(), m.end(), 'EMAIL', 1.0))

    dob_pat = r'(?:D\.?\s*O\.?\s*B\.?|DOB|Date of Birth|Born)[:\s]+\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}'
    for m in re.finditer(dob_pat, text, re.IGNORECASE):
        findings.append((m.start(), m.end(), 'DOB', 1.0))

    for loc in sorted(locations, key=len, reverse=True):
        pat = r'\b' + re.escape(str(loc)) + r'\b'
        for m in re.finditer(pat, text, re.IGNORECASE):
            if ' ' in str(loc):
                if text[m.start():m.end()].lower() == str(loc).lower():
                    findings.append((m.start(), m.end(), 'LOCATION', 1.0))
            else:
                findings.append((m.start(), m.end(), 'LOCATION', 1.0))

    return findings


def _merge_overlapping(findings: list[tuple]) -> list[tuple]:
    """Merge overlapping findings, keeping highest-priority / longest match."""
    if not findings:
        return []
    findings = sorted(findings, key=lambda x: (x[0], _PRIORITY.get(x[2], 5), -x[1]))
    merged = [findings[0]]
    for cur in findings[1:]:
        last = merged[-1]
        if cur[0] < last[1]:
            cur_p = _PRIORITY.get(cur[2], 5)
            last_p = _PRIORITY.get(last[2], 5)
            if cur_p < last_p:
                merged[-1] = cur
            elif cur_p == last_p and cur[1] > last[1]:
                merged[-1] = (last[0], cur[1], last[2], max(last[3], cur[3]))
        else:
            merged.append(cur)
    return merged


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class DeidentifyResult:
    deidentified_text: str
    is_quarantined: bool
    quarantine_reason: Optional[str]
    substitutions: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Main de-identification class
# ---------------------------------------------------------------------------

class MedPrivacyDeidentifier:
    """
    Adapts the MedPrivacy detection engine for automated pipeline use.

    Usage:
        deid = MedPrivacyDeidentifier()
        result = deid.deidentify(
            text=note_text,
            participants=[
                {"ref_code": "PART-001", "first_name": "Jane", "last_name": "Smith",
                 "ndis_number": "430123456", "date_of_birth": "1985-03-15",
                 "address": "42 Example St, Melbourne VIC 3000",
                 "phone": "0412345678", "email": "jane@example.com"},
                ...
            ],
            staff=[
                {"first_name": "Alex", "last_name": "Worker"},
                ...
            ],
        )
    """

    def deidentify(
        self,
        text: str,
        participants: list[dict],
        staff: list[dict] | None = None,
        supplementary_pii=None,
    ) -> DeidentifyResult:
        """
        De-identify a single note.

        participants:       all known participants (each must have ref_code + name fields).
        staff:              optional list of support workers to replace with [STAFF_NAME].
        supplementary_pii:  optional SupplementaryPII from the master CSV (carers, orgs,
                            locations not available via the ShiftCare API).
        """
        result = text
        substitutions: dict[str, int] = defaultdict(int)

        # --- Pass 1: Replace known participant names with their reference codes ---
        name_to_code: dict[str, str] = {}
        for p in participants:
            code = p.get("ref_code", "")
            full_name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            if not full_name or not code:
                continue
            for variation in generate_name_variations(full_name):
                if variation and variation.lower() not in GENERIC_TERMS and len(variation) > 2:
                    name_to_code[variation] = code

        for name, code in sorted(name_to_code.items(), key=lambda x: len(x[0]), reverse=True):
            pat = r'\b' + re.escape(name) + r'\b'
            new, count = re.subn(pat, code, result, flags=re.IGNORECASE)
            if count:
                result = new
                substitutions[f"NAME→{code}"] += count

        # --- Pass 2: Replace known staff names with [STAFF_NAME] ---
        if staff:
            staff_names: dict[str, str] = {}
            for s in staff:
                full = f"{s.get('first_name', '')} {s.get('last_name', '')}".strip()
                if not full:
                    continue
                for variation in generate_name_variations(full):
                    if variation and variation.lower() not in GENERIC_TERMS and len(variation) > 2:
                        staff_names[variation] = "[STAFF_NAME]"

            for name, tag in sorted(staff_names.items(), key=lambda x: len(x[0]), reverse=True):
                pat = r'\b' + re.escape(name) + r'\b'
                new, count = re.subn(pat, tag, result, flags=re.IGNORECASE)
                if count:
                    result = new
                    substitutions["STAFF_NAME"] += count

        # --- Pass 3: Explicit known-value substitution from participant records ---
        for p in participants:
            code = p.get("ref_code", "")

            # NDIS number for this participant → [NDIS_REDACTED]
            ndis = p.get("ndis_number", "")
            if ndis and re.search(r'\b' + re.escape(ndis) + r'\b', result):
                result = re.sub(r'\b' + re.escape(ndis) + r'\b', "[NDIS_REDACTED]", result)
                substitutions["NDIS"] += 1

            # DOB in common formats
            dob = p.get("date_of_birth", "")
            if dob:
                dob_variants = _dob_variants(dob)
                for dob_str in dob_variants:
                    if dob_str in result:
                        result = result.replace(dob_str, "[DOB]")
                        substitutions["DOB"] += 1

            # Phone
            phone = p.get("phone", "")
            if phone:
                phone_clean = re.sub(r'[\s\-()]', '', phone)
                for variant in (phone, phone_clean):
                    if variant and variant in result:
                        result = result.replace(variant, "[PHONE]")
                        substitutions["PHONE"] += 1

            # Email
            email = p.get("email", "")
            if email:
                new, count = re.subn(re.escape(email), "[EMAIL]", result, flags=re.IGNORECASE)
                if count:
                    result = new
                    substitutions["EMAIL"] += count

            # Address — only the street number+name part, not generic suburb words
            address = p.get("address", "")
            if address and len(address) > 8:
                street_match = re.match(
                    r'(\d+[/.-]?\d*\s+[A-Za-z].*?(?:Street|St|Road|Rd|Avenue|Ave|Drive|Dr|Lane|Ln|Court|Ct))',
                    address, re.IGNORECASE
                )
                if street_match:
                    street_part = street_match.group(1)
                    new, count = re.subn(re.escape(street_part), "[ADDRESS]", result, flags=re.IGNORECASE)
                    if count:
                        result = new
                        substitutions["ADDRESS"] += count

        # --- Pass 3b: Supplementary PII from master CSV (carers, orgs, locations) ---
        if supplementary_pii and not supplementary_pii.is_empty:

            # Carers → [CARER_NAME]
            for name in sorted(supplementary_pii.carers, key=len, reverse=True):
                if name and name.lower() not in GENERIC_TERMS and len(name) > 2:
                    pat = r'\b' + re.escape(name) + r'\b'
                    new, count = re.subn(pat, "[CARER_NAME]", result, flags=re.IGNORECASE)
                    if count:
                        result = new
                        substitutions["CARER_NAME"] += count

            # Extra providers not already in staff list → [STAFF_NAME]
            for name in sorted(supplementary_pii.extra_providers, key=len, reverse=True):
                if name and name.lower() not in GENERIC_TERMS and len(name) > 2:
                    pat = r'\b' + re.escape(name) + r'\b'
                    new, count = re.subn(pat, "[STAFF_NAME]", result, flags=re.IGNORECASE)
                    if count:
                        result = new
                        substitutions["STAFF_NAME"] += count

            # Extra participants from CSV not yet seen in ShiftCare → [PARTICIPANT]
            for name in sorted(supplementary_pii.extra_participants, key=len, reverse=True):
                if name and name.lower() not in GENERIC_TERMS and len(name) > 2:
                    pat = r'\b' + re.escape(name) + r'\b'
                    new, count = re.subn(pat, "[PARTICIPANT]", result, flags=re.IGNORECASE)
                    if count:
                        result = new
                        substitutions["PARTICIPANT"] += count

            # Organizations → [ORGANIZATION]
            for org in sorted(supplementary_pii.organizations, key=len, reverse=True):
                if org and len(org) > 2:
                    pat = r'\b' + re.escape(org) + r'\b'
                    new, count = re.subn(pat, "[ORGANIZATION]", result, flags=re.IGNORECASE)
                    if count:
                        result = new
                        substitutions["ORGANIZATION"] += count

            # Known locations from CSV → [LOCATION]
            for loc in sorted(supplementary_pii.locations, key=len, reverse=True):
                if loc and loc.lower() not in GENERIC_TERMS and len(loc) > 2:
                    pat = r'\b' + re.escape(loc) + r'\b'
                    new, count = re.subn(pat, "[LOCATION]", result, flags=re.IGNORECASE)
                    if count:
                        result = new
                        substitutions["LOCATION"] += count

        # --- Pass 4: Pattern-based catch-all for remaining PII ---
        locations = extract_locations_from_text(text)  # run on original to avoid false positives
        generic_findings = _find_pii_generic(result, locations)
        generic_findings = _merge_overlapping(generic_findings)

        label_map = {
            'NDIS': '[NDIS_REDACTED]',
            'PHONE': '[PHONE]',
            'EMAIL': '[EMAIL]',
            'DOB': '[DOB]',
            'LOCATION': '[LOCATION]',
        }
        for start, end, label, _ in sorted(generic_findings, key=lambda x: x[0], reverse=True):
            tag = label_map.get(label, f"[{label}]")
            result = result[:start] + tag + result[end:]
            substitutions[label] += 1

        # --- Verification pass: re-run hard-pattern detectors on the output ---
        residual_ndis = detect_ndis_numbers(result)
        residual_phones = detect_phone_numbers(result)
        residual_emails = detect_emails(result)

        is_quarantined = False
        quarantine_reason: Optional[str] = None

        if residual_ndis:
            is_quarantined = True
            quarantine_reason = f"Residual NDIS-pattern numbers in output: {sorted(residual_ndis)}"
        elif residual_phones:
            is_quarantined = True
            quarantine_reason = f"Residual phone numbers in output: {sorted(residual_phones)}"
        elif residual_emails:
            is_quarantined = True
            quarantine_reason = f"Residual email addresses in output: {sorted(residual_emails)}"

        if is_quarantined:
            logger.warning("Quarantine triggered: %s", quarantine_reason)

        return DeidentifyResult(
            deidentified_text=result,
            is_quarantined=is_quarantined,
            quarantine_reason=quarantine_reason,
            substitutions=dict(substitutions),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dob_variants(dob_iso: str) -> list[str]:
    """
    Return common Australian date-of-birth surface forms for an ISO date string.
    e.g. "1985-03-15" → ["15/03/1985", "15-03-1985", "15.03.1985", "15/03/85"]
    """
    try:
        parts = dob_iso.split("-")
        if len(parts) != 3:
            return []
        yyyy, mm, dd = parts
        yy = yyyy[2:]
        variants = []
        for sep in ('/', '-', '.'):
            variants.append(f"{dd}{sep}{mm}{sep}{yyyy}")
            variants.append(f"{dd}{sep}{mm}{sep}{yy}")
            variants.append(f"{mm}{sep}{dd}{sep}{yyyy}")
        return variants
    except Exception:
        return []
