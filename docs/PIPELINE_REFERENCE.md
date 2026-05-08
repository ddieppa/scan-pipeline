# Scan Pipeline v3 — Complete Reference

## Overview

The scan pipeline takes physical documents from your scanner, OCRs them, classifies them using rules and pattern matching, proposes a filename and PARA destination, and asks you for approval before moving them into QSync.

---

## Folder Layout

### Pipeline Code & State (WSL native ext4 — fast)

```
/home/ddieppa/.openclaw/workspace/scan-pipeline-v3/
├── scan_workflow.py          ← CLI entry point (scan, approve, run, watch, lifecycle, index, etc.)
├── config/
│   ├── scan_rules.yaml       ← All rules: people, orgs, doc types, routing, auto-approve, medications
│   ├── file_types.yaml       ← File type detection config
│   ├── notifications.yaml    ← Notification templates
│   └── rule_suggestions.yaml ← Auto-generated rule suggestions from corrections
├── app/
│   ├── pipeline.py            ← Main pipeline: collect → OCR → classify → dedup → lifecycle → output
│   ├── coordinator.py         ← File move logic: approve_proposal(), _cleanup_empty_parents()
│   ├── move_helper.py         ← Safe file move with verification
│   ├── sidecar.py             ← .meta.json and .ocr.txt sidecar generation
│   ├── settings.py            ← Settings (paths, workers, webhook config)
│   ├── utils.py               ← Shared utilities (normalize_spaces, sha256_file, etc.)
│   ├── classify/
│   │   ├── engine.py          ← Classification engine (classify_document, _match_organization, _match_patient, etc.)
│   │   ├── config.py          ← Rule loading, compilation, compiled matcher objects
│   │   └── corrections.py     ← Classification corrections DB (learn from overrides)
│   ├── extractors/
│   │   ├── pdf.py             ← PDF text extraction (pdftotext + Tesseract OCR fallback)
│   │   ├── images.py          ← Image OCR (Tesseract)
│   │   ├── docx.py            ← DOCX extraction
│   │   ├── xlsx.py            ← XLSX extraction
│   │   ├── common.py          ← ExtractionResult dataclass, shared extraction logic
│   │   └── quality.py         ← OCR text quality assessment + automatic re-extraction
│   ├── state/
│   │   ├── scan_db.py         ← SQLite DB: ocr_cache, scan_results, file_index, classification_corrections, scan_lifecycle, scan_proposals
│   │   └── store.py           ← JSON-based state store (batches, proposals, feedback)
│   ├── notifications/
│   │   └── render.py          ← Telegram notification rendering
│   ├── duplicates/
│   │   └── index.py           ← Legacy duplicate index (filesystem-based, replaced by SQLite file_index)
│   └── watcher/
│       └── bridge.py           ← Watcher-to-pipeline bridge
├── state-data/
│   ├── scan_history.db        ← SQLite DB (all persistent state)
│   ├── batches.json           ← Scan batch history
│   ├── proposals.json         ← Pending proposals awaiting approval
│   ├── feedback.jsonl         ← User feedback log
│   └── last_scan_results.json ← Most recent scan results
└── tests/
    ├── test_approvals.py
    ├── test_config_and_notifications.py
    ├── test_state_store.py
    └── test_watcher.py
```

### Scanner Directories (WSL native ext4)

```
/home/ddieppa/scanner/
├── inbox/          ← Windows scanner app writes here (\\wsl.localhost\Ubuntu\home\ddieppa\scanner\inbox)
├── processing/     ← Files staged for pipeline (moved here by watcher)
├── logs/
│   ├── watcher.log
│   ├── watcher-stdout.log
│   ├── watcher-stderr.log
│   └── fallback.log
└── scripts/
    └── watch-inbox.sh    ← inotifywait-based watcher daemon
```

### QSync (Windows E:\ via 9P — only accessed for reading/moving files)

```
E:\QSync\                          ← PARA method root
├── 01-Projects\                    ← Active projects
├── 02-Areas\                       ← Active areas of responsibility
│   ├── Family\
│   │   ├── Daniel\Health\
│   │   │   ├── Dental\Records\
│   │   │   ├── Hospitalization\
│   │   │   ├── Prescriptions\
│   │   │   ├── Providers\
│   │   │   └── Vision\Eye-Exams\
│   │   ├── Natalie\Health\
│   │   ├── Isabella\
│   │   │   ├── Activities\
│   │   │   ├── Creative_Work\2025\
│   │   │   ├── Education\2025-2026_Grade_4\
│   │   │   └── Health\
│   │   │       ├── Dental\Records\ & Referrals\
│   │   │       ├── Hospitalization\
│   │   │       ├── Lab\
│   │   │       ├── Prescriptions\
│   │   │       ├── Providers\
│   │   │       │   ├── Bagnell Brain Center\
│   │   │       │   ├── MoreThanWordsTherapy\
│   │   │       │   ├── Santana Mental Health Services\
│   │   │       │   └── Beyond the Scale Therapy\
│   │   │       ├── School\
│   │   │       └── Vision\Eye-Exams\
│   │   └── Insurance\Provider_Contacts\
│   ├── Business\
│   │   ├── ND_Tek_Solutions\
│   │   ├── ND_Tek_Minds\
│   │   ├── IcodeCaliber_Inc\
│   │   └── IT_Development_Experts\
│   └── Legal\Identity\{Person}\
├── 03-Resources\                   ← Reference material
└── 04-Archives\                    ← Inactive/archived
    ├── Digital\                    ← AI chat exports, scans
    ├── Finance\
    │   ├── Medical_Bills\{Person}\{Year}\
    │   └── Insurance\
    └── Employment\
```

### Sidecar Files (stored alongside documents in QSync)

Every document can have two sidecar files that enrich the duplicate index:

```
2020-01-21_BaptistHealth_ERVisit_NauseaVomiting_Daniel_000.pdf    ← The document
2020-01-21_BaptistHealth_ERVisit_NauseaVomiting_Daniel_000.meta.json  ← Structured metadata
2020-01-21_BaptistHealth_ERVisit_NauseaVomiting_Daniel_000.ocr.txt     ← Raw OCR text
```

**meta.json** contains: provider, date, patient, doc_type, description, physician, MRN, etc.
**ocr.txt** contains: full extracted text (used for OCR hash-based fuzzy matching)

---

## Pipeline Flow (Step by Step)

### Phase 1: File Detection

```
Windows Scanner App
  ↓ saves PDF/image to \\wsl.localhost\Ubuntu\home\ddieppa\scanner\inbox
  = /home/ddieppa/scanner/inbox/ (native ext4)
  
inotifywait detects close_write/moved_to events
  ↓ 10-second debounce (wait for file size to stabilize)
  ↓ flock prevents concurrent runs
  
watch-inbox.sh moves file to /home/ddieppa/scanner/processing/
  ↓ triggers OpenClaw cron job (scan inbox processor)
```

**3-tier fallback** if OpenClaw is down:
1. `openclaw cron run <job-id>` — preferred, uses main session
2. `openclaw cron wake "New scan files detected"` — wake the main session
3. Direct Python + Telegram — runs pipeline directly, sends Telegram notification

### Phase 2: OCR Extraction

```
scan_workflow.py scan
  ↓ collect_inbox_files() — finds all supported files (.pdf, .jpg, .png, .tif, etc.)
  ↓ _process_single_file() for each file:
  │
  ├─ Check OCR cache (by SHA256) — skip if already extracted
  ├─ Determine file type (PDF, image, DOCX, XLSX)
  ├─ Extract text:
  │   ├─ PDF: pdftotext first (fast)
  │   ├─ Image: Tesseract OCR
  │   ├─ DOCX: python-docx
  │   └─ XLSX: openpyxl
  │
  ├─ OCR Quality Assessment (quality.py):
  │   ├─ Score text 0.0-1.0 (word count, garbled text detection, avg word length)
  │   └─ If quality < 0.3 and source was pdftotext → re-run with Tesseract OCR
  │
  └─ Cache result in ocr_cache table (SHA256 → text, source, quality score)
```

### Phase 3: Classification

```
classify_document(ocr_text, filename, folder_path, rules)
  │
  ├─ 1. Check corrections DB first
  │   └─ If past overrides exist for similar docs → apply learned corrections
  │
  ├─ 2. Match document type (scan_rules.yaml patterns)
  │   ├─ identity_card: "driver license", "passport", "SSN", etc.
  │   ├─ lab_requisition: "ereq", "requisition", "Quest Diagnostics", etc.
  │   ├─ medical_record: "discharge summary", "lab results", etc.
  │   ├─ prescription, eye_exam, dental, bill, receipt, etc.
  │   └─ Each type has patterns, filename template, and labels
  │
  ├─ 3. Match organization (two-pass contextual)
  │   ├─ Pass 1: Get doc_type without org context
  │   ├─ Pass 2: Re-resolve org with doc_type context
  │   │   ├─ Medical/lab docs → prefer Provider/Lab orgs over Insurance
  │   │   ├─ Bills → prefer Insurance orgs
  │   │   └─ Identity → prefer Government orgs
  │   └─ Identity card override: if Lab/Medical org detected, demote identity_card
  │
  ├─ 4. Match person (from OCR text + org person default)
  │   ├─ _match_patient(): For medical docs, prefer patient over guarantor
  │   ├─ People aliases: Daniel, Natalie, Isabella, Nala + nicknames
  │   └─ Org default person (e.g., Kendall Pediatric → Isabella)
  │
  ├─ 5. Detect document date
  │   ├─ Priority: real document date from content > filename date > mtime
  │   └─ Handle multiple date formats (MM/DD/YYYY, YYYY-MM-DD, Month DD YYYY, etc.)
  │
  ├─ 6. Extract extras
  │   ├─ Medication: normalized via medication_map (generic ↔ brand ↔ aliases)
  │   ├─ Expiration date: for identity docs
  │   └─ Side detection: front/back/recto/verso from filename + sequential pairing
  │
  ├─ 7. Compute confidence scores
  │   ├─ rule_match_confidence: how well patterns matched (0-1)
  │   └─ classification_confidence: how certain the TYPE is correct (0-1)
  │       ├─ Reduced by: conflicting orgs (-0.15), type override (-0.20), ambiguity (-0.10), low OCR (-0.15)
  │       └─ Boosted by: org+type agree (+0.05), corrections match (+0.10)
  │
  └─ 8. Generate proposed filename
      ├─ Template from doc type: "{date}_{provider}_LabRequisition_{person}.pdf"
      ├─ Safe filename components (normalize spaces, remove special chars)
      └─ Build proposed destination from category_routing template
```

### Phase 4: Duplicate Detection (4-Tier)

```
check_duplicate_index(sha256, dest_path, ocr_hash, meta_fields)
  │
  ├─ Tier 1: EXACT — same file bytes (SHA256 match)
  │   └─ "This exact file already exists in QSync"
  │
  ├─ Tier 2: NAME CONFLICT — same path at destination
  │   └─ "A file with this name already exists at the proposed location"
  │
  ├─ Tier 3: FUZZY OCR — same OCR text hash (different filename, same content)
  │   └─ "Different scan/resolution but same document text"
  │   └─ Uses sidecar_ocr_hash from .ocr.txt files
  │
  └─ Tier 4: FUZZY META — ≥95% field overlap in .meta.json
      └─ Compares: provider, date, patient, doc_type, description
      └─ "Same provider + patient + date + doc type — likely a duplicate"
```

### Phase 5: Auto-Approve Check

```
auto_approve_check(result, rules)
  │
  ├─ Is classification_confidence ≥ 0.90? → No → SKIP
  ├─ Is doc_type in never_auto_approve (identity_card, business)? → Yes → SKIP
  ├─ Is doc_type in safe_types? → No → SKIP
  ├─ Is person detected? → No → SKIP
  ├─ Are there duplicates? → Yes → SKIP
  ├─ Is side confirmation needed? → Yes → SKIP
  └─ All checks pass → autoApproved = true
```

### Phase 6: Lifecycle Tracking

```
save_lifecycle() — records to scan_lifecycle table:
  ├─ sha256, original_filename, original_path, file_size
  ├─ first_seen_at, ocr_text_hash, text_source, text_quality
  ├─ proposed_name, proposed_dest, proposed_doc_type, proposed_person, proposed_provider
  ├─ classification_confidence, rule_match_id
  └─ (final_* fields filled on approval)

add_proposal_attempt() — records to scan_proposals table:
  ├─ sha256, attempt_number, proposed_at
  ├─ proposed_name, proposed_dest, proposed_doc_type, proposed_person, proposed_provider
  └─ confidence, classification_confidence, response (pending/approved/denied)
```

### Phase 7: Notification & Approval

```
Pipeline outputs proposal → OpenClaw main session
  │
  ├─ For each file, shows:
  │   ├─ Original filename → Proposed Name → Destination
  │   ├─ Person | Type | Confidence (⚠️ if classification_confidence < 0.80)
  │   ├─ Duplicate warnings (if any)
  │   └─ Asks: Approve / Deny / Override
  │
  ├─ On APPROVE:
  │   ├─ cmd_approve() → approve_proposal()
  │   │   ├─ Move file: /home/ddieppa/scanner/processing/FILE → /mnt/e/QSync/DEST/NAME
  │   │   ├─ Generate sidecar files (.meta.json, .ocr.txt)
  │   │   ├─ Log move in file_moves table
  │   │   ├─ Update scan_lifecycle (final_name, final_dest, override_type)
  │   │   └─ If override: save_correction() to learn from the override
  │   └─ Cleanup empty parent directories
  │
  ├─ On DENY:
  │   ├─ Ask for reason → store in scan_proposals
  │   └─ Update scan_lifecycle (override_type='deny', rejection_reason)
  │
  └─ On OVERRIDE (--dest, --name flags):
      ├─ Move to overridden destination/name
      ├─ Save correction to classification_corrections table
      └─ Update scan_lifecycle with override_type
```

### Phase 8: Safety Net (Daily Cron)

```
Cron job (2pm ET daily) checks all inboxes:
  ├─ /home/ddieppa/scanner/inbox/     (primary)
  ├─ /home/ddieppa/scanner/processing/ (staged files)
  └─ /mnt/e/Qsync-Scanned-Documents/!!!Check/ (legacy inbox)
```

---

## Database Schema (scan_history.db)

| Table | Rows | Purpose |
|-------|------|---------|
| `ocr_cache` | 101 | SHA256 → extracted text (avoids re-OCR) |
| `scan_results` | 2,532 | All scan results with proposed names/destinations |
| `scan_sessions` | 85 | Batch sessions (date, file counts) |
| `file_index` | 10,750 | QSync file index with SHA256 + sidecar data |
| `classification_corrections` | 0 | Learned corrections from approval overrides |
| `scan_lifecycle` | 0 | Full audit trail per file (proposed → final) |
| `scan_proposals` | 0 | Individual proposal attempts per file |
| `file_moves` | 20 | Record of approved file moves |
| `duplicate_checks` | 0 | Duplicate detection history |

### Key `file_index` columns (with sidecar data):

| Column | Type | Purpose |
|--------|------|---------|
| `path` | TEXT PK | Full path to file in QSync |
| `sha256` | TEXT | Content hash for exact duplicate detection |
| `size` | INTEGER | File size in bytes |
| `mtime` | REAL | Last modification timestamp |
| `sidecar_meta` | TEXT | Full JSON from .meta.json sidecar |
| `sidecar_ocr_hash` | TEXT | SHA256[:16] of .ocr.txt content |
| `sidecar_has_meta` | BOOLEAN | Whether .meta.json exists |
| `sidecar_has_ocr` | BOOLEAN | Whether .ocr.txt exists |

### Key `scan_lifecycle` columns:

| Column | Purpose |
|--------|---------|
| `sha256` | File content hash (primary key) |
| `original_filename`, `original_path` | What was scanned |
| `ocr_text_hash`, `text_source`, `text_quality` | OCR quality tracking |
| `proposed_*` fields | What the classifier suggested |
| `final_*` fields | What was actually approved (may differ) |
| `override_type` | none/rename/retype/redirect/deny |
| `approval_attempts` | How many times Daniel was asked |
| `correction_applied` | Whether a classification correction was saved |

---

## CLI Commands

```bash
cd /home/ddieppa/.openclaw/workspace/scan-pipeline-v3

# Scan inbox and show proposals (no moves)
SCAN_INBOX=/home/ddieppa/scanner/inbox python3 scan_workflow.py scan

# Scan processing folder
SCAN_INBOX=/home/ddieppa/scanner/processing python3 scan_workflow.py scan

# Approve specific file(s)
python3 scan_workflow.py approve --sha SHA1 SHA2

# Approve all proposals
python3 scan_workflow.py approve --all

# Approve with override
python3 scan_workflow.py approve --sha SHA --dest "02-Areas/Family/Isabella/Health/" --name "2026-03-07_QuestDiagnostics_LabRequisition_Isabella.pdf"

# Full interactive workflow
python3 scan_workflow.py run

# View OCR cache
python3 scan_workflow.py ocr-cache show --sha SHA
python3 scan_workflow.py ocr-cache search --query "Baptist Health"

# Build/update duplicate index
python3 scan_workflow.py index           # incremental update
SCAN_BUILD_INDEX=1 python3 scan_workflow.py index --force  # full rebuild

# View lifecycle
python3 scan_workflow.py lifecycle              # recent scans
python3 scan_workflow.py lifecycle --sha SHA     # full history for one file
python3 scan_workflow.py lifecycle --stats       # aggregate accuracy statistics

# View/corrections
python3 scan_workflow.py corrections
python3 scan_workflow.py correct --sha SHA --type lab_requisition --person Isabella

# Watch daemon mode (systemd service)
python3 scan_workflow.py watch
```

---

## Naming Conventions

### General/Medical
```
YYYY-MM-DD_Provider_Description_Person.extension
Example: 2026-03-07_KendallPediatricPartners_QuestDiagnostics_LabRequisition_Isabella.pdf
```

### Identity Documents
```
YYYY-MM-DD_DocumentTypeSide_Person_EXP-YYYY-MM-DD.extension
Example: 2025-06-05_PassportFront_Daniel_EXP-2035-06-04.jpg
```

### Prescriptions
```
YYYY-MM-DD_Provider_Rx_Medication_Person.extension
Example: 2026-04-07_Walgreens_Rx_Prednisone_20mg_Daniel.pdf
```

---

## Configuration

| Setting | Value | Source |
|---------|-------|--------|
| Inbox | `/home/ddieppa/scanner/inbox` | `settings.py` / `SCAN_INBOX` env |
| Processing | `/home/ddieppa/scanner/processing` | Watcher moves files here |
| QSync root | `/mnt/e/QSync` | `settings.py` / `SCAN_QSYNC_ROOT` env |
| State DB | `state-data/scan_history.db` | `settings.py` / `SCAN_STATE_DIR` env |
| Auto-approve threshold | 0.90 | `config/scan_rules.yaml` |
| Watcher debounce | 10s | `watch-inbox.sh` |
| Watcher cooldown | 30s | `watch-inbox.sh` |
| Cron job ID | `bde66a5f-...` | OpenClaw cron |
| Cron schedule | Daily 2pm ET | OpenClaw cron |
| Telegram chat | 8277191343 | Cron delivery config |