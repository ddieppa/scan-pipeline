from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.classify.config import CompiledRules
from app.utils import normalize_spaces, safe_filename_component


def extract_page_indicator(text: str) -> tuple[int, int] | None:
    """Extract 'X of Y' page indicator from OCR text.
    
    Looks for patterns like '3 of 16', 'Page 7 of 10', 'pg 2/5'.
    Filters out dates (MM/DD/YYYY, MM/DD/YY) and other false positives.
    Returns (page_num, total_pages) or None.
    """
    if not text:
        return None
    # Match 'X of Y' patterns where X <= Y, Y > 2, both small numbers
    # Avoid matching dates like '10/22/85' or '09/29/2019'
    candidates = []
    for m in re.finditer(r'(?:(?:page|pg|p\.)\s*)?(\d{1,2})\s+(?:of|/)\s+(\d{1,3})(?!\d)', text, re.IGNORECASE):
        page_num = int(m.group(1))
        total = int(m.group(2))
        # Valid page indicator: page number <= total, total > 2, total <= 200
        # Avoid date-like matches: if both numbers look like a month/day pair, skip
        if 1 <= page_num <= total and 3 <= total <= 200:
            # Extra check: if this looks like MM/DD of a date, skip it
            # E.g., '09/29' is a date, not '9 of 29'
            # Check context: if preceded by month-like number, likely a date
            before = text[max(0, m.start()-20):m.start()]
            # If 'of' is preceded by a slash, it's likely a date: '09/29/2019'
            if '/' in before.split()[-1] if before.strip() else False:
                continue
            # If total > 31 and looks like a year suffix, skip
            if total > 50:
                continue  # Unlikely to have 50+ page medical docs in our context
            candidates.append((page_num, total))
    
    if not candidates:
        return None
    
    # Return the most common total_pages, then highest page_num for that total
    from collections import Counter
    total_counts = Counter(t for _, t in candidates)
    most_common_total = total_counts.most_common(1)[0][0]
    # Filter to candidates with that total, pick the first (top of page = most reliable)
    matching = [(p, t) for p, t in candidates if t == most_common_total]
    return matching[0] if matching else None


def group_sequential_pages(results: list[dict]) -> list[dict]:
    """Group scan results that belong to the same multi-page document.
    
    Uses 'X of Y' page indicators from OCR text to:
    1. Detect that multiple files are pages of the same document
    2. Group files with the same (folder, date, provider, doc_type, total_pages)
    3. Add sequential suffixes (_001, _002, etc.) based on page numbers
    
    Files in the same folder with the same date, provider, and total_pages
    belong to the same document set.
    
    Returns updated results with proposedName suffixes adjusted.
    """
    from pathlib import Path
    
    # Step 1: Extract page indicators from OCR
    page_info = {}  # sha -> (page_num, total_pages)
    for r in results:
        if r.get('status') != 'success':
            continue
        indicator = extract_page_indicator(r.get('ocrFullText', ''))
        if indicator:
            page_info[r['sha256']] = indicator
    
    if not page_info:
        return results  # No page indicators found
    
    # Step 2: Group files by (parent_folder, date, provider, doc_type, total_pages)
    # Files sharing these keys AND having page indicators belong to the same doc set
    groups = {}  # group_key -> list of (result_index, page_num)
    for i, r in enumerate(results):
        if r.get('status') != 'success':
            continue
        sha = r['sha256']
        if sha not in page_info:
            continue
        page_num, total_pages = page_info[sha]
        
        # Group key: folder + total_pages only
        # Provider varies page-to-page within the same document, so don't group by it.
        # Date and doc_type also vary (Rx leaflets vs lab results in same discharge packet).
        # The total_pages value separates different multi-page documents in the same folder
        # (e.g., 10-page discharge packet vs 16-page packet).
        parent = str(Path(r['path']).parent)
        key = (parent, total_pages)
        if key not in groups:
            groups[key] = []
        groups[key].append((i, page_num))
    
    # Step 3: For each group, assign sequential suffixes based on page number
    results_out = [dict(r) for r in results]  # shallow copy
    for key, members in groups.items():
        if len(members) < 2:
            continue  # Single-page doc, no suffix needed
        
        # Sort by page number
        members.sort(key=lambda x: x[1])
        
        total_pages = page_info[results_out[members[0][0]]['sha256']][1]
        
        # Check for page number conflicts (same page number appearing twice)
        page_nums = [p for _, p in members]
        if len(page_nums) != len(set(page_nums)):
            # Conflicting page numbers — don't add suffixes, leave as-is
            continue
        
        # Add sequential suffixes: _001, _002, etc. based on page number
        for idx, (result_idx, page_num) in enumerate(members):
            r = results_out[result_idx]
            suffix = f"{page_num:03d}"
            # Modify proposedName to include page suffix
            # Format: date_provider_type_person_001.ext
            # Insert suffix before extension, after person name
            name = r['proposedName']
            name_base, name_ext = name.rsplit('.', 1) if '.' in name else (name, 'jpg')
            r['proposedName'] = f"{name_base}_{suffix}.{name_ext}"
            # Also update the name in the results dict for consistency
            results_out[result_idx] = r
    
    return results_out


def _safe_format(template: str, **kwargs) -> str:
    """Format a template string, replacing missing placeholders with 'Unknown'."""
    import string
    class SafeDict(dict):
        def __missing__(self, key):
            return "Unknown"
    return string.Template(template.replace("{", "${").replace("}", "}")).safe_substitute(SafeDict(**kwargs))
    # Fallback: use format with defaults for missing keys
    import re
    # Find all {key} placeholders
    placeholders = set(re.findall(r'\{(\w+)\}', template))
    defaults = {k: 'Unknown' for k in placeholders - set(kwargs.keys())}
    return template.format(**defaults, **kwargs)


@dataclass(frozen=True)
class ClassificationResult:
    doc_type: str
    person: str
    provider: str
    proposed_name: str
    proposed_dest: str
    confidence: float
    rule_match_id: str
    highlights: dict
    exp_date: str | None = None
    side: str | None = None  # "Front", "Back", or None
    side_confidence: float = 0.0  # 0.0 to 1.0
    needs_side_confirmation: bool = False  # True when side < 90% confidence (was 95%)
    ambiguous: str | None = None  # Ambiguous category (e.g., "Business_or_PersonalFinance")
    reason_for_visit: str | None = None  # Extracted from discharge/ER docs
    final_diagnosis: str | None = None  # Extracted from discharge/ER docs
    physician: str | None = None  # Attending physician name
    question: str | None = None  # Question to ask user when ambiguous
    medication: str | None = None  # Generic medication name (e.g., "Escitalopram")
    brand_name: str | None = None  # Brand name (e.g., "Lexapro")
    classification_confidence: float = 0.0  # How certain we are the TYPE is correct
    auto_approved: bool = False  # True if confidence >= threshold and no conflicting signals


# Recent identity document cache for sequential Front/Back detection
# Key: (person, doc_type, doc_date), Value: list of (timestamp, side) entries
_recent_id_docs: dict[tuple[str, str, str], list[tuple[float, str]]] = {}
_RECENT_WINDOW_SECONDS = 1800  # 30 minutes (extended from 5 min — some scanners take longer between front/back)


_DATE_PATTERNS = [
    re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b"),
    re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b"),
    re.compile(r"\b(\d{2})/(\d{2})/(\d{2})\b"),  # MM/DD/YY
]
_EXP_DATE_PATTERNS = [
    re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b"),  # MM/DD/YYYY
    re.compile(r"\b(\d{2})/(\d{2})/(\d{2})\b"),  # MM/DD/YY
]
_AMOUNT_PATTERN = re.compile(r"\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})?)")

# Medication extraction patterns
_MEDICATION_PATTERN = re.compile(
    r'MEDICATION\s+([A-Z][A-Z0-9]{4,}(?:\s+[0-9]+(?:\.[0-9]+)?(?:MG|MCG|ML|G)?)?(?:\s+(?:TABLETS?|CAPSULES?|TABLET|CAPSULE|CHEWABLES?|SOLUTION|SYRUP|CREAM|OINTMENT|DROPS?|INHALER|INJECTION|PATCH|SPRAY|SUPPOSITORY|SUSPENSION|SYRINGE))?)',
    re.IGNORECASE
)
_GENERIC_INGREDIENT_PATTERN = re.compile(
    r'GENERIC\s+INGREDIENT:\s*([A-Z][A-Za-z]+(?:\s+[A-Za-z]+)*?)\s*(?:Tablets?|Capsules?|\(|$)',
    re.IGNORECASE
)
_BRAND_NAMES_PATTERN = re.compile(
    r'BRAND\s+NAMES?:\s*([A-Z][A-Za-z]+(?:,\s*[A-Za-z]+)*)',
    re.IGNORECASE
)


def _extract_business_name(text: str) -> str | None:
    """Extract business name from OCR text.
    
    Looks for company names in common patterns:
    - IRS Form 2553 / CP575 / CP261: company name after "Name ... Employer identification number"
    - Bank statement headers (BofA, Wells Fargo, etc.): company name near bank logo
    - Business name after 'Member FDIC' or bank disclaimers
    - Business name followed by street address
    """
    # Pattern 0: IRS form header — company name after "Name ... Employer identification number"
    # After normalize_spaces, newlines become spaces, so pattern must handle both formats:
    #   Name\nEmployer identification number\nN&D TEK SOLUTIONS LLC 88-0528126
    #   Name Employer identification number N&D TEK SOLUTIONS LLC 88-0528126
    irs_name_pattern = re.compile(
        r'Name\s+.{0,40}?Employer\s+identification\s+number\s*\n?\s*'
        r'([A-Z][A-Z0-9\s&.\-]+(?:LLC|INC\.?|LTD|LLP|CORP\.?|CORPORATION|CO\.?|COMPANY)?)\s+'
        r'(?:\d{2}-\d{7})',
        re.IGNORECASE | re.MULTILINE
    )
    match = irs_name_pattern.search(text)
    if match:
        return match.group(1).strip()
    
    # Pattern 0b: CP575 / EIN notice — "CP 575 [G] N&D TEK SOLUTIONS LLC"
    # After normalize_spaces, company name may be on same line as "CP 575"
    ein_pattern = re.compile(
        r'CP\s*575\s+[A-Z]?\s*'
        r'([A-Z][A-Z0-9\s&.\-]+(?:LLC|INC\.?|LTD|LLP|CORP\.?|CORPORATION|CO\.?|COMPANY))',
        re.IGNORECASE
    )
    match = ein_pattern.search(text)
    if match:
        return match.group(1).strip()
    
    # Pattern 0c: CP261 S-Corp acceptance notice — company name on same line
    # e.g., "IT DEVELOPMENT EXPERTS INC Notice date May 20,2019"
    cp261_pattern = re.compile(
        r'([A-Z][A-Z0-9\s&.\-]+(?:LLC|INC\.?|LTD|LLP|CORP\.?|CORPORATION|CO\.?|COMPANY))\s+'
        r'Notice\s+(?:date\s+)?(?:[A-Z][a-z]+\s+)?\d{1,2},?\s*\d{4}',
        re.MULTILINE
    )
    match = cp261_pattern.search(text)
    if match:
        return match.group(1).strip()
    
    # Pattern 1: Bank statement header — company name near bank name
    # e.g., "Bank of America\nIT DEVELOPMENT EXPERTS INC\n5474 1513 1703 8641"
    # or "Bank of America\nIT DEVELOPMENT EXPERTS INC\nBusiness Advantage"
    bank_stmt_pattern = re.compile(
        r'(?:Bank\s+of\s+America|Wells\s+Fargo|Chase|Citibank|TD\s+Bank|PNC\s+Bank|U\.?S\.?\s+Bank|SunTrust|Truist)\s*\n\s*'
        r'([A-Z][A-Z0-9\s&.\-]+(?:LLC|INC\.?|LTD|LLP|CORP\.?|CORPORATION|CO\.?|COMPANY))\s*\n',
        re.IGNORECASE | re.MULTILINE
    )
    match = bank_stmt_pattern.search(text)
    if match:
        return match.group(1).strip()
    
    # Pattern 1b: BofA Business Advantage — company name after "Business Advantage" + account number
    # e.g., "Business Advantage 6474 15131703 9641\nCompany Statement\n...IT DEVELOPMENT EXPERTS INC"
    bofa_biz_pattern = re.compile(
        r'Business\s+Advantage\s+\d[\d\s]{10,30}\s*\n\s*'
        r'(?:Company\s+Statement|Account\s+Information)?\s*\n\s*'
        r'([A-Z][A-Z0-9\s&.\-]+(?:LLC|INC\.?|LTD|LLP|CORP\.?|CORPORATION|CO\.?|COMPANY))',
        re.IGNORECASE | re.MULTILINE
    )
    match = bofa_biz_pattern.search(text)
    if match:
        return match.group(1).strip()
    
    # Pattern 1c: Company name on its own line near bank-specific keywords
    # e.g., "IT DEVELOPMENT EXPERTS INC\n5474 1513 1703 8641"
    # Account number pattern after company name
    biz_acct_pattern = re.compile(
        r'\n\s*([A-Z][A-Z0-9\s&.\-]+(?:LLC|INC\.?|LTD|LLP|CORP\.?|CORPORATION|CO\.?|COMPANY))\s*\n'
        r'\s*\d{4}[\s\d]{8,20}',  # account number format
        re.MULTILINE
    )
    match = biz_acct_pattern.search(text)
    if match:
        name = match.group(1).strip()
        # Sanity check: skip single words or too-short matches
        if len(name) > 5 and ' ' in name:
            return name
    
    # Pattern 2: Business name right after "Member FDIC" line (common bank statement layout)
    fdic_pattern = re.compile(
        r'(?:Member\s+FDIC|Member\s+FDIC\.|Accounts?\s+offered\s+by)\s*\.?\s*\n'
        r'\s*([A-Z][A-Z0-9\s&\.\-]+(?:LLC|INC\.?|LTD|LLP|CORP\.?|CORPORATION|CO\.?|COMPANY))\s*\n',
        re.IGNORECASE | re.MULTILINE
    )
    match = fdic_pattern.search(text)
    if match:
        return match.group(1).strip()
    
    # Pattern 3: Business name followed by street address
    addr_pattern = re.compile(
        r'\n\s*([A-Z][A-Z0-9\s&\.\-]+(?:LLC|INC\.?|LTD|LLP|CORP\.?|CORPORATION|CO\.?|COMPANY))\s*\n'
        r'\s*(?:\d+\s+[A-Z0-9\s]+(?:ST|AVE|RD|BLVD|DR|WAY|CT|CIR|PL|LN)|P\.?O\.?\s*BOX)',
        re.IGNORECASE | re.MULTILINE
    )
    match = addr_pattern.search(text)
    if match:
        return match.group(1).strip()
    
    return None

def _resolve_company_folder(raw_name: str, rules: CompiledRules) -> str | None:
    """Resolve a raw business name to the canonical QSync folder name.
    
    Uses the business_companies map in scan_rules.yaml to find the
    canonical folder name. Falls back to safe_filename_component if
    no mapping found.
    """
    if not raw_name:
        return None
    # Normalize for comparison: strip, upper, remove punctuation variations
    normalized = raw_name.strip().upper()
    # Remove common suffixes for matching
    for suffix in ["LLC", "INC.", "INC", "LTD", "CORP.", "CORP", "CORPORATION"]:
        normalized = normalized.removesuffix(suffix).strip()
    # Check business_companies map
    for company in rules.routing.get("business_companies", []):
        for alias in company.get("match", []):
            alias_norm = alias.strip().upper()
            for suffix in ["LLC", "INC.", "INC", "LTD", "CORP.", "CORP", "CORPORATION"]:
                alias_norm = alias_norm.removesuffix(suffix).strip()
            if normalized == alias_norm or normalized.replace("&", "AND") == alias_norm.replace("&", "AND"):
                return company["folder"]
    # Also check org definitions for folder_alias
    for org in rules.organizations:
        if org.category == "Business" and hasattr(org, 'folder_alias') and org.folder_alias:
            for name in org.names:
                name_norm = name.strip().upper()
                for suffix in ["LLC", "INC.", "INC", "LTD", "CORP.", "CORP", "CORPORATION"]:
                    name_norm = name_norm.removesuffix(suffix).strip()
                if normalized == name_norm or normalized.replace("&", "AND") == name_norm.replace("&", "AND"):
                    return org.folder_alias
    # Fallback: use safe_filename_component but preserve & (replace with nothing, not "And")
    cleaned = raw_name.strip()
    # Remove LLC/INC/LTD etc.
    for suffix in ["LLC", "Inc.", "Inc", "LTD", "Corp.", "Corp", "Corporation"]:
        cleaned = cleaned.removesuffix(suffix).strip()
    # Replace & with nothing, keep underscores
    cleaned = re.sub(r'[^&A-Za-z0-9._ -]', '', cleaned)
    cleaned = cleaned.replace('&', '').strip()
    cleaned = re.sub(r'\s+', '_', cleaned)
    return safe_filename_component(cleaned) if cleaned else None


def _extract_medication(text: str, medication_map: dict | None = None) -> tuple[str | None, str | None]:
    """Extract medication generic name and brand name from prescription text.

    Returns (medication, brand_name) where both are safe filename components.
    Checks the medication_map first for known medications, then falls back to OCR extraction.
    """
    medication = None
    brand_name = None

    # Try MEDICATION line first (most structured)
    med_match = _MEDICATION_PATTERN.search(text)
    if med_match:
        raw_med = med_match.group(1).strip()
        # Strip dosage info to get just the medication name
        # e.g., "ARIPIPRAZOLE 2MG TABLETS" -> "Aripiprazole"
        parts = raw_med.split()
        med_name_parts = []
        for part in parts:
            # Stop at dosage patterns (numbers, MG, MCG, TABLET, etc.)
            if re.match(r'^\d', part) or part.upper() in {'MG', 'MCG', 'ML', 'G', 'TABLETS', 'TABLET', 'CAPSULES', 'CAPSULE', 'CHEWABLES', 'CHEWABLE', 'SOLUTION', 'SYRUP', 'CREAM', 'OINTMENT', 'DROPS', 'DROP', 'INHALER', 'INJECTION', 'PATCH', 'SPRAY', 'SUPPOSITORY', 'SUSPENSION', 'SYRINGE'}:
                break
            med_name_parts.append(part)
        if med_name_parts:
            medication = ' '.join(med_name_parts).title()

    # If MEDICATION line didn't work, try GENERIC INGREDIENT
    if not medication:
        gen_match = _GENERIC_INGREDIENT_PATTERN.search(text)
        if gen_match:
            raw = gen_match.group(1).strip()
            # Clean up parenthetical pronunciation guide
            raw = re.sub(r'\s*\\([^)]*\\)', '', raw).strip()
            medication = raw.title()

    # Extract brand names
    brand_match = _BRAND_NAMES_PATTERN.search(text)
    if brand_match:
        raw_brands = brand_match.group(1).strip()
        # Take the first brand if multiple are listed
        brand_name = raw_brands.split(',')[0].strip().title()

    # Apply medication map overrides/canonicalization
    if medication_map and medication:
        med_lower = medication.lower()
        # Check if medication is a key in the map (generic name)
        if med_lower in medication_map:
            entry = medication_map[med_lower]
            medication = entry.get('canonical', medication)
            if not brand_name and entry.get('brand'):
                brand_name = entry['brand']
        # Check if medication is a brand name in the map
        for generic, entry in medication_map.items():
            aliases = entry.get('aliases', [])
            if med_lower in [a.lower() for a in aliases]:
                medication = entry.get('canonical', medication)
                if not brand_name and entry.get('brand'):
                    brand_name = entry['brand']
                break

    # Safe filename versions
    if medication:
        medication = safe_filename_component(medication)
    if brand_name:
        brand_name = safe_filename_component(brand_name)

    return medication, brand_name


_TAG_DECAL_PATTERN = re.compile(
    r'TAG[/\\]DECAL[/\\]VESS#?\s*:?\s*([A-Z0-9]{5,})',
    re.IGNORECASE,
)

_VIN_PATTERN = re.compile(
    r'VIN[/\\]HIN\s*:?\s*([A-Z0-9]{10,})',
    re.IGNORECASE,
)


def _extract_vehicle_info(text: str) -> dict:
    """Extract tag/decal/vessel number and VIN from vehicle registration OCR text.
    
    Returns dict with: tag_decal, vin
    """
    result = {"tag_decal": None, "vin": None}
    
    # Extract tag/decal/vessel number
    tag_match = _TAG_DECAL_PATTERN.search(text)
    if tag_match:
        result["tag_decal"] = tag_match.group(1)
    
    # Extract VIN
    vin_match = _VIN_PATTERN.search(text)
    if vin_match:
        result["vin"] = vin_match.group(1)
    
    return result


_RECOGNITION_PATTERNS = [
    re.compile(r'\bon\s+the\s+spot\s+award\b', re.IGNORECASE),
    re.compile(r'\bemployee\s+recognition\b', re.IGNORECASE),
    re.compile(r'\brecognition\s+(?:award|certificate|letter)\b', re.IGNORECASE),
    re.compile(r'\baward\s+(?:for|certificate)\b', re.IGNORECASE),
    re.compile(r'\bnominated\s+by\b', re.IGNORECASE),
    re.compile(r'\bperformance\s+award\b', re.IGNORECASE),
]

_EVALUATION_PATTERNS = [
    re.compile(r'\bperformance\s+evaluation\b', re.IGNORECASE),
    re.compile(r'\bemployee\s+evaluation\b', re.IGNORECASE),
    re.compile(r'\bperformance\s+review\b', re.IGNORECASE),
    re.compile(r'\bemployee\s+performance\b', re.IGNORECASE),
    re.compile(r'\bmeets\s+expectations\b', re.IGNORECASE),
    re.compile(r'\bexceeds\s+expectations\b', re.IGNORECASE),
    re.compile(r'\bdevelopment\s+plan(?:ning)?\b', re.IGNORECASE),
    re.compile(r'\baccomplishments?\s+and\s+opportunit\b', re.IGNORECASE),
]


def _detect_employment_subcategory(text: str) -> str:
    """Determine if employment document is an evaluation or recognition/award.
    
    Returns: 'Evaluation', 'Recognition', or 'Employment' (generic)
    """
    recognition_hits = sum(1 for p in _RECOGNITION_PATTERNS if p.search(text))
    evaluation_hits = sum(1 for p in _EVALUATION_PATTERNS if p.search(text))
    
    if recognition_hits > evaluation_hits and recognition_hits >= 2:
        return "Recognition"
    if evaluation_hits > recognition_hits and evaluation_hits >= 2:
        return "Evaluation"
    # Tie or low confidence
    if recognition_hits > 0 and evaluation_hits == 0:
        return "Recognition"
    if evaluation_hits > 0 and recognition_hits == 0:
        return "Evaluation"
    return "Employment"


def classify_document(text: str, path: Path, scan_date: str, rules: CompiledRules) -> ClassificationResult:
    normalized = normalize_spaces(text)
    lowered = normalized.lower()
    person = _match_person(lowered, rules) or _match_person(path.stem.lower(), rules) or _match_person_from_path(str(path), rules) or "Daniel"
    # Context-aware org matching: first pass without doc_type context
    org, all_org_matches = _match_organization_contextual(lowered, rules)
    doc_rule = _match_document_type(lowered, rules)
    # Fallback: try filename-based detection when OCR doesn't match
    if not doc_rule:
        doc_rule = _match_document_type(path.stem.lower(), rules)
    # Second pass: re-resolve org with doc_type context now known
    org, all_org_matches = _match_organization_contextual(lowered, rules, doc_type=doc_rule.id if doc_rule else None)
    # Priority override: bank/financial statement docs should NOT be classified
    # as vehicle registration, medical bills, or pharmacy just because they contain
    # keywords like "minimum payment due", "payment due date", "statement" etc.
    # BofA business credit card statements frequently trigger false positives.
    _bank_stmt_keywords = ["account number", "routing number", "statement period",
                           "closing date", "available credit", "minimum payment due",
                           "new balance", "credit limit", "cash advance",
                           "payment due date", "finance charge", "purchase(s)",
                           "balance transfer", "business advantage", "business checking",
                           "member fdic", "fdic insured", "bank of america",
                           "wells fargo", "chase", "citibank", "account summary",
                           "cardholder activity", "transactions", "posting date",
                           "reference number", "cardholder"]
    _bank_stmt_hits = sum(1 for kw in _bank_stmt_keywords if kw in lowered)
    if _bank_stmt_hits >= 3 and doc_rule and doc_rule.id in ("bill", "insurance", "vehicle", "medical_record", "prescription", "receipt"):
        # Strong bank statement signal — override to business
        for rule in rules.document_types:
            if rule.id == "business":
                doc_rule = rule
                break
    # Priority override: business docs (EIN, S-Corp, Form 2553) should not
    # be mis-classified as identity_card just because they contain SSNs.
    # If both identity_card and business match, prefer business when the text
    # contains Form 2553 or EIN-specific keywords.
    if doc_rule and doc_rule.id == "identity_card":
        biz_rule = rules.document_type_regexes.get("business")
        if biz_rule and biz_rule.search(lowered):
            # Check if business-specific keywords are present
            biz_keywords = ["form 2553", "s-corp", "s corporation election",
                            "cp 575", "ss-4", "employer identification number",
                            "ein ", "incorporation"]
            if any(kw in lowered for kw in biz_keywords):
                # Find the business DocumentTypeRule
                for rule in rules.document_types:
                    if rule.id == "business":
                        doc_rule = rule
                        break
    # Priority override: identity_card should NOT match forms that merely
    # CONTAIN an SSN field — only documents that ARE an SSN card.
    # Lab requisitions, medical forms, and other docs often have an SSN field
    # but are not identity documents.
    if doc_rule and doc_rule.id == "identity_card":
        # Check ALL matching orgs (not just the first) — a Lab or Medical org
        # in the text means this is almost certainly NOT an identity document.
        lab_med_org = None
        for organization in rules.organizations:
            if rules.org_regexes[organization.id].search(lowered):
                if organization.category in ("Lab", "Medical", "Pediatric", "Dental", "Vision", "Pharmacy"):
                    lab_med_org = organization
                    break
        
        if lab_med_org:
            # A medical/lab org is present — override identity_card
            if lab_med_org.category == "Lab":
                for rule in rules.document_types:
                    if rule.id == "lab_requisition":
                        doc_rule = rule
                        break
                else:
                    for rule in rules.document_types:
                        if rule.id == "medical_record":
                            doc_rule = rule
                            break
            elif lab_med_org.category in ("Medical", "Pediatric", "Dental", "Vision", "Pharmacy"):
                for rule in rules.document_types:
                    if rule.id == "medical_record":
                        doc_rule = rule
                        break
            # Use the medical/lab org as the provider, not the insurance org
            org = lab_med_org
        elif not org or org.category not in ("Legal", "Government"):
            # No org matched at all, or only a non-identity org — check for lab keywords
            lab_keywords = ["quest diagnostics", "labcorp", "requisition", "specimen",
                            "collection date", "ordering physician", "patient preparation",
                            "fasting", "cbc ", "metabolic panel", "lipid panel",
                            "urinalysis", "vitamin d", "lab test", "tests ordered",
                            "test ordered", "ereq", "e-req"]
            if sum(1 for kw in lab_keywords if kw in lowered) >= 2:
                for rule in rules.document_types:
                    if rule.id == "lab_requisition":
                        doc_rule = rule
                        break
                else:
                    for rule in rules.document_types:
                        if rule.id == "medical_record":
                            doc_rule = rule
                            break

    # Patient-preference: for medical/lab docs, prefer the patient name
    # over other names (e.g., responsible party/guarantor).
    # A lab req for Isabella with Daniel as guarantor should identify Isabella.
    if doc_rule and doc_rule.id in ("lab_requisition", "medical_record", "prescription",
                                       "bill", "medical_bill", "lab_bill", "dental_bill",
                                       "hospital_bill", "vision_bill", "dental"):
        patient = _match_patient(normalized, lowered, rules)
        if patient:
            person = patient

    # Priority override: medical documents with billing keywords should
    # be classified as bills, not medical records or prescriptions.
    # E.g. Quest Diagnostics lab result with "patient balance" is a bill,
    # Costco Optical "prescription lenses" invoice is a bill.
    if doc_rule and doc_rule.id in ("medical_record", "prescription"):
        bill_rule = rules.document_type_regexes.get("bill")
        if bill_rule and bill_rule.search(lowered):
            # Billing keywords present — reclassify as bill
            for rule in rules.document_types:
                if rule.id == "bill":
                    doc_rule = rule
                    break
    # Priority override: prescription docs should not be mis-classified
    # as medical_record. Walgreens Rx labels have "prescription" and "medication"
    # but also match "surgery" etc. from fine print.
    if doc_rule and doc_rule.id == "medical_record":
        rx_rule = rules.document_type_regexes.get("prescription")
        if rx_rule and rx_rule.search(lowered):
            for rule in rules.document_types:
                if rule.id == "prescription":
                    doc_rule = rule
                    break
    provider = getattr(org, 'folder_alias', None) or safe_filename_component(org.names[0] if org else "Unknown")
    doc_date = _extract_doc_date(normalized) or scan_date
    # Apply filename heuristics for scanner defaults
    if _is_scanner_default_filename(path.stem):
        doc_date = scan_date
    amount = _extract_amount(normalized)
    year = int(doc_date.split("-")[0])

    # Also check filename for classification hints when OCR is poor
    filename_lower = path.stem.lower()
    # Folder-based doc_type hint when no OCR match
    folder_hints = _detect_folder_doc_type(str(path), rules)
    folder_hint = folder_hints.get("doc_type") if folder_hints else None
    # Full folder metadata extraction for low-confidence fallback
    folder_meta = _extract_folder_metadata(path, rules)
    
    # For medical/Rx/bill docs inside dated folders, prefer folder date over OCR date
    # OCR often picks up DOB or pharmacy fill date instead of the actual visit/service date
    if folder_meta.get("date") and folder_meta["date"] != doc_date:
        if doc_rule and doc_rule.id in ("medical_record", "prescription", "bill", "dental_bill"):
            doc_date = folder_meta["date"]
            year = int(doc_date.split("-")[0])
    
    # Override person from folder hints if available (high confidence)
    if folder_meta.get("person"):
        person = folder_meta["person"]
    
    # Handle ambiguous cases (Bank Statement could be Business or Personal)
    ambiguous_flag = folder_meta.get("ambiguous")
    ambiguous_question = folder_meta.get("question")
    
    # Clear ambiguity if inside a company folder (clearly business)
    if ambiguous_flag and folder_meta.get("company"):
        ambiguous_flag = None
        ambiguous_question = None

    # --- PRE-CHECK: Business context override ---
    # If folder is inside a business folder, check for business-specific patterns FIRST
    # This prevents "passport" in "passportservices" from overriding bank statements,
    # or "insurance" patterns from overriding S Corp docs inside a business folder.
    business_name = None
    business_folder = None  # Canonical QSync folder name (e.g., "ND_Tek_Solutions")
    subcategory = None
    if folder_meta.get("company") and folder_meta.get("doc_type") == "Business":
        if "checking account" in lowered or "business checking" in lowered:
            subcategory = "BusinessChecking"
            for rule in rules.document_types:
                if rule.id == "business":
                    doc_rule = rule
                    break
        elif "credit limit" in lowered or "available credit" in lowered or ("minimum payment due" in lowered and "new balance" in lowered):
            subcategory = "BusinessCreditCard"
            for rule in rules.document_types:
                if rule.id == "business":
                    doc_rule = rule
                    break
        elif "s corporation" in lowered or "s-corp" in lowered or "s corporation election" in lowered or "form 2553" in lowered:
            # S Corporation election confirmation
            for rule in rules.document_types:
                if rule.id == "business":
                    doc_rule = rule
                    break
        elif "internal revenue service" in lowered or "department of the treasury" in lowered or "irs" in lowered.split() or "employer id" in lowered or "employer identification" in lowered:
            # IRS/tax docs inside business folder → business
            for rule in rules.document_types:
                if rule.id == "business":
                    doc_rule = rule
                    break
        # Extract business name whenever we force business classification
        if doc_rule and doc_rule.id == "business":
            raw_name = _extract_business_name(normalized)
            if not raw_name and folder_meta.get("company"):
                raw_name = folder_meta["company"]
            # Resolve to canonical QSync folder name
            business_folder = _resolve_company_folder(raw_name, rules)
            business_name = raw_name  # Keep raw name for filename display
        # For financial statements, prefer statement/closing date over generic date extraction
        stmt_date = _extract_statement_date(normalized)
        if stmt_date:
            doc_date = stmt_date
    # --- END PRE-CHECK ---

    # --- BUSINESS FOLDER RESOLUTION (also for org-matched business docs) ---
    # If doc_rule is business but no business_folder yet (e.g., from root-level files),
    # try to resolve from org match or OCR text
    if doc_rule and doc_rule.id == "business" and not business_folder:
        # Try from org first
        if org and org.category == "Business" and hasattr(org, 'folder_alias') and org.folder_alias:
            business_folder = org.folder_alias
            if not business_name:
                business_name = org.names[0]
        # Try from OCR text extraction
        if not business_name:
            raw_name = _extract_business_name(normalized)
            if raw_name:
                business_name = raw_name
                business_folder = _resolve_company_folder(raw_name, rules)
        # Last resort: try from org name
        if not business_folder and org and org.category == "Business":
            business_folder = _resolve_company_folder(org.names[0], rules)
            if not business_name:
                business_name = org.names[0]

    if doc_rule:
        label = doc_rule.labels.get(org.category if org else "", doc_rule.labels.get("default", doc_rule.id.capitalize()))
    elif folder_hint:
        label = folder_hint
    else:
        label = "Unknown"

    # Determine destination using category_routing (v2) or org.destination fallback
    destination = _resolve_destination(
        doc_rule=doc_rule,
        org=org,
        person=person,
        year=year,
        rules=rules,
        folder_hint=folder_hint if not doc_rule else None,
        subcategory=subcategory,
        business_name=business_name,
        business_folder=business_folder,
    )

    ext = path.suffix.lower().lstrip(".")
    exp_date = None
    side = None
    side_confidence = 0.0
    medication = None
    brand_name = None
    # Also check filename for classification hints when OCR is poor
    filename_lower = path.stem.lower()

    if doc_rule:
        filename_template = doc_rule.filename_template
        # Detect specific ID type from text (and filename fallback)
        if doc_rule.id == "identity_card":
            exp_date = _extract_expiration_date(normalized)
            destination = f"02-Areas/Legal/Identity/{safe_filename_component(person)}/"
            # Determine specific ID type — check OCR text first, then filename
            if "driver license" in lowered or "driver's license" in lowered:
                label = "DriverLicense"
            elif "passport" in lowered:
                label = "Passport"
            elif "social security" in lowered or "ssn" in lowered:
                label = "SSN"
            elif "birth certificate" in lowered:
                label = "BirthCertificate"
            elif "naturalization" in lowered:
                label = "NaturalizationCert"
            # Filename fallback when OCR didn't match
            elif "driver license" in filename_lower or "driver's license" in filename_lower or "dl" in filename_lower.split():
                label = "DriverLicense"
            elif "passport" in filename_lower:
                label = "Passport"
            elif "social security" in filename_lower or "ssn" in filename_lower.split():
                label = "SSN"
            elif "birth certificate" in filename_lower:
                label = "BirthCertificate"
            elif "naturalization" in filename_lower:
                label = "NaturalizationCert"
            else:
                label = "IDCard"

            # For identity docs, prefer scan date over DOB as document date
            # DOB is not the document date; use scan_date as fallback
            if doc_date != scan_date:
                # Check if doc_date might be a DOB (before 2010 is unlikely as issue date)
                try:
                    doc_year = int(doc_date.split("-")[0])
                    if doc_year < 2010:
                        doc_date = scan_date
                except (ValueError, IndexError):
                    pass

            # Sequential Front/Back detection
            # Use document date (or scan date) as cache key since both sides share same issue date
            cache_key_doc_date = doc_date if doc_date != scan_date else scan_date
            # First check filename for Front/Back hints
            filename_side = _detect_side_from_filename(path.stem)
            if filename_side:
                # Filename explicitly says Front/Back — high confidence
                side = filename_side
                side_confidence = 0.98
            else:
                side, side_confidence = _detect_side_sequential(person, label, cache_key_doc_date)

            # Build label with side suffix if detected with high confidence (>= 0.90)
            needs_side_confirm = False
            if side and side_confidence >= 0.90:
                label = f"{label}{side}"
            elif side is None or side_confidence < 0.90:
                # Low confidence or no side detected — will need user confirmation
                needs_side_confirm = True

            proposed_name = _safe_format(filename_template,
                date=doc_date,
                doc_label=label,
                person=safe_filename_component(person),
                exp_date=exp_date or "UNKNOWN",
                ext=ext,
            )
        elif doc_rule.id == "prescription":
            # Prescription: extract medication and brand name from OCR text
            medication_map = rules.routing.get("medication_map", {})
            medication, brand_name = _extract_medication(normalized, medication_map)
            
            # Determine the pharmacy/provider — use the actual pharmacy if matched, not insurance
            pharmacy_org = None
            for organization in rules.organizations:
                if organization.category in ("Pharmacy", "Medical") and rules.org_regexes[organization.id].search(lowered):
                    pharmacy_org = organization
                    break
            # If we found a pharmacy, use it as provider instead of insurance
            if pharmacy_org:
                provider = safe_filename_component(pharmacy_org.names[0])
                # Override destination to health folder (not insurance archive)
                # Prescriptions belong in Health/Pharmacy, not Insurance archives
                if pharmacy_org.destination:
                    try:
                        destination = pharmacy_org.destination.format(
                            person=safe_filename_component(person),
                            year=year,
                        )
                        # Clean up template artifacts
                        import re as _re
                        destination = _re.sub(r'/{2,}', '/', destination)
                        destination = _re.sub(r'/Unknown/?', '/', destination)
                        destination = destination.rstrip('/') + '/'
                    except KeyError:
                        destination = pharmacy_org.destination
                else:
                    # Use Pharmacy category routing
                    pharmacy_route = rules.routing.get("category_routing", {}).get("Pharmacy", {})
                    pharmacy_template = pharmacy_route.get("template", "")
                    if pharmacy_template:
                        try:
                            destination = pharmacy_template.format(
                                person=safe_filename_component(person),
                                year=year,
                            ).rstrip('/') + '/'
                        except KeyError:
                            pass
            elif "pharmacy" in lowered or "walgreens" in lowered or "cv s" in lowered:
                # Pharmacy detected in text but no org match — route to Health
                destination = f"02-Areas/Family/{safe_filename_component(person)}/Health/"
            # Build filename: {date}_{provider}_Rx_{medication}_BrandName_{brand}_{person}.{ext}
            name_parts = [doc_date, provider, "Rx"]
            if medication:
                name_parts.append(medication)
            if brand_name:
                name_parts.append("BrandName")
                name_parts.append(brand_name)
            name_parts.append(safe_filename_component(person))
            proposed_name = "_".join(name_parts) + f".{ext}"
        elif doc_rule.id == "vehicle":
            # Vehicle registration: extract tag/decal/vessel# instead of amount
            vehicle_info = _extract_vehicle_info(normalized)
            tag_decal = vehicle_info.get("tag_decal")
            vin = vehicle_info.get("vin")
            # Determine specific vehicle doc type
            if "registration renewal" in lowered or "registration" in lowered:
                label = "RegistrationRenewal"
            elif "title transfer" in lowered:
                label = "TitleTransfer"
            elif "lease end" in lowered or "lease" in lowered:
                label = "LeaseEnd"
            else:
                label = doc_rule.labels.get("default", "VehicleRegistration")
            # Build filename with tag/decal if available
            name_parts = [doc_date, safe_filename_component(provider) if provider != "Unknown" else "",
                          label]
            if tag_decal:
                name_parts.append(f"TagDecal_{tag_decal}")
            elif vin:
                name_parts.append(f"VIN_{vin[:8]}")  # Use first 8 chars of VIN for readability
            name_parts.append(safe_filename_component(person))
            # Remove empty parts
            name_parts = [p for p in name_parts if p]
            proposed_name = "_".join(name_parts) + f".{ext}"
        elif doc_rule.id == "employment":
            # Employment: detect subcategory (Evaluation vs Recognition) from OCR content
            emp_subcategory = _detect_employment_subcategory(normalized)
            label = doc_rule.labels.get(emp_subcategory, doc_rule.labels.get("default", "Employment"))
            # Use folder date if available (e.g., "Employee Evaluation 20191223" folder)
            folder_date = folder_meta.get("date")
            if folder_date and folder_date != doc_date:
                # Prefer folder date for employment docs (often more accurate)
                doc_date = folder_date
                year = int(doc_date.split("-")[0])
            # Also check filename for embedded date (e.g., "20170901 Employee Recognition.pdf")
            if doc_date == scan_date:
                fname_date = re.search(r'(\d{4})(\d{2})(\d{2})', path.stem)
                if fname_date:
                    candidate = f"{fname_date.group(1)}-{fname_date.group(2)}-{fname_date.group(3)}"
                    if candidate != doc_date:
                        doc_date = candidate
                        year = int(doc_date.split("-")[0])
            # Build filename
            name_parts = [doc_date]
            # Use company from folder meta if provider is Unknown
            company = folder_meta.get("company") or provider
            if company and company != "Unknown":
                name_parts.append(safe_filename_component(company))
            name_parts.append(label)
            name_parts.append(safe_filename_component(person))
            # Add sequential suffix for multi-page scanner output
            suffix = path.stem.rsplit("_", 1)[-1] if "_" in path.stem and path.stem.rsplit("_", 1)[-1].isdigit() else ""
            if suffix:
                name_parts.append(suffix)
            proposed_name = "_".join(name_parts) + f".{ext}"
            # Ensure destination includes company subfolder
            if company and company != "Unknown" and "Employment" in destination and company not in destination:
                destination = f"04-Archives/Employment/{safe_filename_component(company)}/"
        else:
            label = doc_rule.labels.get(org.category if org else "", doc_rule.labels.get("default", doc_rule.id.capitalize()))
            
            # --- BUSINESS DOC SUBTYPE DETECTION ---
            if doc_rule.id == "business":
                # EIN confirmation (CP575) — specific to EIN notices; check BEFORE S-Corp
                # because CP575 notices reference "form 2553" but are NOT Form 2553.
                # CP575 tells you the EIN was assigned; Form 2553 is the election itself.
                if "cp 575" in lowered or "ss-4" in lowered:
                    label = doc_rule.labels.get("EIN", "EINConfirmation")
                    if not business_name:
                        raw_name = _extract_business_name(normalized)
                        if not raw_name and folder_meta.get("company"):
                            raw_name = folder_meta["company"]
                        business_name = raw_name
                        business_folder = _resolve_company_folder(raw_name, rules) if raw_name else None
                # S Corporation election (Form 2553) — the election form itself
                elif "form 2553" in lowered or "s-corp" in lowered or "s corporation election" in lowered or ("s corporation" in lowered and "election" in lowered):
                    label = doc_rule.labels.get("SCorp", "SCorporationElection")
                    if not business_name:
                        raw_name = _extract_business_name(normalized)
                        if not raw_name and folder_meta.get("company"):
                            raw_name = folder_meta["company"]
                        business_name = raw_name
                        business_folder = _resolve_company_folder(raw_name, rules) if raw_name else None
                # CP261 — IRS acceptance notice for S-Corp election
                elif "cp 261" in lowered or "cp261" in lowered or ("accepted" in lowered and "s corporation" in lowered) or ("we've accepted" in lowered and "s corporation" in lowered):
                    label = doc_rule.labels.get("SCorp", "SCorporationElection")
                    if not business_name:
                        raw_name = _extract_business_name(normalized)
                        if not raw_name and folder_meta.get("company"):
                            raw_name = folder_meta["company"]
                        business_name = raw_name
                        business_folder = _resolve_company_folder(raw_name, rules) if raw_name else None
                # FL Division of Corporations / Sunbiz government filing
                elif "division of corporations" in lowered or "department of state" in lowered or "sunbiz" in lowered or ("tracking number" in lowered and "document number" in lowered):
                    label = doc_rule.labels.get("GovernmentFiling", "GovernmentFiling")
                    if not business_name:
                        raw_name = _extract_business_name(normalized)
                        if not raw_name and folder_meta.get("company"):
                            raw_name = folder_meta["company"]
                        business_name = raw_name
                        business_folder = _resolve_company_folder(raw_name, rules) if raw_name else None
                # Generic EIN mention (not CP575 or Form 2553)
                elif "employer identification number" in lowered or "ein " in lowered:
                    label = doc_rule.labels.get("EIN", "EINConfirmation")
                    if not business_name:
                        raw_name = _extract_business_name(normalized)
                        if not raw_name and folder_meta.get("company"):
                            raw_name = folder_meta["company"]
                        business_name = raw_name
                        business_folder = _resolve_company_folder(raw_name, rules) if raw_name else None
                # For financial statements, prefer statement/closing date over generic date extraction
                stmt_date = _extract_statement_date(normalized)
                if stmt_date:
                    doc_date = stmt_date
            
            # --- BUSINESS DOC FILENAME AND DESTINATION OVERRIDE ---
            if doc_rule.id == "business" and business_name:
                # Use canonical folder name for filenames (ND_Tek_Solutions not NAndD_Tek_Solutions)
                display_name = business_folder if business_folder else safe_filename_component(business_name)
                # Use YYYY-MM for monthly statements, YYYY-MM-DD for one-time docs
                if subcategory in ("BusinessChecking", "BusinessCreditCard"):
                    date_str = doc_date[:7]  # YYYY-MM for monthly statements
                else:
                    date_str = doc_date  # YYYY-MM-DD for one-time docs (S Corp, EIN, etc.)
                
                if subcategory:
                    proposed_name = f"{date_str}_{display_name}_{subcategory}.{ext}"
                else:
                    proposed_name = f"{date_str}_{display_name}_{label}.{ext}"
                
                # Route to proper subfolder based on document subtype
                if business_folder:
                    base = f"02-Areas/Business/{business_folder}"
                    if label == "SCorporationElection":
                        # S Corp election → Legal/
                        destination = f"{base}/Legal/"
                    elif label == "EINConfirmation":
                        # EIN confirmation → Legal/
                        destination = f"{base}/Legal/"
                    elif label == "GovernmentFiling":
                        # Government filings (Sunbiz, etc.) → Legal/
                        destination = f"{base}/Legal/"
                    elif subcategory in ("BusinessChecking", "BusinessCreditCard"):
                        # Bank/CC statements → Financial/{year}/Banking/
                        dest_year = doc_date[:4]
                        destination = f"{base}/Financial/{dest_year}/Banking/"
                    else:
                        # Other business docs → Financial/00_Foundational/
                        destination = f"{base}/Financial/00_Foundational/"
            elif doc_rule.id == "business" and not business_name:
                # Business doc without extracted company name — use org or fallback
                proposed_name = f"{doc_date}_{label}.{ext}"
                # If org is a business org with folder_alias, route to its subfolder
                if org and org.category == "Business" and hasattr(org, 'folder_alias') and org.folder_alias:
                    base = f"02-Areas/Business/{org.folder_alias}"
                    if label == "SCorporationElection":
                        destination = f"{base}/Legal/"
                    elif label == "EINConfirmation":
                        destination = f"{base}/Legal/"
                    else:
                        destination = f"{base}/Financial/00_Foundational/"
            else:
                proposed_name = _safe_format(filename_template,
                    date=doc_date,
                    provider=provider,
                    doc_label=label,
                    person=safe_filename_component(person),
                    ext=ext,
                )
        # Tiered confidence scoring:
        # - org + folder match: 0.92 (strongest — both OCR and folder agree)
        # - org match: 0.88 (good — org detected from OCR text)
        # - rule + folder match: 0.72 (moderate — rule matched + folder hints confirm)
        # - rule only, no org: 0.55 (weak — rule matched but no org confirmation)
        # Confidence is further reduced if doc_type pattern is ambiguous (matches multiple categories)
        if org:
            confidence = 0.92 if folder_meta.get("doc_type") else 0.88
        else:
            confidence = 0.72 if folder_meta.get("doc_type") else 0.55
        # Reduce confidence if rule matches an ambiguous doc type
        # (e.g., "bill" patterns that also match "business" docs)
        if doc_rule and doc_rule.id in ("bill", "insurance", "vehicle"):
            # Check if business keywords are also present
            biz_keywords = ["account number", "routing number", "statement period",
                           "closing date", "available credit", "minimum payment due",
                           "new balance", "business advantage", "business checking",
                           "credit limit", "member fdic", "fdic", "bank of america",
                           "wells fargo", "chase", "citibank"]
            if any(kw in lowered for kw in biz_keywords):
                confidence = min(confidence, 0.40)  # Likely misclassified
        rule_match_id = f"{doc_rule.id}:{org.id if org else 'no-org'}"
        doc_type = "Rx" if doc_rule.id == "prescription" else label
    else:
        # No rule matched — use folder metadata for better classification
        if _is_scanner_default_filename(path.stem):
            # Use folder-based description for scanner defaults AND low-confidence matches
            desc = folder_meta.get("doc_type") or folder_hint or "Scan"
            person_override = folder_meta.get("person") or person
            date_override = folder_meta.get("date") or doc_date
            company = folder_meta.get("company") or provider
            subcategory = folder_meta.get("subcategory")
            event = folder_meta.get("event")
            # Employment subcategory detection from OCR + folder context
            if desc == "Employment":
                emp_sub = _detect_employment_subcategory(normalized)
                if emp_sub == "Recognition":
                    desc = "EmployeeRecognition"
                elif emp_sub == "Evaluation":
                    desc = "EmployeeEvaluation"
                elif event and "recognition" in event.lower():
                    desc = "EmployeeRecognition"
                elif event and "evaluation" in event.lower():
                    desc = "EmployeeEvaluation"
                # Prefer folder date for employment docs
                if folder_meta.get("date") and folder_meta["date"] != doc_date:
                    date_override = folder_meta["date"]
            # Build a cleaner name using folder metadata
            # For Creative_Work with an event, use: {date}_{Event}_Pics_{person}_{seq}
            if desc == "Creative_Work" and event:
                name_parts = [date_override, safe_filename_component(event), "Pics", safe_filename_component(person_override)]
            elif desc == "EmployeeEvaluation":
                name_parts = [date_override]
                if company and company != "Unknown":
                    name_parts.append(safe_filename_component(company))
                name_parts.append("EmployeeEvaluation")
                name_parts.append(safe_filename_component(person_override))
            elif desc == "EmployeeRecognition":
                name_parts = [date_override]
                if company and company != "Unknown":
                    name_parts.append(safe_filename_component(company))
                name_parts.append("EmployeeRecognition")
                name_parts.append(safe_filename_component(person_override))
            elif desc == "Education":
                # Education: {date_range}_{Event}_{SubEvent}_{person}_{seq}
                # e.g., 2019-2020_Homework_MiniMe_Isabella_005.jpg
                date_label = folder_meta.get("date_range") or date_override
                name_parts = [date_label]
                # Combine event and event_suffix for compound names
                event_parts = []
                if folder_meta.get("event"):
                    event_parts.append(folder_meta["event"])
                if folder_meta.get("event_suffix"):
                    event_parts.append(folder_meta["event_suffix"])
                if event_parts:
                    name_parts.append("_".join(safe_filename_component(p) for p in event_parts))
                name_parts.append(safe_filename_component(person_override))
            else:
                name_parts = [date_override, desc]
                if subcategory:
                    name_parts.append(subcategory)
                if event and desc != "Creative_Work":
                    name_parts.append(safe_filename_component(event))
                name_parts.append(safe_filename_component(person_override))
            # Add sequential suffix for multi-page scanner output
            suffix = path.stem.rsplit("_", 1)[-1] if "_" in path.stem and path.stem.rsplit("_", 1)[-1].isdigit() else ""
            if suffix:
                name_parts.append(suffix)
            proposed_name = "_".join(name_parts) + f".{ext}"
            # Boost confidence based on how much folder metadata we have
            folder_clues = sum(1 for v in [folder_meta.get("person"), folder_meta.get("doc_type"), folder_meta.get("company"), folder_meta.get("date")] if v)
            if folder_clues >= 3:
                confidence = 0.72
            elif folder_clues >= 2:
                confidence = 0.55
            else:
                confidence = 0.40
            # Use folder_meta to resolve destination (scanner defaults)
            if folder_meta.get("doc_type") and folder_meta.get("doc_type") in rules.routing.get("category_routing", {}):
                category = folder_meta["doc_type"]
                route = rules.routing["category_routing"][category]
                template = route.get("template") or route.get("archive_template", "")
                if template:
                    try:
                        destination = template.format(
                            person=safe_filename_component(person_override),
                            year=folder_meta.get("year") or year,
                            org_name=company if company != "Unknown" else "",
                            subcategory=subcategory or "",
                            company=company if company != "Unknown" else "",
                            date_range=folder_meta.get("date_range") or "",
                            grade=folder_meta.get("date_range") or folder_meta.get("year", ""),
                        )
                        import re as _re
                        destination = _re.sub(r'/{2,}', '/', destination)
                        destination = _re.sub(r'/\s*_\s*/', '/', destination)
                        destination = _re.sub(r'/Unknown/?', '/', destination)
                        destination = _re.sub(r'/None/?', '/', destination)
                        destination = destination.rstrip('/') + '/'
                    except KeyError:
                        pass
            rule_match_id = f"folder-hint:{desc}"
            doc_type = desc
        else:
            # Non-scanner-default with no rule match — try folder metadata
            desc = folder_meta.get("doc_type") or folder_hint or label
            person_override = folder_meta.get("person") or person
            date_override = folder_meta.get("date") or doc_date
            company = folder_meta.get("company") or provider
            subcategory = folder_meta.get("subcategory")
            # If company was detected, use it in the name
            name_parts = [date_override]
            if company and company != "Unknown":
                name_parts.append(safe_filename_component(company))
            name_parts.append(safe_filename_component(desc))
            if subcategory:
                name_parts.append(subcategory)
            name_parts.append(safe_filename_component(person_override))
            proposed_name = "_".join(name_parts) + f".{ext}"
            # Boost confidence based on folder metadata richness
            folder_clues = sum(1 for v in [folder_meta.get("person"), folder_meta.get("doc_type"), folder_meta.get("company"), folder_meta.get("date")] if v)
            if folder_clues >= 2:
                confidence = 0.55
            elif folder_clues >= 1:
                confidence = 0.40
            else:
                confidence = 0.25
            rule_match_id = "fallback-folder"
            doc_type = desc
            # Try folder-based destination override
            if folder_meta.get("doc_type") and folder_meta.get("doc_type") in rules.routing.get("category_routing", {}):
                category = folder_meta["doc_type"]
                route = rules.routing["category_routing"][category]
                template = route.get("template") or route.get("archive_template", "")
                if template:
                    try:
                        destination = template.format(
                            person=safe_filename_component(person_override),
                            year=folder_meta.get("year") or year,
                            org_name=company if company != "Unknown" else "",
                            subcategory=subcategory or "",
                            company=company if company != "Unknown" else "",
                            date_range=folder_meta.get("date_range") or "",
                            grade=folder_meta.get("date_range") or folder_meta.get("year", ""),
                        )
                        import re as _re
                        destination = _re.sub(r'/{2,}', '/', destination)
                        destination = _re.sub(r'/\s*_\s*/', '/', destination)
                        destination = _re.sub(r'/Unknown/?', '/', destination)
                        destination = _re.sub(r'/None/?', '/', destination)
                        destination = destination.rstrip('/') + '/'
                    except KeyError:
                        pass

    if amount and amount not in proposed_name and doc_type not in ("Unknown", "Rx", "VehicleRegistration", "RegistrationRenewal", "Business", "BusinessChecking", "BusinessCreditCard", "Bill", "MedicalBill", "LabBill", "DentalBill", "HospitalBill", "VisionBill"):
        name_path = Path(proposed_name)
        proposed_name = f"{name_path.stem}_{amount}{name_path.suffix}"

    # For bills, extract and append due date
    if doc_type in ("Bill", "MedicalBill", "LabBill", "DentalBill", "HospitalBill", "VisionBill", "Insurance"):
        due_date = _extract_due_date(normalized)
        if due_date and due_date not in proposed_name:
            name_path = Path(proposed_name)
            proposed_name = f"{name_path.stem}_DueDate_{due_date}{name_path.suffix}"

    # ── Dual confidence: classification_confidence ──
    # Starts at same value as rule-match confidence, then adjusted
    classification_confidence = confidence

    # Reduce for conflicting org matches (multiple orgs from different categories)
    if len(all_org_matches) > 1:
        categories = set(o.category for o in all_org_matches)
        if len(categories) > 1:
            classification_confidence -= 0.15

    # Reduce if identity_card override happened
    if doc_rule and doc_rule.id == "identity_card" and org and org.category not in ("Legal", "Government"):
        classification_confidence -= 0.20

    # Reduce for ambiguous type (matches multiple categories)
    if ambiguous_flag:
        classification_confidence -= 0.10

    # Reduce for low OCR quality (text < 100 chars or very short avg word length)
    if len(normalized) < 100:
        classification_confidence -= 0.15
    else:
        words = normalized.split()
        avg_word_len = sum(len(w) for w in words) / max(len(words), 1)
        if avg_word_len < 3.0:
            classification_confidence -= 0.15

    # Boost if org + type agree (contextual match)
    if org and doc_rule:
        org_type_agree = False
        medical_cats = {"Lab", "Medical", "Pediatric", "Dental", "Vision", "Pharmacy", "Hospital", "PrimaryCare", "BehavioralHealth"}
        if doc_rule.id in ("lab_requisition", "medical_record", "prescription") and org.category in medical_cats:
            org_type_agree = True
        elif doc_rule.id in ("bill", "medical_bill", "insurance") and org.category == "Insurance":
            org_type_agree = True
        elif doc_rule.id == "identity_card" and org.category in ("Legal", "Government"):
            org_type_agree = True
        elif doc_rule.id == "business" and org.category == "Business":
            org_type_agree = True
        if org_type_agree:
            classification_confidence += 0.05

    # ── Check corrections table for learning overrides ──
    correction_applied = False
    try:
        from app.classify.corrections import check_corrections
        correction = check_corrections(
            org_id=org.id if org else None,
            ocr_text=normalized,
            rules=rules,
        )
        if correction:
            # Apply correction: override doc_type, boost confidence
            if correction["corrected_doc_type"] and correction["corrected_doc_type"] != doc_type:
                # Find the corrected doc_type rule
                for dt_rule in rules.document_types:
                    if dt_rule.id == correction["corrected_doc_type"].lower():
                        doc_rule = dt_rule
                        doc_type = dt_rule.labels.get("default", correction["corrected_doc_type"])
                        correction_applied = True
                        break
            if correction.get("corrected_person") and correction["corrected_person"] != "Unknown":
                person = correction["corrected_person"]
            if correction.get("corrected_provider") and correction["corrected_provider"] != "Unknown":
                provider = correction["corrected_provider"]
            # Boost classification confidence for correction match
            classification_confidence += correction.get("confidence_boost", 0.10)
    except Exception:
        pass  # Corrections table may not exist yet; skip silently

    # Clamp classification_confidence to [0, 1]
    classification_confidence = max(0.0, min(1.0, classification_confidence))

    highlights = {
        "docDate": doc_date,
        "amount": amount,
        "provider": provider if provider != "Unknown" else None,
        "person": person,
        "sample": normalized[:240],
        "folderMeta": folder_meta,
        "ambiguous": ambiguous_flag,
        "question": ambiguous_question,
        "medication": medication,
        "brandName": brand_name,
    }

    # Extract clinical details from OCR text
    reason_for_visit = _extract_reason_for_visit(text)
    final_diagnosis = _extract_final_diagnosis(text)
    physician = _extract_physician(text)

    return ClassificationResult(
        doc_type=doc_type,
        person=person,
        provider=provider,
        proposed_name=proposed_name,
        proposed_dest=destination,
        confidence=confidence,
        rule_match_id=rule_match_id,
        highlights=highlights,
        exp_date=exp_date,
        side=side,
        side_confidence=side_confidence,
        needs_side_confirmation=needs_side_confirm if doc_rule and doc_rule.id == "identity_card" else False,
        ambiguous=ambiguous_flag,
        question=ambiguous_question,
        medication=medication,
        brand_name=brand_name,
        reason_for_visit=reason_for_visit,
        final_diagnosis=final_diagnosis,
        physician=physician,
        classification_confidence=classification_confidence,
        auto_approved=False,  # Set later by pipeline
    )


def _match_person(lowered_text: str, rules: CompiledRules) -> str | None:
    for person, aliases in rules.people_aliases.items():
        if any(alias in lowered_text for alias in aliases):
            return person
    return None


def _match_patient(text: str, lowered_text: str, rules: CompiledRules) -> str | None:
    """Detect the patient name from medical/lab documents.
    
    Looks for patterns like 'PATIENT:', 'Patient Name:', 'DIEPPA, ISABELLA' etc.
    Returns the first family person found near a patient label.
    Falls back to the first person mentioned if no patient label is found.
    """
    # Pattern 1: Explicit patient label followed by name
    patient_patterns = [
        re.compile(r'patient\s*(?:name|information)?\s*[:\-]*\s*([\w,\s]+?)(?:\s*(?:M|F|Male|Female|DOB|Date|SSN|MRN|Born|Age|\d))', re.IGNORECASE),
        re.compile(r'PATIENT\s+INFORMATION\s*[:\-]*\s*([\w,\s]+?)(?:\s*(?:M|F|Male|Female|DOB|Date|SSN|MRN))', re.IGNORECASE),
    ]
    for pat in patient_patterns:
        m = pat.search(text)
        if m:
            candidate = m.group(1).strip().lower()
            # Check if any family person appears in this candidate
            for person, aliases in rules.people_aliases.items():
                for alias in aliases:
                    if alias in candidate:
                        return person
    
    # Pattern 2: 'DIEPPA, ISABELLA' format near 'PATIENT' keyword
    # Look for LASTNAME, FIRSTNAME within 100 chars of 'PATIENT'
    patient_idx = lowered_text.find('patient')
    if patient_idx >= 0:
        nearby = lowered_text[max(0, patient_idx-10):patient_idx+200]
        # Find the person who appears closest to 'patient' keyword
        best_person = None
        best_pos = len(nearby)
        for person, aliases in rules.people_aliases.items():
            for alias in aliases:
                pos = nearby.find(alias)
                if pos >= 0 and pos < best_pos:
                    best_person = person
                    best_pos = pos
        if best_person:
            return best_person
    
    return None


def _match_organization(lowered_text: str, rules: CompiledRules):
    """Return the first matching organization (legacy, single-match)."""
    for organization in rules.organizations:
        if rules.org_regexes[organization.id].search(lowered_text):
            return organization
    return None


def _match_organization_contextual(lowered_text: str, rules: CompiledRules, doc_type: str | None = None) -> tuple[Any | None, list[Any]]:
    """Return the best-matching org based on document context, plus all matches.

    Returns (best_org, all_matches) where:
    - best_org: the org most relevant to the doc_type context
    - all_matches: list of all orgs that matched, for provider extraction etc.

    Priority rules by doc_type context:
    - medical/lab/prescription: prefer Lab > Medical/Pharmacy/Provider > Insurance
    - bill types: prefer Insurance > Medical/Lab/Provider
    - identity: prefer Government > other
    - business: prefer Business > other
    """
    all_matches = []
    for organization in rules.organizations:
        if rules.org_regexes[organization.id].search(lowered_text):
            all_matches.append(organization)

    if not all_matches:
        return None, []
    if len(all_matches) == 1:
        return all_matches[0], all_matches

    # Define priority categories by doc_type context
    # Medical/lab/Rx: provider > insurance
    medical_priority = {"Lab", "LabRequisition", "Medical", "Pediatric", "Dental", "Vision",
                        "Pharmacy", "BehavioralHealth", "PrimaryCare", "Hospital"}
    # Bill types: insurance > provider
    bill_priority = {"Insurance", "AutoInsurance"}
    # Identity: government > other
    identity_priority = {"Government", "Legal"}
    # Business: business > other
    business_priority = {"Business"}

    # Determine context from doc_type
    medical_doc_types = {"lab_requisition", "medical_record", "prescription", "eye_exam",
                         "school_entry", "referral"}
    bill_doc_types = {"bill", "medical_bill", "lab_bill", "dental_bill", "hospital_bill",
                      "vision_bill", "insurance"}
    identity_doc_types = {"identity_card"}
    business_doc_types = {"business", "bank_statement", "employment"}

    # Determine priority categories based on context
    priority_cats = set()
    if doc_type in medical_doc_types:
        priority_cats = medical_priority
    elif doc_type in bill_doc_types:
        priority_cats = bill_priority
    elif doc_type in identity_doc_types:
        priority_cats = identity_priority
    elif doc_type in business_doc_types:
        priority_cats = business_priority

    # Find best org: prefer priority category matches
    if priority_cats:
        for org in all_matches:
            if org.category in priority_cats:
                return org, all_matches

    # Fallback: return first match (original behavior)
    return all_matches[0], all_matches


def _match_document_type(lowered_text: str, rules: CompiledRules):
    for rule in rules.document_types:
        pattern = rules.document_type_regexes.get(rule.id)
        if pattern and pattern.search(lowered_text):
            return rule
    return None


def _extract_doc_date(text: str) -> str | None:
    """Extract the document date from OCR text, skipping DOB patterns.
    
    Dates preceded by DOB markers (DOB:, Date of Birth:, etc.) are skipped
    because they represent the person's birth date, not the document date.
    """
    # DOB markers that indicate a date is a birth date, not a document date
    dob_markers = re.compile(
        r'(?:DOB\s*:|date\s+of\s+birth\s*:|born\s*:|birthday\s*:)',
        re.IGNORECASE
    )
    
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            # Check if this date is preceded by a DOB marker
            # Look at text before the match for a DOB marker within 30 chars
            prefix_start = max(0, match.start() - 30)
            prefix = text[prefix_start:match.start()]
            if dob_markers.search(prefix):
                continue  # Skip DOB dates
            
            if "/" in match.group(0):
                mm, dd, yy = match.group(1), match.group(2), match.group(3)
                # Handle 2-digit year (MM/DD/YY)
                if len(yy) == 2:
                    year_int = int(yy)
                    yy = f"{2000 + year_int}" if year_int < 50 else f"{1900 + year_int}"
                return f"{yy}-{mm}-{dd}"
            return match.group(0)
    return None


def _extract_statement_date(text: str) -> str | None:
    """Extract closing/statement date from financial documents.
    Looks for 'Statement Period', 'closing date', 'statement date' etc.
    before the date, so we get the actual statement date instead of
    random dates like 'rate expires MM/DD/YYYY'."""
    lowered = text.lower()
    # Pattern 1: "Statement Period MM/DD/YYYY - MM/DD/YYYY" or "MM/DD/YYYY - MM/DD/YYYY"
    for m in re.finditer(r'statement\s+period\s+(\d{1,2})/(\d{1,2})/(\d{2,4})\s*-\s*(\d{1,2})/(\d{1,2})/(\d{2,4})', lowered):
        # Return the END date of the period
        mm, dd, yy = m.group(4), m.group(5), m.group(6)
        if len(yy) == 2:
            yy = f"{2000 + int(yy)}" if int(yy) < 50 else f"{1900 + int(yy)}"
        return f"{yy}-{mm}-{dd}"
    # Pattern 2: "closing date MM/DD/YYYY" or "closing date MM/DD/YY"
    for m in re.finditer(r'closing\s+date[:\s]*(\d{1,2})/(\d{1,2})/(\d{2,4})', lowered):
        mm, dd, yy = m.group(1), m.group(2), m.group(3)
        if len(yy) == 2:
            yy = f"{2000 + int(yy)}" if int(yy) < 50 else f"{1900 + int(yy)}"
        return f"{yy}-{mm}-{dd}"
    # Pattern 3: "as of MM/DD/YYYY" (common on checking statements)
    for m in re.finditer(r'as\s+of[:\s]*(\d{1,2})/(\d{1,2})/(\d{2,4})', lowered):
        mm, dd, yy = m.group(1), m.group(2), m.group(3)
        if len(yy) == 2:
            yy = f"{2000 + int(yy)}" if int(yy) < 50 else f"{1900 + int(yy)}"
        return f"{yy}-{mm}-{dd}"
    # Pattern 4: "Notice date Month DD, YYYY" (IRS/tax docs)
    for m in re.finditer(r'notice\s+date[:\s]*(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2}),?\s*(\d{4})', lowered):
        month_names = {"january": "01", "february": "02", "march": "03", "april": "04", "may": "05", "june": "06", "july": "07", "august": "08", "september": "09", "october": "10", "november": "11", "december": "12"}
        mm = month_names.get(m.group(1), "01")
        dd = m.group(2).zfill(2)
        yy = m.group(3)
        return f"{yy}-{mm}-{dd}"
    return None


def _extract_amount(text: str) -> str | None:
    match = _AMOUNT_PATTERN.search(text)
    if not match:
        return None
    return match.group(1).replace(",", "")


def _extract_due_date(text: str) -> str | None:
    """Extract due date from bill/statement text.
    Looks for: due date, due, pay before, payment due, pay by, must be received by.
    Returns YYYY-MM-DD or None."""
    due_keywords = [
        "due date",
        "payment due",
        "pay before",
        "pay by",
        "must be received by",
        "due ",  # bare "due" followed by date
    ]
    lowered = text.lower()
    for keyword in due_keywords:
        idx = lowered.find(keyword)
        if idx == -1:
            continue
        # Look for a date pattern in the 80 chars after the keyword
        substring = text[idx:idx+80]
        for pattern in _EXP_DATE_PATTERNS:  # reuse MM/DD/YYYY patterns
            match = pattern.search(substring)
            if match:
                mm, dd, yy = match.group(1), match.group(2), match.group(3)
                if len(yy) == 2:
                    year_int = int(yy)
                    yy = "20" + yy if year_int < 50 else "19" + yy
                return f"{yy}-{mm}-{dd}"
    # Also try: "Due" followed by a date on same or next line
    for pattern in _EXP_DATE_PATTERNS:
        matches = pattern.finditer(text)
        for m in matches:
            # Check if "due" appears within 40 chars before this date
            start = max(0, m.start() - 40)
            prefix = text[start:m.start()].lower()
            if "due" in prefix:
                mm, dd, yy = m.group(1), m.group(2), m.group(3)
                if len(yy) == 2:
                    year_int = int(yy)
                    yy = "20" + yy if year_int < 50 else "19" + yy
                return f"{yy}-{mm}-{dd}"
    return None


def _extract_expiration_date(text: str) -> str | None:
    """Extract expiration date from text. Looks for patterns like 'exp', 'expires', 'expiration'."""
    # Look for expiration keywords followed by date
    exp_keywords = ["exp", "expires", "expiration", "exp date", "exp."]
    for keyword in exp_keywords:
        # Find the keyword position
        idx = text.lower().find(keyword)
        if idx == -1:
            continue
        # Look for a date pattern after the keyword
        substring = text[idx:idx+50]
        for pattern in _EXP_DATE_PATTERNS:
            match = pattern.search(substring)
            if match:
                # Convert to YYYY-MM-DD format
                mm, dd, yy = match.group(1), match.group(2), match.group(3)
                if len(yy) == 2:
                    year_int = int(yy)
                    if year_int < 50:
                        yy = "20" + yy
                    else:
                        yy = "19" + yy
                return f"{yy}-{mm}-{dd}"
    # Also try to find any future date that could be an expiration
    for pattern in _EXP_DATE_PATTERNS:
        matches = pattern.findall(text)
        for mm, dd, yy in matches:
            if len(yy) == 2:
                year_int = int(yy)
                if year_int < 50:
                    yy = "20" + yy
                else:
                    yy = "19" + yy
            year_val = int(yy)
            # If year is in the future, likely an expiration date
            if year_val >= 2026:
                return f"{yy}-{mm}-{dd}"
    return None


def _detect_side_from_filename(stem: str) -> str | None:
    """Check filename stem for Front/Back keywords, including recto/verso."""
    lowered = stem.lower()
    # Match "front"/"back" as:
    #  - Standalone words with delimiters: _front_, -front, front_, etc.
    #  - CamelCase suffixes before _separator: PassportFront_Daniel, DLBack_Natalie
    #  - At end of string: somethingFront, somethingBack
    front_patterns = [
        re.compile(r'(?:^|[\s_\-])front(?:$|[\s_\-])'),
        re.compile(r'[a-z]front(?:$|[\s_\-])'),
        re.compile(r'front$', re.IGNORECASE),
        # Recto = front side (French/European scanners)
        re.compile(r'(?:^|[\s_\-])recto(?:$|[\s_\-])'),
        re.compile(r'[a-z]recto(?:$|[\s_\-])'),
        re.compile(r'recto$', re.IGNORECASE),
    ]
    back_patterns = [
        re.compile(r'(?:^|[\s_\-])back(?:$|[\s_\-])'),
        re.compile(r'[a-z]back(?:$|[\s_\-])'),
        re.compile(r'back$', re.IGNORECASE),
        re.compile(r'(?:^|[\s_\-])(reverso|reverse)(?:$|[\s_\-])'),
        re.compile(r'[a-z](reverso|reverse)(?:$|[\s_\-])'),
        # Verso = back side (French/European scanners)
        re.compile(r'(?:^|[\s_\-])verso(?:$|[\s_\-])'),
        re.compile(r'[a-z]verso(?:$|[\s_\-])'),
        re.compile(r'verso$', re.IGNORECASE),
    ]
    for pat in front_patterns:
        if pat.search(lowered):
            return "Front"
    for pat in back_patterns:
        if pat.search(lowered):
            return "Back"
    return None


def _resolve_destination(
    doc_rule,
    org,
    person: str,
    year: int,
    rules: CompiledRules,
    folder_hint: str | None = None,
    subcategory: str | None = None,
    business_name: str | None = None,
    business_folder: str | None = None,
) -> str:
    """Resolve destination path using category_routing, org.destination, or fallback.

    Priority doc types (Prescription, EyeExam, SchoolEntry) always route via their
    own category_routing entry, even when an org matched with a different category.
    This prevents e.g. a prescription from routing to Insurance archives when
    the org is Florida Blue (Insurance) — prescriptions belong in Health.
    """
    category_routing = rules.routing.get("category_routing", {})

    # Doc types that always get priority routing over org category
    PRIORITY_DOC_CATEGORIES = {"Prescription", "EyeExam", "SchoolEntry", "Business", "Bill", "MedicalBill", "LabBill", "DentalBill", "HospitalBill", "VisionBill"}

    # Determine category
    doc_type_category = doc_rule.id.capitalize() if doc_rule else None
    org_category = org.category if org else None
    folder_category = folder_hint if folder_hint else None

    # If doc type has priority routing, use it instead of org category
    if doc_type_category and doc_type_category in PRIORITY_DOC_CATEGORIES and doc_type_category in category_routing:
        category = doc_type_category
    elif org_category:
        category = org_category
    elif doc_type_category:
        category = doc_type_category
    elif folder_category:
        category = folder_category
    else:
        category = None

    # Check category_routing first (v2 rules)
    if category and category in category_routing:
        route = category_routing[category]
        template = route.get("template") or route.get("archive_template", "")
        if template:
            try:
                result = template.format(
                    person=person,
                    year=year,
                    org_name=org.names[0] if org else "",
                    subcategory=subcategory or "",
                    company=business_folder if business_folder else (business_name if business_name else (org.names[0] if org else "")),
                    date_range="",
                    grade="",
                )
                # Clean up empty template vars that leave double slashes, Unknown, or trailing slashes
                import re as _re
                result = _re.sub(r'/{2,}', '/', result)  # Remove double slashes
                result = _re.sub(r'/\s*_\s*/', '/', result)  # Remove empty _ between slashes
                result = _re.sub(r'/Unknown/?', '/', result)  # Remove Unknown segments
                result = _re.sub(r'/None/?', '/', result)  # Remove None segments
                result = result.rstrip('/')  # Remove trailing slash
                if result:
                    return result + '/'
            except KeyError:
                pass

    # Fall back to org.destination
    if org and org.destination:
        return org.destination

    # Archive routing for past-year docs
    if org and year != rules.current_year and org.category in set(rules.routing.get("archive_categories", [])):
        archive_templates = rules.routing.get("archive_templates", {})
        archive_template = archive_templates.get(org.category, archive_templates.get("default"))
        if archive_template:
            return archive_template.format(category=org.category, person=org.person, year=year)

    # Default fallback
    return rules.default_destination


def _extract_vehicle_detail(text: str) -> str | None:
    """Extract plate/tag number or vehicle info from text."""
    # Look for tag/plate numbers
    plate_match = re.search(r'(?:tag|plate|decal)\s*(?:number|#|no)?[:\s]*(\w{2,})', text, re.IGNORECASE)
    if plate_match:
        return plate_match.group(1)
    return None


def _match_person_from_path(path_str: str, rules: CompiledRules) -> str | None:
    """Detect person from folder/file path using path_person_detection rules."""
    path_person_detection = rules.routing.get("path_person_detection", {})
    if not path_person_detection or not path_person_detection.get("enabled", False):
        return None
    for pattern_rule in path_person_detection.get("patterns", []):
        if re.search(pattern_rule["pattern"], path_str, re.IGNORECASE):
            return pattern_rule["person"]
    return None


def _is_scanner_default_filename(stem: str) -> bool:
    """Check if filename matches a scanner default pattern (no useful info)."""
    scanner_patterns = [
        re.compile(r'^Scan\d{4}-\d{2}-\d{2}_\d{6}', re.IGNORECASE),  # ScanYYYY-MM-DD_HHMMSS
        re.compile(r'^\d{8}_\d{6}_\d{3}$'),  # YYYYMMDD_HHMMSS_NNN
        re.compile(r'^DocFile\s*\(\d+\)$', re.IGNORECASE),  # DocFile (4).jpg
        re.compile(r'^SKMBT_', re.IGNORECASE),  # Scanner/copier prefix SKMBT_C454e...
        re.compile(r'^[A-Z]{2,6}_C\d+[a-z]?\d*', re.IGNORECASE),  # SKMBT_C454e15031115150
        re.compile(r'^\d{8}\s+\w+', re.IGNORECASE),  # 20190312 Bofa Statement 01 (date + scanner words)
    ]
    # Also check for generic patterns that look like scanner output
    # e.g., "S-Corp Doc 01.jpg" — descriptive but not our naming convention
    generic_descriptive = re.compile(r'^(?:S-Corp|Doc|Document|Image|Scan|IMG|Photo|Page|File)\s*\d*$', re.IGNORECASE)
    if generic_descriptive.match(stem):
        return True
    for pattern in scanner_patterns:
        if pattern.match(stem):
            return True
    return False


def _extract_date_from_filename(stem: str) -> str | None:
    """Extract date from filename patterns like YYYYMMDD or YYYY-MM-DD."""
    # Short date: YYYYMMDD
    m = re.match(r'^(\d{4})(\d{2})(\d{2})', stem)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _detect_folder_doc_type(path_str: str, rules: CompiledRules) -> dict | None:
    """Detect document type and metadata hints from parent folder names.
    
    Returns dict with: doc_type_hint, person_hint, company_hint, event_hint, ambiguous, question
    """
    folder_heuristics = rules.routing.get("filename_heuristics", {}).get("folder_name_detection", {})
    if not folder_heuristics or not folder_heuristics.get("enabled", False):
        return None
    for rule in folder_heuristics.get("patterns", []):
        if re.search(rule["folder_pattern"], path_str, re.IGNORECASE):
            return {
                "doc_type": rule.get("doc_type_hint"),
                "person": rule.get("person_hint"),
                "company": rule.get("company_hint"),
                "event": rule.get("event_hint"),
                "ambiguous": rule.get("ambiguous"),
                "question": rule.get("question"),
            }
    return None


def _extract_folder_metadata(path: Path, rules: CompiledRules) -> dict:
    """Parse parent folder structure for date, person, doc_type, company, subcategory, year, date_range, event.
    
    Returns dict with: date, person, doc_type, company, subcategory, year, date_range, event, ambiguous, question
    These are used when OCR/filename confidence is low.
    """
    result = {"date": None, "person": None, "doc_type": None, "company": None, 
              "subcategory": None, "year": None, "date_range": None, "event": None, "event_suffix": None, "ambiguous": None, "question": None}
    
    # Walk up parent folders (closest parent first)
    parts = list(path.parents)
    
    # Detect rich folder hints (doc_type + person + company + event + ambiguous flags)
    folder_hints = _detect_folder_doc_type(str(path), rules)
    if folder_hints:
        if folder_hints.get("doc_type"):
            result["doc_type"] = folder_hints["doc_type"]
        if folder_hints.get("person"):
            result["person"] = folder_hints["person"]
        if folder_hints.get("company"):
            result["company"] = folder_hints["company"]
        if folder_hints.get("event"):
            result["event"] = folder_hints["event"]  # e.g., "Homework" from folder pattern
        if folder_hints.get("ambiguous"):
            result["ambiguous"] = folder_hints["ambiguous"]
        if folder_hints.get("question"):
            result["question"] = folder_hints["question"]
    
    # Also check path-based person detection as fallback
    if not result["person"]:
        person_from_path = _match_person_from_path(str(path), rules)
        if person_from_path:
            result["person"] = person_from_path
    
    # Extract date from folder names - check multiple patterns
    for parent in parts:
        name = parent.name
        if not name or name == "!!!Check":
            continue
        
        # Date range: 2019-2020 (school year format)
        year_range_match = re.match(r'^(\d{4})-(\d{4})', name)
        if year_range_match:
            result["date_range"] = f"{year_range_match.group(1)}-{year_range_match.group(2)}"
            result["year"] = int(year_range_match.group(1))  # Use start year
            continue
        
        # Date embedded in folder name: Employee Evaluation 20191223
        embedded_date = re.search(r'(\d{4})(\d{2})(\d{2})', name)
        if embedded_date and not result["date"]:
            result["date"] = f"{embedded_date.group(1)}-{embedded_date.group(2)}-{embedded_date.group(3)}"
            if not result["year"]:
                result["year"] = int(embedded_date.group(1))
            # Extract event description from the folder name (text around the date)
            event_text = name[:embedded_date.start()] + name[embedded_date.end():]
            event_text = re.sub(r'[^A-Za-z0-9 ]+', ' ', event_text).strip()
            event_text = re.sub(r'\s+', ' ', event_text).strip()
            # Remove common noise words
            noise_words = {'scan', 'img', 'image', 'document', 'doc'}
            event_words = [w for w in event_text.split() if w.lower() not in noise_words and len(w) > 1]
            if event_words and not result["event"]:
                result["event"] = "_".join(event_words)
            elif event_words and not result.get("event_suffix"):
                result["event_suffix"] = "_".join(event_words)
            continue
        
        # Full date: YYYY-MM-DD
        date_match = re.match(r'^(\d{4})-(\d{2})-(\d{2})', name)
        if date_match:
            result["date"] = f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}"
            result["year"] = int(date_match.group(1))
            continue
        
        # Compact date at start with event text: 20181128 Daycare Party
        compact_date = re.match(r'^(\d{4})(\d{2})(\d{2})\s+(.+)', name)
        if compact_date:
            result["date"] = f"{compact_date.group(1)}-{compact_date.group(2)}-{compact_date.group(3)}"
            result["year"] = int(compact_date.group(1))
            # Extract event from the text after the date
            event_text = compact_date.group(4).strip()
            event_text = re.sub(r'[^A-Za-z0-9 ]+', ' ', event_text).strip()
            event_text = re.sub(r'\s+', ' ', event_text).strip()
            noise_words = {'scan', 'img', 'image', 'document', 'doc'}
            event_words = [w for w in event_text.split() if w.lower() not in noise_words and len(w) > 1]
            if event_words and not result["event"]:
                result["event"] = "_".join(event_words)
            elif event_words and not result.get("event_suffix"):
                result["event_suffix"] = "_".join(event_words)
            continue
        
        # Compact date only (no event text): YYYYMMDD
        compact_date_only = re.match(r'^(\d{4})(\d{2})(\d{2})$', name)
        if compact_date_only:
            result["date"] = f"{compact_date_only.group(1)}-{compact_date_only.group(2)}-{compact_date_only.group(3)}"
            result["year"] = int(compact_date_only.group(1))
            continue
        
        # Single year: 2025, 2019
        year_match = re.match(r'^(\d{4})', name)
        if year_match and not result["year"]:
            result["year"] = int(year_match.group(1))
            continue
        
        # Detect company/org from folder names (if not already set)
        if not result["company"]:
            lowered_name = name.lower()
            for org in rules.organizations:
                for org_name in org.names:
                    if org_name.lower() in lowered_name:
                        result["company"] = org.names[0]
                        if not result["person"] and org.person:
                            result["person"] = org.person
                        break
        
        # Extract event description from non-date folder names
        # (e.g., "Daycare Party", "Employee Recognition")
        if not result["event"] or not result.get("event_suffix"):
            # Skip generic/unhelpful folder names
            skip_folders = {'check', 'scans', 'inbox', 'new', 'pending', 'todo', '!!!check', 'export', 'scan'}
            clean_name = name.strip()
            if clean_name.lower() not in skip_folders and len(clean_name) > 2:
                # Remove any embedded dates from the name to get pure event text
                event_text = re.sub(r'\d{4}\d{4}', '', clean_name)  # Remove YYYYMMDD
                event_text = re.sub(r'\d{4}-\d{2}-\d{2}', '', event_text)  # Remove YYYY-MM-DD
                event_text = re.sub(r'[^A-Za-z0-9 ]+', ' ', event_text).strip()
                event_text = re.sub(r'\s+', ' ', event_text).strip()
                noise_words = {'scan', 'img', 'image', 'document', 'doc', 'pics', 'pictures', 'photos'}
                event_words = [w for w in event_text.split() if w.lower() not in noise_words and len(w) > 1]
                if event_words:
                    event_str = "_".join(event_words)
                    if not result["event"]:
                        result["event"] = event_str
                    elif not result.get("event_suffix"):
                        result["event_suffix"] = event_str
    
    # Detect subcategory from folder names
    subcategory_map = {
        "checking statements": "Checking",
        "credit card statements": "CreditCard",
        "credit card": "CreditCard",
        "savings statements": "Savings",
        "irs documentation": "IRS",
        "irs": "IRS",
        "eye exams": "Eye-Exams",
        "dental": "Dental",
        "vision": "Vision",
        "bofa statements": "BusinessChecking",
        "bankofamerica": "BusinessChecking",
        "bank of america": "BusinessChecking",
        "sunbiz payment": "GovernmentFiling",
        "sunbiz": "GovernmentFiling",
        "corporation docs": "SCorporationElection",
    }
    for parent in parts:
        name = parent.name
        if not name or name == "!!!Check":
            continue
        lowered = name.lower()
        for key, val in subcategory_map.items():
            if key in lowered:
                result["subcategory"] = val
                break
    
    return result


def _detect_side_sequential(person: str, doc_type: str, doc_date: str) -> tuple[str | None, float]:
    """Detect Front/Back based on recent scans of same document.

    Returns (side, confidence) where:
    - side: "Front", "Back", or None
    - confidence: 0.0 to 1.0 (1.0 = very confident, 0.0 = no data)
    """
    import time

    cache_key = (person, doc_type, doc_date)
    now = time.time()

    # Clean up expired entries
    expired = [k for k, v in _recent_id_docs.items() if v and now - v[-1][0] > _RECENT_WINDOW_SECONDS]
    for k in expired:
        del _recent_id_docs[k]

    if cache_key in _recent_id_docs:
        entries = _recent_id_docs[cache_key]
        # Check last entry
        last_ts, last_side = entries[-1]
        # If recent scan was Front, this is likely Back
        if last_side == "Front":
            entries.append((now, "Back"))
            return ("Back", 0.95)
        # If we already have Front+Back, don't guess further
        elif last_side == "Back":
            entries.append((now, "Unknown"))
            return (None, 0.0)
        else:
            return (None, 0.0)
    else:
        # No recent scan - assume this is Front
        _recent_id_docs[cache_key] = [(now, "Front")]
        return ("Front", 0.95)


def _extract_reason_for_visit(text: str) -> str | None:
    """Extract 'Reason For Visit' from discharge/ER docs."""
    patterns = [
        r"Reason\s+For\s+Visit\s*[:\-]\s*(.+?)(?:\n|$)",
        r"Chief\s+Complaint\s*[:\-]\s*(.+?)(?:\n|$)",
        r"Reason\s+for\s+visit\s*[:\-]\s*(.+?)(?:\n|$)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()[:200]
    return None


def _extract_final_diagnosis(text: str) -> str | None:
    """Extract 'Final Diagnosis' from discharge/ER docs."""
    patterns = [
        r"Final\s+Diagnosis\s*[:\-]\s*(.+?)(?:\n|$)",
        r"Admission\s+Diagnosis\s*[:\-]\s*(.+?)(?:\n|$)",
        r"Principal\s+Diagnosis\s*[:\-]\s*(.+?)(?:\n|$)",
        r"Discharge\s+Diagnosis\s*[:\-]\s*(.+?)(?:\n|$)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()[:200]
    return None


def _extract_physician(text: str) -> str | None:
    """Extract attending physician name from medical docs."""
    patterns = [
        # "LastName, FirstName MiddleInit MD" pattern (very common in medical docs)
        r'([A-Z][a-z]+,\s+[A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+MD\b)',
        r'Primary\s+Physician\s*[:\-]+\s*([A-Z][a-z]+[^\n]+?)(?:\n|$)',
        r'Attending\s+Physician\s*[:\-]\s*(.+?)(?:\n|$)',
    ]
    # Extract patient name to avoid false positives
    patient_match = re.search(r'Patient(?:\s+Name)?\s*[:\-]+\s*(?:Dieppa|Jo),\s+(?:Daniel|Natalie|Isabella|Grisell)', text, re.IGNORECASE)

    for pattern in patterns:
        for m in re.finditer(pattern, text):
            name = m.group(1).strip()[:100]
            # Clean OCR noise
            name = re.sub(r'\s{2,}', ' ', name).strip()
            name = re.sub(r'[^a-zA-Z,\s\\.]$', '', name).strip()
            if name and name.lower() not in ('none', 'unknown', 'n/a', 'none, doctor', '.', '..', '...'):
                # Skip if it matches the patient name
                if patient_match and any(p in name for p in ['Dieppa, Daniel', 'Jo, Natalie', 'Dieppa, Isabella']):
                    continue
                return name
    return None
