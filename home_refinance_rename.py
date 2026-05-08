#!/usr/bin/env python3
"""Batch rename Home Refinance files with proper naming convention.

All files move from inbox to: E:\\QSync\\04-Archives\\Finance\\Home_Refinance\\
Preserving subfolder structure, renaming files with proper names.

Naming convention: YYYY-MM-DD_Description.ext
- Use document date from filename/folder when available
- Fall back to scan date (file mtime) for scanner-default names
- Normalize spaces to underscores
"""

import os
import re
import json
import shutil
from pathlib import Path
from datetime import datetime

INBOX = Path("/mnt/e/Qsync-Scanned-Documents/!!!Check/Home Refinance")
DEST_ROOT = Path("/mnt/e/QSync/04-Archives/Finance/Home_Refinance")

def extract_date_from_name(name: str) -> str | None:
    """Try to extract YYYY-MM-DD or YYYYMMDD date from filename.
    Skip scanner default names like scan211240852 (invalid year)."""
    lower = name.lower()
    if re.match(r'^(scan|img|dsc|p)\d{5,}', lower):
        return None
    # YYYY-MM-DD
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', name)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 2000 <= year <= 2030 and 1 <= month <= 12 and 1 <= day <= 31:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # YYYYMMDD
    m = re.search(r'(\d{4})(\d{2})(\d{2})', name)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 2000 <= year <= 2030 and 1 <= month <= 12 and 1 <= day <= 31:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None

def get_mtime_date(path: Path) -> str:
    """Get file modification time as YYYY-MM-DD."""
    try:
        mtime = os.path.getmtime(path)
        return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
    except:
        return "0000-00-00"

def clean_name(name: str) -> str:
    """Normalize: replace spaces with underscores, remove problematic chars."""
    name = name.strip()
    name = re.sub(r'\s+', '_', name)
    name = name.rstrip('_.')
    # Remove leading/trailing parens
    name = name.strip('()')
    return name

def is_scanner_default(name: str) -> bool:
    """Check if filename is a scanner default (Scan*, scan*, img*, scan211*)."""
    lower = name.lower()
    return bool(re.match(r'^(scan|img|dsc|p)\d', lower))


# ── Per-file rename mapping ──────────────────────────────────────

# Explicit rename overrides for files we can name precisely
EXPLICIT_RENAMES = {
    # Root level
    "2021-01-28_signed credit inquiry letter of explanation.jpg": "2021-01-28_CreditInquiryLetter_Signed_Daniel.jpg",
    "signe inquiry explanation 2021-01-28_163421.jpg": "2021-01-28_CreditInquiryLetter_Signed_Daniel_002.jpg",
    "Dieppa.FT.docx": "2021-01-19_Dieppa_FinancialTemplate.docx",
    "Dieppa.FT.rev.docx": "2021-01-19_Dieppa_FinancialTemplate_Revised.docx",
    "Dieppa.LQI.CIL .pdf": "2021-01-19_Dieppa_LQI_CIL.pdf",
    "Employment Acknowledgement 2021-02-12_134203.jpg": "2021-02-12_EmploymentAcknowledgement_Daniel.jpg",
    "Loan_20250267283_Disclosures_for_eSignature.pdf": "2021-01-19_LoanDisclosures_eSignature_Daniel.pdf",

    # Credit Report
    "20201126 Equifax Credit Report.pdf": "2020-11-26_Equifax_CreditReport.pdf",
    "20201126 TransUnion Credit Report.pdf": "2020-11-26_TransUnion_CreditReport.pdf",

    # Final Signed Docs
    "Dieppa_20250267283_161020_ALL.pdf": "2021-01-19_Dieppa_FinalSignedDocs_ALL.pdf",
    "Dieppa_20250267283_161020_ALL (Natalie consent).pdf": "2021-01-19_Dieppa_FinalSignedDocs_NatalieConsent.pdf",

    # Home Insurance Bill
    "Heritage Insurance 2020.jpg": "2020_Heritage_HomeInsurance.jpg",

    # Lender Docs - top level
    "Borrowers Authorization.pdf": "2020-12-07_BorrowersAuthorization.pdf",
    "Credit Score Disclosure Exception for Loans Secured by One to Four Units of Residential Real Property.pdf": "2020-12-07_CreditScoreDisclosure_Exception.pdf",
    "Dieppa 30 yr with 600$ credit.pdf": "2021-01-19_Dieppa_30yr_LoanTerms.pdf",
    "Dieppa.Address.pdf": "2021-01-19_Dieppa_AddressProof.pdf",
    "Dieppa.Property.pdf": "2021-01-19_Dieppa_PropertyProof.pdf",
    "SSA-89 Borrower.pdf": "2020-12-07_SSA89_Borrower.pdf",
    "Summary of intended use of loan proceeds.docx": "2021-01-19_SummaryIntendedUse_Proceeds.docx",
    "Work Status.docx": "2021-01-19_WorkStatus.docx",

    # Lender Docs / 20210119
    "Cash out letter.docx": "2021-01-19_CashOutLetter.docx",
    "HomeSurveyImage.jpg": "2021-01-19_HomeSurvey.jpg",
    "Remote letter.docx": "2021-01-19_RemoteLetter.docx",
    "dieppa.address.jpg": "2021-01-19_Dieppa_AddressProof.jpg",
    "dieppa.property.jpg": "2021-01-19_Dieppa_PropertyProof.jpg",
    "20201102 Home Mortgage Statement.pdf": "2020-11-02_HomeMortgageStatement.pdf",
    "20201214 Home Mortgage Statement.pdf": "2020-12-14_HomeMortgageStatement.pdf",
    "20210104 Home Mortgage Statement.pdf": "2021-01-04_HomeMortgageStatement.pdf",

    # Lender Docs / signed
    "Borrower-Authorization-2020-12-07_161709_000.jpg": "2020-12-07_BorrowersAuthorization_Signed_000.jpg",
    "Borrower-Authorization-2020-12-07_161709_001.jpg": "2020-12-07_BorrowersAuthorization_Signed_001.jpg",
    "Loan_20250267283_Disclosures_for_eSignature_signed.pdf": "2021-01-19_LoanDisclosures_eSignature_Signed.pdf",
    "SSA-89-Borrower-2020-12-07_161848_000.jpg": "2020-12-07_SSA89_Borrower_Signed_000.jpg",
    "SSA-89-Borrower-2020-12-07_161848_001.jpg": "2020-12-07_SSA89_Borrower_Signed_001.jpg",
    "SSA-89-Borrower-2020-12-07_161848_002.jpg": "2020-12-07_SSA89_Borrower_Signed_002.jpg",
    "dieppa.address.jpg": "2021-01-19_Dieppa_AddressProof.jpg",  # duplicate key handled below
    "dieppa.property.jpg": "2021-01-19_Dieppa_PropertyProof.jpg",

    # Mortgage Statements (top level)
    # PayStub
    "20201120.pdf": "2020-11-20_PayStub_Daniel.pdf",
    "20201127.pdf": "2020-11-27_PayStub_Daniel.pdf",
    "PayStub 20201120.pdf": "2020-11-20_PayStub_Daniel.pdf",
    "PayStub 20201127.pdf": "2020-11-27_PayStub_Daniel.pdf",

    # Taxes
    "2019 Home Taxes.jpg": "2019_HomeTaxes_Daniel.jpg",
    "2019 w2.jpg": "2019_W2_Daniel.jpg",

    # Wells Fargo
    # nany consent
    "2021-01-26 16_38_29-Great Purchase & Refinance Rates _ Consumer Direct Mortgage and 9 more pages - P.png": "2021-01-26_MortgageRates_ConsumerDirect_NatalieConsent_001.png",
    "2021-01-26 16_39_26-Great Purchase & Refinance Rates _ Consumer Direct Mortgage.png": "2021-01-26_MortgageRates_ConsumerDirect_NatalieConsent_002.png",
}


# Destination subfolder mapping
DEST_SUBFOLDERS = {
    "Credit Report": "Credit_Report",
    "Home Insurance Bill": "Home_Insurance",
    "Final Signed Docs": "Final_Signed_Docs",
    "Lender Docs": "Lender_Docs",
    "Mortgage Statements": "Mortgage_Statements",
    "PayStub": "PayStubs",
    "Wells Fargo Fund Received": "Wells_Fargo",
    "Wells Fargo Lien Release Document": "Wells_Fargo",
    "nany consent": "Natalie_Consent",
}

# Tax subfolder mapping
TAX_SUBFOLDERS = {
    "Personal Taxes/2018 Daniel Personal Taxes": "Taxes/2018_Daniel_Personal_Taxes",
    "Personal Taxes/2019 Daniel Personal Taxes": "Taxes/2019_Daniel_Personal_Taxes",
    "Company Taxes/2018 IcodeCaliberInc Taxes": "Taxes/2018_IcodeCaliberInc",
    "Company Taxes/2019 IT Development Experts Inc Taxes": "Taxes/2019_ITDevelopmentExperts",
}

# Tax folder year for scanner-default files
TAX_FOLDER_YEAR = {
    "Personal Taxes/2018 Daniel Personal Taxes": "2018",
    "Personal Taxes/2019 Daniel Personal Taxes": "2019",
    "Company Taxes/2018 IcodeCaliberInc Taxes": "2018",
    "Company Taxes/2019 IT Development Experts Inc Taxes": "2019",
}

# Tax folder description for scanner-default files
TAX_FOLDER_DESC = {
    "Personal Taxes/2018 Daniel Personal Taxes": "DanielPersonalTaxes",
    "Personal Taxes/2019 Daniel Personal Taxes": "DanielPersonalTaxes",
    "Company Taxes/2018 IcodeCaliberInc Taxes": "IcodeCaliberInc",
    "Company Taxes/2019 IT Development Experts Inc Taxes": "ITDevelopmentExperts",
}


def resolve_dest_subfolder(rel_path: Path) -> str:
    """Determine destination subfolder from relative path."""
    parts = rel_path.parts

    # Root-level files (no subfolder)
    if len(parts) == 1:
        return ""

    top = parts[0] if parts else ""

    # Taxes - special handling
    if top == "Taxes":
        subpath = "/".join(parts[:-1])  # everything except filename
        for tax_key, tax_dest in TAX_SUBFOLDERS.items():
            if tax_key in subpath:
                # Check for Mortgage Statements nested under Lender Docs 20210119
                if "Mortgage Statements" in subpath:
                    return "Lender_Docs/2021-01-19_Package/Mortgage_Statements"
                return tax_dest
        return "Taxes"

    if top in DEST_SUBFOLDERS:
        dest = DEST_SUBFOLDERS[top]
        # Handle nested subfolders
        if len(parts) > 1:
            sub = parts[1]
            if top == "Lender Docs":
                if sub == "signed":
                    return "Lender_Docs/Signed"
                elif sub == "20210119":
                    # Check for Mortgage Statements
                    if len(parts) > 2 and "Mortgage Statements" in parts[2]:
                        return "Lender_Docs/2021-01-19_Package/Mortgage_Statements"
                    return "Lender_Docs/2021-01-19_Package"
        return dest

    return clean_name(top) if top else ""


def resolve_proposed_name(rel_path: Path) -> str:
    """Determine proposed filename."""
    filename = rel_path.parts[-1]
    ext = Path(filename).suffix.lower()
    stem = Path(filename).stem

    # Check explicit renames first
    if filename in EXPLICIT_RENAMES:
        return EXPLICIT_RENAMES[filename]

    # Scanner defaults
    if is_scanner_default(stem):
        parts = rel_path.parts
        top = parts[0] if parts else ""
        subpath = "/".join(parts[:-1])

        # Tax scanner files
        if top == "Taxes":
            for tax_key, tax_year in TAX_FOLDER_YEAR.items():
                if tax_key in subpath:
                    desc = TAX_FOLDER_DESC[tax_key]
                    # Extract scan date from ScanYYYY-MM-DD pattern
                    scan_date = extract_date_from_name(filename)
                    date = scan_date if scan_date else tax_year
                    # Sequential suffix
                    seq_match = re.search(r'_(\d{3})$', stem)
                    suffix = f"_{seq_match.group(1)}" if seq_match else ""
                    return f"{date}_{desc}{suffix}{ext}"

        # Home Insurance scanner files
        if top == "Home Insurance Bill":
            scan_date = extract_date_from_name(filename)
            date = scan_date or get_mtime_date(INBOX / rel_path)
            seq_match = re.search(r'_(\d{3})$', stem)
            suffix = f"_{seq_match.group(1)}" if seq_match else ""
            return f"{date}_HomeInsurance{suffix}{ext}"

        # Wells Fargo scanner files
        if top == "Wells Fargo Fund Received":
            scan_date = extract_date_from_name(filename) or get_mtime_date(INBOX / rel_path)
            seq_match = re.search(r'_(\d{3})$', stem)
            suffix = f"_{seq_match.group(1)}" if seq_match else ""
            return f"{scan_date}_WellsFargo_FundReceived{suffix}{ext}"

        if top == "Wells Fargo Lien Release Document":
            scan_date = extract_date_from_name(filename) or get_mtime_date(INBOX / rel_path)
            seq_match = re.search(r'_(\d{3})$', stem)
            suffix = f"_{seq_match.group(1)}" if seq_match else ""
            return f"{scan_date}_WellsFargo_LienRelease{suffix}{ext}"

        # Generic fallback
        scan_date = extract_date_from_name(filename) or get_mtime_date(INBOX / rel_path)
        seq_match = re.search(r'_(\d{3})$', stem)
        suffix = f"_{seq_match.group(1)}" if seq_match else ""
        desc = clean_name(top) if top else "Unknown"
        return f"{scan_date}_{desc}{suffix}{ext}"

    # Non-scanner files - clean up the name
    doc_date = extract_date_from_name(filename)

    if doc_date:
        # Already has date - just clean spaces/underscores
        return clean_name(filename)
    else:
        # No date - prepend mtime date
        mtime = get_mtime_date(INBOX / rel_path)
        return f"{mtime}_{clean_name(filename)}"


def main():
    """Generate full rename plan."""
    if not INBOX.exists():
        print(f"❌ Inbox not found: {INBOX}")
        return

    files = sorted(INBOX.rglob("*"))
    files = [f for f in files if f.is_file() and f.suffix.lower() not in {'.zip', '.7z', '.rar'}]

    print(f"Found {len(files)} files\n")

    proposals = []
    for f in files:
        rel = f.relative_to(INBOX)
        dest_folder = resolve_dest_subfolder(rel)
        proposed_name = resolve_proposed_name(rel)
        dest_path = DEST_ROOT / dest_folder / proposed_name if dest_folder else DEST_ROOT / proposed_name

        proposals.append({
            "original": str(rel),
            "proposed_name": proposed_name,
            "dest_folder": dest_folder,
            "dest_path": str(dest_path),
        })

    # Fix conflicts: detect duplicate proposed_name+dest_folder and add sequential suffixes
    by_dest = {}
    for p in proposals:
        key = (p["dest_folder"], p["proposed_name"])
        if key not in by_dest:
            by_dest[key] = []
        by_dest[key].append(p)

    for key, items in by_dest.items():
        if len(items) > 1:
            for i, p in enumerate(items):
                stem = Path(p["proposed_name"]).stem
                ext = Path(p["proposed_name"]).suffix.lower()
                p["proposed_name"] = f"{stem}_{i:03d}{ext}"
                dest_folder = p["dest_folder"]
                p["dest_path"] = str(DEST_ROOT / dest_folder / p["proposed_name"]) if dest_folder else str(DEST_ROOT / p["proposed_name"])

    # Group by destination folder for display
    by_folder = {}
    for p in proposals:
        folder = p["dest_folder"] or "(root)"
        if folder not in by_folder:
            by_folder[folder] = []
        by_folder[folder].append(p)

    for folder in sorted(by_folder.keys()):
        print(f"\n📁 {folder}/")
        for p in by_folder[folder]:
            orig = p["original"]
            if len(orig) > 80:
                orig = "..." + orig[-77:]
            print(f"  {orig}")
            print(f"    → {p['proposed_name']}")

    # Save plan as JSON
    plan_path = Path("/home/ddieppa/.openclaw/workspace/scan-pipeline-v3/home_refinance_plan.json")
    with open(plan_path, "w") as f:
        json.dump(proposals, f, indent=2)
    print(f"\n💾 Plan saved to {plan_path}")
    print(f"Total files: {len(proposals)}")


def execute(dry_run=True):
    """Execute the rename plan."""
    plan_path = Path("/home/ddieppa/.openclaw/workspace/scan-pipeline-v3/home_refinance_plan.json")
    if not plan_path.exists():
        print("❌ No plan file found. Run main() first.")
        return

    with open(plan_path) as f:
        proposals = json.load(f)

    moved = 0
    errors = 0
    for p in proposals:
        src = INBOX / p["original"]
        dst = Path(p["dest_path"])
        if not src.exists():
            print(f"⚠️ Source missing: {src}")
            errors += 1
            continue
        if dry_run:
            print(f"  {src} → {dst}")
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(src), str(dst))
                moved += 1
            except Exception as e:
                print(f"❌ Error moving {src}: {e}")
                errors += 1

    if dry_run:
        print(f"\n🔍 Dry run: {len(proposals)} files would be moved")
    else:
        print(f"\n✅ Moved {moved} files, {errors} errors")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--execute":
        execute(dry_run=False)
    elif len(sys.argv) > 1 and sys.argv[1] == "--dry-run":
        execute(dry_run=True)
    else:
        main()