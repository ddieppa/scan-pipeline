import sqlite3
import json
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).parent.parent.parent / "state-data" / "scan_history.db"

def init_db():
    """Initialize the scan tracking database."""
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS scan_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            total_files INTEGER,
            ocr_files INTEGER,
            pdf_files INTEGER,
            filename_only_files INTEGER,
            notes TEXT
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS ocr_cache (
            sha256 TEXT PRIMARY KEY,
            file_path TEXT NOT NULL,
            filename TEXT NOT NULL,
            ocr_text TEXT NOT NULL,
            text_source TEXT DEFAULT 'ocr_image',
            ocr_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ocr_duration_ms INTEGER,
            file_size INTEGER
        )
    ''')
    
    c.execute('''
        CREATE INDEX IF NOT EXISTS idx_ocr_cache_filename ON ocr_cache(filename)
    ''')
    
    c.execute('''
        CREATE INDEX IF NOT EXISTS idx_ocr_cache_path ON ocr_cache(file_path)
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS scan_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            row_number INTEGER,
            original_folder TEXT,
            original_name TEXT,
            proposed_name TEXT,
            proposed_dest TEXT,
            confidence REAL,
            extraction_method TEXT,
            ocr_text_preview TEXT,
            file_path TEXT,
            status TEXT DEFAULT 'pending',  -- pending, approved, declined, modified
            action_date TIMESTAMP,
            user_notes TEXT,
            FOREIGN KEY (session_id) REFERENCES scan_sessions(id)
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS file_moves (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            result_id INTEGER,
            source_path TEXT,
            destination_path TEXT,
            move_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            success BOOLEAN,
            error_message TEXT,
            FOREIGN KEY (result_id) REFERENCES scan_results(id)
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS duplicate_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            result_id INTEGER,
            dest_path TEXT,
            existing_file_size INTEGER,
            new_file_size INTEGER,
            existing_file_hash TEXT,
            new_file_hash TEXT,
            user_decision TEXT,  -- keep_both, overwrite, skip
            decision_date TIMESTAMP,
            FOREIGN KEY (result_id) REFERENCES scan_results(id)
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS file_index (
            path TEXT PRIMARY KEY,
            sha256 TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime REAL NOT NULL,
            indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            -- Sidecar data (from .meta.json and .ocr.txt companions)
            sidecar_meta TEXT,  -- JSON: provider, date, patient, doc_type, etc.
            sidecar_ocr_hash TEXT,  -- SHA256 hash of .ocr.txt content (for fuzzy dup detection)
            sidecar_has_meta BOOLEAN DEFAULT 0,
            sidecar_has_ocr BOOLEAN DEFAULT 0
        )
    ''')

    c.execute('''
        CREATE INDEX IF NOT EXISTS idx_file_index_sha ON file_index(sha256)
    ''')

    # Migrate: add sidecar columns if missing (existing DBs)
    for col, ctype in [
        ("sidecar_meta", "TEXT"),
        ("sidecar_ocr_hash", "TEXT"),
        ("sidecar_has_meta", "BOOLEAN DEFAULT 0"),
        ("sidecar_has_ocr", "BOOLEAN DEFAULT 0"),
    ]:
        try:
            c.execute(f"ALTER TABLE file_index ADD COLUMN {col} {ctype}")
        except sqlite3.OperationalError:
            pass  # Column already exists

    c.execute('''
        CREATE INDEX IF NOT EXISTS idx_file_index_meta ON file_index(sidecar_has_meta)
    ''')

    # ── Lifecycle tracking: full audit trail per file ──
    c.execute('''
        CREATE TABLE IF NOT EXISTS scan_lifecycle (
            sha256 TEXT PRIMARY KEY,
            original_filename TEXT NOT NULL,
            original_path TEXT,
            file_size INTEGER,
            first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ocr_text_hash TEXT,
            text_source TEXT,
            text_quality REAL,
            -- Proposed (classifier output)
            proposed_name TEXT,
            proposed_dest TEXT,
            proposed_doc_type TEXT,
            proposed_person TEXT,
            proposed_provider TEXT,
            classification_confidence REAL,
            rule_match_id TEXT,
            -- Final (after approval/override)
            final_name TEXT,
            final_dest TEXT,
            final_doc_type TEXT,
            final_person TEXT,
            final_provider TEXT,
            approved_at TIMESTAMP,
            -- Approval tracking
            approval_attempts INTEGER DEFAULT 0,
            override_type TEXT DEFAULT 'none',  -- none/rename/retype/redirect/deny
            rejection_reason TEXT,
            correction_applied BOOLEAN DEFAULT 0,
            notes TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS scan_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sha256 TEXT NOT NULL,
            attempt_number INTEGER NOT NULL,
            proposed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            proposed_name TEXT,
            proposed_dest TEXT,
            proposed_doc_type TEXT,
            proposed_person TEXT,
            proposed_provider TEXT,
            confidence REAL,
            classification_confidence REAL,
            response TEXT,  -- approved/denied/modified/pending
            user_feedback TEXT,
            modification_notes TEXT,
            FOREIGN KEY (sha256) REFERENCES scan_lifecycle(sha256)
        )
    ''')

    c.execute('''
        CREATE INDEX IF NOT EXISTS idx_lifecycle_status ON scan_lifecycle(override_type)
    ''')
    c.execute('''
        CREATE INDEX IF NOT EXISTS idx_lifecycle_doc_type ON scan_lifecycle(proposed_doc_type)
    ''')
    c.execute('''
        CREATE INDEX IF NOT EXISTS idx_proposals_sha ON scan_proposals(sha256)
    ''')

    conn.commit()
    conn.close()
    return DB_PATH


def get_ocr_cache(sha256: str) -> dict | None:
    """Look up OCR cache entry by SHA256. Returns dict with keys: sha256, file_path, filename, ocr_text, text_source, ocr_timestamp, ocr_duration_ms, file_size. Returns None if not cached."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM ocr_cache WHERE sha256 = ?', (sha256,))
    row = c.fetchone()
    conn.close()
    if row:
        return dict(row)
    return None


def save_ocr_cache(sha256: str, file_path: str, filename: str, ocr_text: str,
                   text_source: str = "ocr_image", ocr_duration_ms: int | None = None,
                   file_size: int | None = None) -> None:
    """Save or update an OCR cache entry."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO ocr_cache (sha256, file_path, filename, ocr_text, text_source, ocr_timestamp, ocr_duration_ms, file_size)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?)
    ''', (sha256, file_path, filename, ocr_text, text_source, ocr_duration_ms, file_size))
    conn.commit()
    conn.close()


def get_ocr_cache_by_path(file_path: str) -> dict | None:
    """Look up OCR cache entry by file path. Returns dict or None."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM ocr_cache WHERE file_path = ?', (file_path,))
    row = c.fetchone()
    conn.close()
    if row:
        return dict(row)
    return None


def list_ocr_cache(limit: int = 50, offset: int = 0) -> list[dict]:
    """List OCR cache entries, most recent first."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT sha256, file_path, filename, text_source, ocr_timestamp, ocr_duration_ms, file_size, length(ocr_text) as ocr_text_len FROM ocr_cache ORDER BY ocr_timestamp DESC LIMIT ? OFFSET ?', (limit, offset))
    rows = [dict(row) for row in c.fetchall()]
    conn.close()
    return rows


def delete_ocr_cache(sha256: str | None = None, all_entries: bool = False) -> int:
    """Delete OCR cache entries. Delete one by sha256, or all if all_entries=True. Returns count deleted."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if all_entries:
        c.execute('SELECT COUNT(*) FROM ocr_cache')
        count = c.fetchone()[0]
        c.execute('DELETE FROM ocr_cache')
    elif sha256:
        c.execute('DELETE FROM ocr_cache WHERE sha256 = ?', (sha256,))
        count = c.rowcount
    else:
        conn.close()
        return 0
    conn.commit()
    conn.close()
    return count

def save_scan_session(results, notes=""):
    """Save a scan session and its results to the database."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Count methods
    ocr_count = sum(1 for r in results if r.get("extraction_method") == "OCR")
    pdf_count = sum(1 for r in results if r.get("extraction_method") == "PDF")
    filename_count = sum(1 for r in results if r.get("extraction_method") == "filename")
    
    # Insert session
    c.execute('''
        INSERT INTO scan_sessions (total_files, ocr_files, pdf_files, filename_only_files, notes)
        VALUES (?, ?, ?, ?, ?)
    ''', (len(results), ocr_count, pdf_count, filename_count, notes))
    
    session_id = c.lastrowid
    
    # Insert results
    for r in results:
        c.execute('''
            INSERT INTO scan_results 
            (session_id, row_number, original_folder, original_name, proposed_name, 
             proposed_dest, confidence, extraction_method, ocr_text_preview, file_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            session_id,
            r.get("id"),
            r.get("original_folder", ""),
            r.get("original_name", ""),
            r.get("proposed_name", ""),
            r.get("proposed_dest", ""),
            r.get("confidence", 0),
            r.get("extraction_method", "filename"),
            r.get("ocr_text_preview", "")[:500],
            r.get("file_path", "")
        ))
    
    conn.commit()
    conn.close()
    return session_id

def update_result_status(row_number, status, user_notes=""):
    """Update the status of a scan result by row_number."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        UPDATE scan_results
        SET status = ?, action_date = CURRENT_TIMESTAMP, user_notes = ?
        WHERE row_number = ? AND status = 'pending'
        ORDER BY id DESC LIMIT 1
    ''', (status, user_notes, row_number))
    conn.commit()
    conn.close()


def update_result_status_by_path(file_path, status, user_notes=""):
    """Update the status of a scan result by file_path."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        UPDATE scan_results
        SET status = ?, action_date = CURRENT_TIMESTAMP, user_notes = ?
        WHERE file_path = ? AND status = 'pending'
        ORDER BY id DESC LIMIT 1
    ''', (status, user_notes, file_path))
    conn.commit()
    conn.close()

def check_duplicate(dest_path, file_path):
    """Check if a file already exists at the destination."""
    import hashlib
    
    dest = Path(dest_path)
    if not dest.exists():
        return None  # No duplicate
    
    # Calculate hashes
    def file_hash(path):
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            while chunk := f.read(8192):
                h.update(chunk)
        return h.hexdigest()[:16]
    
    existing_size = dest.stat().st_size
    new_size = Path(file_path).stat().st_size
    existing_hash = file_hash(dest)
    new_hash = file_hash(file_path)
    
    return {
        "exists": True,
        "dest_path": str(dest),
        "existing_size": existing_size,
        "new_size": new_size,
        "existing_hash": existing_hash,
        "new_hash": new_hash,
        "same_content": existing_hash == new_hash,
    }

def log_file_move(source_path, destination, success=True, error=""):
    """Log a file move operation by looking up the result by source_path."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Find the result_id from scan_results by file_path
    c.execute('''
        SELECT id FROM scan_results WHERE file_path = ? ORDER BY id DESC LIMIT 1
    ''', (source_path,))
    row = c.fetchone()
    result_id = row[0] if row else None
    
    c.execute('''
        INSERT INTO file_moves (result_id, source_path, destination_path, success, error_message)
        VALUES (?, ?, ?, ?, ?)
    ''', (result_id, source_path, destination, success, error))
    
    conn.commit()
    conn.close()

def get_pending_results():
    """Get all pending scan results."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    c.execute('''
        SELECT * FROM scan_results 
        WHERE status = 'pending'
        ORDER BY session_id DESC, row_number
    ''')
    
    results = [dict(row) for row in c.fetchall()]
    conn.close()
    return results

def get_scan_history(limit=10):
    """Get recent scan sessions."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    c.execute('''
        SELECT * FROM scan_sessions
        ORDER BY session_date DESC
        LIMIT ?
    ''', (limit,))
    
    sessions = [dict(row) for row in c.fetchall()]
    conn.close()
    return sessions

def get_stats():
    """Get scan statistics."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    stats = {}
    
    # Total files scanned
    c.execute("SELECT COUNT(*) FROM scan_results")
    stats["total_scanned"] = c.fetchone()[0]
    
    # Status breakdown
    c.execute("SELECT status, COUNT(*) FROM scan_results GROUP BY status")
    stats["status_breakdown"] = dict(c.fetchall())
    
    # Total moved
    c.execute("SELECT COUNT(*) FROM file_moves WHERE success = 1")
    stats["total_moved"] = c.fetchone()[0]
    
    # Total duplicates found
    c.execute("SELECT COUNT(*) FROM duplicate_checks")
    stats["total_duplicates"] = c.fetchone()[0]
    
    conn.close()
    return stats

# ── Lifecycle tracking functions ──────────────────────────────────────────

def save_lifecycle(sha256: str, original_filename: str, original_path: str = "",
                   file_size: int = 0, ocr_text_hash: str = "", text_source: str = "",
                   text_quality: float = 0.0, proposed_name: str = "", proposed_dest: str = "",
                   proposed_doc_type: str = "", proposed_person: str = "", proposed_provider: str = "",
                   classification_confidence: float = 0.0, rule_match_id: str = "") -> dict:
    """Create or update a scan_lifecycle record when a file is first classified."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Upsert — if same sha256 scanned again, update the proposal
    c.execute("""INSERT OR REPLACE INTO scan_lifecycle
        (sha256, original_filename, original_path, file_size, first_seen_at,
         ocr_text_hash, text_source, text_quality,
         proposed_name, proposed_dest, proposed_doc_type, proposed_person, proposed_provider,
         classification_confidence, rule_match_id,
         approval_attempts, override_type)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP,
                ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?,
                0, 'none')""",
        (sha256, original_filename, original_path, file_size,
         ocr_text_hash, text_source, text_quality,
         proposed_name, proposed_dest, proposed_doc_type, proposed_person, proposed_provider,
         classification_confidence, rule_match_id))

    # Also create the first proposal entry
    c.execute("""INSERT INTO scan_proposals
        (sha256, attempt_number, proposed_name, proposed_dest, proposed_doc_type,
         proposed_person, proposed_provider, confidence, classification_confidence, response)
        VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
        (sha256, proposed_name, proposed_dest, proposed_doc_type,
         proposed_person, proposed_provider, classification_confidence, classification_confidence))

    conn.commit()
    # Return the lifecycle record
    c.execute("SELECT * FROM scan_lifecycle WHERE sha256 = ?", (sha256,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else {}


def update_lifecycle_approval(sha256: str, final_name: str, final_dest: str,
                              final_doc_type: str = "", final_person: str = "",
                              final_provider: str = "", override_type: str = "none",
                              rejection_reason: str = "", correction_applied: bool = False,
                              user_feedback: str = "") -> dict | None:
    """Update lifecycle when a file is approved/denied with final values."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Increment approval_attempts
    c.execute("""SELECT approval_attempts, override_type FROM scan_lifecycle WHERE sha256 = ?""", (sha256,))
    row = c.fetchone()
    if not row:
        conn.close()
        return None

    current_attempts = row['approval_attempts'] + 1
    # Determine override type if not explicitly provided
    if override_type == "none":
        c.execute("SELECT proposed_name, proposed_dest, proposed_doc_type, proposed_person, proposed_provider FROM scan_lifecycle WHERE sha256 = ?", (sha256,))
        orig = c.fetchone()
        if orig:
            if orig['proposed_name'] != final_name and orig['proposed_doc_type'] != final_doc_type:
                override_type = "retype"
            elif orig['proposed_name'] != final_name:
                override_type = "rename"
            elif orig['proposed_dest'] != final_dest:
                override_type = "redirect"
            else:
                override_type = "none"  # approved as-is

    c.execute("""UPDATE scan_lifecycle SET
        final_name = ?, final_dest = ?, final_doc_type = ?, final_person = ?,
        final_provider = ?, approved_at = CURRENT_TIMESTAMP,
        approval_attempts = ?, override_type = ?, rejection_reason = ?,
        correction_applied = ?
        WHERE sha256 = ?""",
        (final_name, final_dest, final_doc_type, final_person, final_provider,
         current_attempts, override_type, rejection_reason,
         1 if correction_applied else 0, sha256))

    # Update the latest proposal with the response
    c.execute("""UPDATE scan_proposals SET response = ?, user_feedback = ?, modification_notes = ?
        WHERE sha256 = ? AND attempt_number = (SELECT MAX(attempt_number) FROM scan_proposals WHERE sha256 = ?)""",
        ('approved' if override_type != 'deny' else 'denied', user_feedback,
         f"override: {override_type}", sha256, sha256))

    conn.commit()
    c.execute("SELECT * FROM scan_lifecycle WHERE sha256 = ?", (sha256,))
    result = c.fetchone()
    conn.close()
    return dict(result) if result else None


def add_proposal_attempt(sha256: str, proposed_name: str, proposed_dest: str,
                         proposed_doc_type: str, proposed_person: str,
                         proposed_provider: str, confidence: float = 0,
                         classification_confidence: float = 0, response: str = "pending",
                         user_feedback: str = "", modification_notes: str = "") -> int:
    """Add a new proposal attempt for a file (when re-proposed after denial/modification)."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Get next attempt number
    c.execute("SELECT MAX(attempt_number) FROM scan_proposals WHERE sha256 = ?", (sha256,))
    row = c.fetchone()
    next_attempt = (row[0] or 0) + 1

    # Update the previous attempt's response if still pending
    c.execute("""UPDATE scan_proposals SET response = ?, user_feedback = ?
        WHERE sha256 = ? AND attempt_number = ? AND response = 'pending'""",
        ('denied', user_feedback, sha256, next_attempt - 1))

    # Increment approval_attempts on lifecycle
    c.execute("UPDATE scan_lifecycle SET approval_attempts = approval_attempts + 1 WHERE sha256 = ?", (sha256,))

    c.execute("""INSERT INTO scan_proposals
        (sha256, attempt_number, proposed_name, proposed_dest, proposed_doc_type,
         proposed_person, proposed_provider, confidence, classification_confidence, response)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
        (sha256, next_attempt, proposed_name, proposed_dest, proposed_doc_type,
         proposed_person, proposed_provider, confidence, classification_confidence))

    conn.commit()
    proposal_id = c.lastrowid
    conn.close()
    return proposal_id


def get_lifecycle(sha256: str) -> dict | None:
    """Get a lifecycle record by SHA256."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM scan_lifecycle WHERE sha256 = ?", (sha256,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_proposals(sha256: str) -> list[dict]:
    """Get all proposal attempts for a file."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM scan_proposals WHERE sha256 = ? ORDER BY attempt_number", (sha256,))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_lifecycle_stats() -> dict:
    """Get aggregate lifecycle statistics."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    stats = {}

    c.execute("SELECT COUNT(*) FROM scan_lifecycle")
    stats['total_files'] = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM scan_lifecycle WHERE override_type = 'none'")
    stats['approved_as_proposed'] = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM scan_lifecycle WHERE override_type != 'none'")
    stats['overridden'] = c.fetchone()[0]

    c.execute("""SELECT override_type, COUNT(*) FROM scan_lifecycle
        WHERE override_type != 'none' GROUP BY override_type""")
    stats['override_breakdown'] = dict(c.fetchall())

    c.execute("""SELECT AVG(approval_attempts) FROM scan_lifecycle
        WHERE approved_at IS NOT NULL""")
    avg = c.fetchone()[0]
    stats['avg_approval_attempts'] = round(avg, 2) if avg else 0

    c.execute("""SELECT proposed_doc_type, COUNT(*),
        SUM(CASE WHEN override_type != 'none' THEN 1 ELSE 0 END) as overrides
        FROM scan_lifecycle GROUP BY proposed_doc_type ORDER BY overrides DESC LIMIT 10""")
    stats['doc_type_accuracy'] = [{"doc_type": r[0], "total": r[1], "overridden": r[2], "accuracy": round(1 - r[2]/r[1], 2) if r[1] > 0 else 0} for r in c.fetchall()]

    c.execute("""SELECT proposed_doc_type, final_doc_type, COUNT(*) as cnt
        FROM scan_lifecycle
        WHERE override_type IN ('retype', 'rename')
        GROUP BY proposed_doc_type, final_doc_type
        ORDER BY cnt DESC LIMIT 10""")
    stats['common_misclassifications'] = [{"from": r[0], "to": r[1], "count": r[2]} for r in c.fetchall()]

    conn.close()
    return stats


def get_recent_lifecycle(limit: int = 20, offset: int = 0) -> list[dict]:
    """Get recent lifecycle records, most recent first."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""SELECT * FROM scan_lifecycle
        ORDER BY first_seen_at DESC LIMIT ? OFFSET ?""", (limit, offset))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_lifecycle_history(sha256: str) -> dict | None:
    """Get full lifecycle + all proposals for a file."""
    lifecycle = get_lifecycle(sha256)
    if not lifecycle:
        return None
    proposals = get_proposals(sha256)
    return {"lifecycle": lifecycle, "proposals": proposals}


def _read_sidecar(filepath: Path) -> tuple[str | None, str | None, bool, bool]:
    """Read sidecar .meta.json and .ocr.txt files for a given document.
    
    Returns (meta_json_str, ocr_hash, has_meta, has_ocr).
    meta_json_str is the full JSON string of the .meta.json file.
    ocr_hash is SHA256[:16] of the .ocr.txt content for fuzzy duplicate detection.
    """
    import hashlib as _hashlib
    import json as _json
    meta_json = None
    ocr_hash = None
    has_meta = False
    has_ocr = False
    
    stem = filepath.stem
    parent = filepath.parent
    
    meta_path = parent / f"{stem}.meta.json"
    if meta_path.exists():
        try:
            meta_json = meta_path.read_text(encoding='utf-8')
            has_meta = True
        except (OSError, UnicodeDecodeError):
            pass
    
    ocr_path = parent / f"{stem}.ocr.txt"
    if ocr_path.exists():
        try:
            ocr_content = ocr_path.read_text(encoding='utf-8')
            ocr_hash = _hashlib.sha256(ocr_content.encode()).hexdigest()[:16]
            has_ocr = True
        except (OSError, UnicodeDecodeError):
            pass
    
    return meta_json, ocr_hash, has_meta, has_ocr


def build_file_index(root_path: str, extensions: set[str] | None = None) -> dict:
    """Walk QSync root and build a SHA256 index of all supported files.
    
    Includes sidecar data (.meta.json, .ocr.txt) for enhanced duplicate detection.
    Returns stats dict with counts. Index is stored in the file_index table.
    Only updates changed files (incremental via mtime comparison).
    Sidecar data is always re-read since it may change independently.
    """
    import hashlib
    import json as _json
    root = Path(root_path)
    if not root.exists():
        return {"indexed": 0, "skipped": 0, "errors": 0}
    
    if extensions is None:
        extensions = {'.pdf', '.jpg', '.jpeg', '.png', '.tif', '.tiff', '.docx', '.xlsx', '.bmp', '.gif', '.webp'}
    
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Get current indexed paths for incremental update
    c.execute("SELECT path, mtime FROM file_index")
    indexed = {row[0]: row[1] for row in c.fetchall()}
    
    stats = {"indexed": 0, "skipped": 0, "errors": 0, "sidecars_found": 0}
    
    for filepath in root.rglob("*"):
        try:
            if not filepath.is_file():
                continue
            if filepath.suffix.lower() not in extensions:
                continue
            
            str_path = str(filepath)
            mtime = filepath.stat().st_mtime
            
            # Skip if unchanged since last index (but always re-read sidecars)
            if str_path in indexed and abs(indexed[str_path] - mtime) < 1.0:
                # File unchanged, but update sidecar data
                meta_json, ocr_hash, has_meta, has_ocr = _read_sidecar(filepath)
                if has_meta or has_ocr:
                    c.execute("UPDATE file_index SET sidecar_meta = ?, sidecar_ocr_hash = ?, sidecar_has_meta = ?, sidecar_has_ocr = ? WHERE path = ?",
                              (meta_json, ocr_hash, has_meta, has_ocr, str_path))
                    stats["sidecars_found"] += 1
                stats["skipped"] += 1
                continue
            
            sha = hashlib.sha256(filepath.read_bytes()).hexdigest()
            size = filepath.stat().st_size
            
            # Read sidecar data
            meta_json, ocr_hash, has_meta, has_ocr = _read_sidecar(filepath)
            if has_meta or has_ocr:
                stats["sidecars_found"] += 1
            
            c.execute("INSERT OR REPLACE INTO file_index (path, sha256, size, mtime, indexed_at, sidecar_meta, sidecar_ocr_hash, sidecar_has_meta, sidecar_has_ocr) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?)",
                      (str_path, sha, size, mtime, meta_json, ocr_hash, has_meta, has_ocr))
            stats["indexed"] += 1
        except (OSError, PermissionError):
            stats["errors"] += 1
    
    conn.commit()
    conn.close()
    return stats


def check_duplicate_index(sha256: str, dest_path: str | None = None,
                             ocr_hash: str | None = None, meta_fields: dict | None = None,
                             fuzzy_threshold: float = 0.95) -> list[dict]:
    """Check if a file with the same SHA256 exists in the file index.
    
    Also performs fuzzy duplicate detection using sidecar data:
    - If ocr_hash is provided, checks for matching .ocr.txt hashes
    - If meta_fields is provided, checks for >95% field overlap with .meta.json entries
    
    Args:
        sha256: File content hash
        dest_path: Destination path to check for filename conflicts
        ocr_hash: SHA256[:16] of OCR text for fuzzy matching
        meta_fields: Dict of meta fields (provider, date, patient, etc.) for fuzzy matching
        fuzzy_threshold: Minimum field overlap ratio for fuzzy match (default 0.95)
    
    Returns list of matching entries (empty if no duplicates).
    Each entry has an added 'match_type' field: 'exact', 'name_conflict', or 'fuzzy'
    """
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    matches = []
    seen_paths = set()
    
    # 1. Exact content match by SHA256
    c.execute("SELECT * FROM file_index WHERE sha256 = ?", (sha256,))
    for row in c.fetchall():
        entry = dict(row)
        entry['match_type'] = 'exact'
        matches.append(entry)
        seen_paths.add(entry['path'])
    
    # 2. Name conflict at destination
    if dest_path:
        dest = Path(dest_path)
        dest_str = str(dest) if not isinstance(dest_path, str) else dest_path
        c.execute("SELECT * FROM file_index WHERE path = ?", (dest_str,))
        for row in c.fetchall():
            if row['path'] not in seen_paths:
                entry = dict(row)
                entry['match_type'] = 'name_conflict'
                matches.append(entry)
                seen_paths.add(entry['path'])
    
    # 3. Fuzzy match via OCR hash (exact OCR content match)
    if ocr_hash:
        c.execute("SELECT * FROM file_index WHERE sidecar_ocr_hash = ?", (ocr_hash,))
        for row in c.fetchall():
            if row['path'] not in seen_paths:
                entry = dict(row)
                entry['match_type'] = 'fuzzy_ocr'
                matches.append(entry)
                seen_paths.add(entry['path'])
    
    # 4. Fuzzy match via metadata fields (>95% overlap)
    if meta_fields:
        import json as _json
        # Get all files with sidecar metadata
        c.execute("SELECT * FROM file_index WHERE sidecar_has_meta = 1")
        for row in c.fetchall():
            if row['path'] in seen_paths:
                continue
            if not row['sidecar_meta']:
                continue
            try:
                existing_meta = _json.loads(row['sidecar_meta'])
            except (_json.JSONDecodeError, TypeError):
                continue
            
            # Compare fields
            fields_to_check = ['provider', 'date', 'patient', 'doc_type', 'description']
            matching = 0
            total = 0
            for field in fields_to_check:
                new_val = meta_fields.get(field, '').strip().lower()
                existing_val = str(existing_meta.get(field, '')).strip().lower()
                if new_val and existing_val:
                    total += 1
                    if new_val == existing_val:
                        matching += 1
                    elif new_val in existing_val or existing_val in new_val:
                        matching += 0.5  # Partial match
            
            if total > 0 and (matching / total) >= fuzzy_threshold:
                entry = dict(row)
                entry['match_type'] = 'fuzzy_meta'
                entry['match_score'] = matching / total
                matches.append(entry)
                seen_paths.add(entry['path'])
    
    conn.close()
    return matches


if __name__ == "__main__":
    init_db()
    print(f"✅ Database initialized at: {DB_PATH}")
    
    # Test: save current scan results
    results_file = Path(__file__).parent.parent / "state-data" / "last_scan_results.json"
    if results_file.exists():
        with open(results_file) as f:
            results = json.load(f)
        session_id = save_scan_session(results, "Initial scan with OCR/PDF extraction")
        print(f"💾 Saved {len(results)} results to session #{session_id}")
        
        stats = get_stats()
        print(f"📊 Stats: {stats}")
