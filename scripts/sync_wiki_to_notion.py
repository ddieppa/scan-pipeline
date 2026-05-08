#!/usr/bin/env python3
"""Sync OpenClaw wiki pages to Notion database — incremental.

Only creates/updates pages that have changed since the last sync.
Uses a local index file (.notion_sync_index.json) to track hashes.
Saves progress after each page so partial syncs resume cleanly.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

NOTION_KEY = os.environ.get("NOTION_API_KEY") or ""
NOTION_VERSION = "2022-06-28"

# Also check workspace .env file (persists across OpenClaw updates)
_env_file = Path("/home/ddieppa/.openclaw/workspace/.env")
if not NOTION_KEY and _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line.startswith("NOTION_API_KEY="):
            NOTION_KEY = _line.split("=", 1)[1].strip()
            break

WIKI_DB_ID = "5f458758-a401-41e1-bc56-d22f390457b5"
WIKI_ROOT = Path("/home/ddieppa/.openclaw/wiki/main")
INDEX_FILE = Path("/home/ddieppa/.openclaw/workspace/.notion_sync_index.json")

SKIP_FILES = {"index.md", "WIKI.md", "AGENTS.md", "inbox.md"}
SKIP_DIRS = {"_attachments", "_views", "sources", "syntheses"}


def content_hash(text: str) -> str:
    """SHA-256 hash of file content for change detection."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def notion_api(method, endpoint, data=None, retries=3):
    """Make a Notion API call with retry logic."""
    import urllib.request

    url = f"https://api.notion.com/v1/{endpoint}"
    headers = {
        "Authorization": f"Bearer {NOTION_KEY}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

    body = json.dumps(data).encode("utf-8") if data else None

    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, method=method, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8")
            error_json = json.loads(error_body) if error_body else {"error": str(e)}
            if e.code == 429 and attempt < retries - 1:
                retry_after = float(e.headers.get("Retry-After", 2))
                print(f"  ⏳ Rate limited, waiting {retry_after}s...")
                time.sleep(retry_after)
                continue
            return error_json
        except (TimeoutError, urllib.error.URLError) as e:
            if attempt < retries - 1:
                wait = 5 * (attempt + 1)
                print(f"  ⏳ Timeout/error, retrying in {wait}s... ({e})")
                time.sleep(wait)
                continue
            return {"error": str(e)}
    return {"error": "Max retries exceeded"}


def parse_wiki_frontmatter(content):
    """Extract frontmatter fields from a wiki markdown file."""
    meta = {"type": "unknown", "updated": None, "claims": 0, "status": "Active"}

    fm_match = re.match(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
    if fm_match:
        fm = fm_match.group(1)
        for line in fm.split('\n'):
            if line.startswith("name:"):
                meta["name"] = line.split(":", 1)[1].strip().strip('"').strip("'")
            elif line.startswith("type:"):
                meta["type"] = line.split(":", 1)[1].strip().strip('"').strip("'")
            elif line.startswith("updated:") or line.startswith("updatedAt:"):
                val = line.split(":", 1)[1].strip().strip('"').strip("'")
                if "T" in val:
                    val = val.split("T")[0]
                meta["updated"] = val
            elif line.startswith("created:"):
                val = line.split(":", 1)[1].strip().strip('"').strip("'")
                if not meta["updated"]:
                    meta["updated"] = val

    if fm_match:
        fm = fm_match.group(1)
        meta["claims"] = len(re.findall(r'^\s*-\s+claim:', fm, re.MULTILINE))

    type_map = {
        "entity": "Entity", "concept": "Concept", "report": "Report",
        "person": "Entity", "provider": "Entity", "organization": "Entity", "pet": "Entity",
    }
    meta["type"] = type_map.get(meta["type"], meta["type"].capitalize())
    return meta


def markdown_to_blocks(content, max_blocks=100):
    """Convert markdown content to Notion block format."""
    blocks = []

    content = re.sub(r'^---\s*\n.*?\n---\s*\n', '', content, count=1, flags=re.DOTALL)

    lines = content.split('\n')
    i = 0

    while i < len(lines) and len(blocks) < max_blocks:
        line = lines[i]

        if not line.strip():
            i += 1
            continue

        if line.startswith('### '):
            text = line[4:].strip()
            if text:
                blocks.append({"object": "block", "type": "heading_3",
                                "heading_3": {"rich_text": [{"text": {"content": text[:2000]}}]}})
            i += 1
        elif line.startswith('## '):
            text = line[3:].strip()
            if text:
                blocks.append({"object": "block", "type": "heading_2",
                                "heading_2": {"rich_text": [{"text": {"content": text[:2000]}}]}})
            i += 1
        elif line.startswith('# '):
            text = line[2:].strip()
            if text:
                blocks.append({"object": "block", "type": "heading_1",
                                "heading_1": {"rich_text": [{"text": {"content": text[:2000]}}]}})
            i += 1
        elif line.strip() == '---':
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            i += 1
        elif line.strip().startswith('- '):
            text = line.strip()[2:]
            if text:
                blocks.append({"object": "block", "type": "bulleted_list_item",
                                "bulleted_list_item": {"rich_text": [{"text": {"content": text[:2000]}}]}})
            i += 1
        elif line.strip().startswith('|') and '|' in line[1:]:
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                if not lines[i].strip().startswith('|-'):
                    table_lines.append(lines[i].strip())
                i += 1
            if table_lines:
                table_text = '\n'.join(table_lines)
                blocks.append({"object": "block", "type": "code",
                                "code": {"rich_text": [{"text": {"content": table_text[:2000]}}],
                                         "language": "plain text"}})
        else:
            para_lines = []
            while (i < len(lines) and lines[i].strip()
                   and not lines[i].startswith('#')
                   and not lines[i].strip().startswith('- ')
                   and not lines[i].strip().startswith('|')
                   and lines[i].strip() != '---'):
                para_lines.append(lines[i])
                i += 1
                if len(para_lines) >= 5:
                    break
            text = ' '.join(para_lines).strip()
            if text:
                blocks.append({"object": "block", "type": "paragraph",
                                "paragraph": {"rich_text": [{"text": {"content": text[:2000]}}]}})

    return blocks


def collect_wiki_pages():
    """Find all wiki pages in the vault."""
    pages = []

    for subdir in ["entities", "concepts", "reports"]:
        dir_path = WIKI_ROOT / subdir
        if not dir_path.exists():
            continue
        for f in sorted(dir_path.glob("*.md")):
            if f.name in SKIP_FILES:
                continue
            content = f.read_text(encoding="utf-8")
            meta = parse_wiki_frontmatter(content)
            meta["wiki_path"] = f"{subdir}/{f.stem}"
            meta["file_path"] = str(f)
            meta["content_hash"] = content_hash(content)
            meta["raw_content"] = content
            if "name" not in meta:
                meta["name"] = f.stem.replace("-", " ").title()
            pages.append(meta)

    return pages


def load_index():
    """Load the sync index from disk."""
    if INDEX_FILE.exists():
        try:
            return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_index(index):
    """Save the sync index to disk."""
    INDEX_FILE.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")


def get_existing_pages():
    """Get all existing pages in the Notion database (wiki_path → page_id)."""
    existing = {}
    has_more = True
    start_cursor = None

    while has_more:
        endpoint = f"databases/{WIKI_DB_ID}/query"
        data = {"page_size": 100}
        if start_cursor:
            data["start_cursor"] = start_cursor

        result = notion_api("POST", endpoint, data)

        for page in result.get("results", []):
            props = page.get("properties", {})
            wiki_path = ""
            for rt in props.get("Wiki Path", {}).get("rich_text", []):
                wiki_path += rt.get("plain_text", "")
            existing[wiki_path] = page["id"]

        has_more = result.get("has_more", False)
        start_cursor = result.get("next_cursor")

    return existing


def create_page(wiki_page, dry_run=False):
    """Create a new page in the Notion database."""
    name = wiki_page["name"]
    page_type = wiki_page.get("type", "Unknown")
    updated = wiki_page.get("updated")
    claims = wiki_page.get("claims", 0)
    wiki_path = wiki_page.get("wiki_path", "")

    blocks = markdown_to_blocks(wiki_page["raw_content"])

    page_data = {
        "parent": {"database_id": WIKI_DB_ID},
        "properties": {
            "Name": {"title": [{"text": {"content": name[:2000]}}]},
            "Claims": {"number": claims},
            "Wiki Path": {"rich_text": [{"text": {"content": wiki_path[:2000]}}]},
        },
    }

    if page_type in ("Entity", "Concept", "Report"):
        page_data["properties"]["Type"] = {"select": {"name": page_type}}
    if updated:
        page_data["properties"]["Updated"] = {"date": {"start": updated}}

    if blocks:
        page_data["children"] = blocks[:100]

    if dry_run:
        print(f"  [DRY RUN] Would create: {name} ({page_type}, {claims} claims)")
        return True, None

    result = notion_api("POST", "pages", page_data)

    if "id" in result:
        print(f"  ✅ Created: {name} ({page_type}, {claims} claims)")
        page_id = result["id"]

        remaining = blocks[100:]
        for chunk_start in range(0, len(remaining), 100):
            chunk = remaining[chunk_start:chunk_start + 100]
            time.sleep(0.4)
            notion_api("PATCH", f"blocks/{page_id}/children", {"children": chunk})

        return True, page_id
    else:
        error_msg = result.get("message", str(result))
        print(f"  ❌ Failed: {name} — {error_msg}")
        return False, None


def update_page_content(page_id, wiki_page):
    """Update an existing page's properties and content blocks."""
    name = wiki_page["name"]
    page_type = wiki_page.get("type", "Unknown")
    updated = wiki_page.get("updated")
    claims = wiki_page.get("claims", 0)

    # Update properties
    props = {
        "Name": {"title": [{"text": {"content": name[:2000]}}]},
        "Claims": {"number": claims},
    }
    if page_type in ("Entity", "Concept", "Report"):
        props["Type"] = {"select": {"name": page_type}}
    if updated:
        props["Updated"] = {"date": {"start": updated}}

    result = notion_api("PATCH", f"pages/{page_id}", {"properties": props})

    if "id" not in result:
        print(f"  ❌ Failed to update props: {name}")
        return False

    # Delete old content blocks
    existing = notion_api("GET", f"blocks/{page_id}/children?page_size=100")
    for block in existing.get("results", []):
        notion_api("DELETE", f"blocks/{block['id']}")
        time.sleep(0.3)

    # Append new content
    blocks = markdown_to_blocks(wiki_page["raw_content"])
    for chunk_start in range(0, len(blocks), 100):
        chunk = blocks[chunk_start:chunk_start + 100]
        time.sleep(0.4)
        notion_api("PATCH", f"blocks/{page_id}/children", {"children": chunk})

    print(f"  ✅ Updated: {name}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Incremental sync OpenClaw wiki to Notion")
    parser.add_argument("--dry-run", action="store_true", help="Preview without making changes")
    parser.add_argument("--force", action="store_true", help="Force update all pages (ignore index)")
    args = parser.parse_args()

    global NOTION_KEY
    if not NOTION_KEY:
        config_path = Path("/home/ddieppa/.openclaw/openclaw.json")
        if config_path.exists():
            with config_path.open() as f:
                cfg = json.load(f)
            NOTION_KEY = cfg.get("notion", {}).get("apiKey", "")

    if not NOTION_KEY:
        print("ERROR: NOTION_API_KEY not set and not found in workspace/.env or openclaw.json")
        sys.exit(1)

    print("📚 Syncing wiki pages to Notion (incremental)...\n")

    wiki_pages = collect_wiki_pages()
    print(f"Found {len(wiki_pages)} wiki pages in vault")

    index = {} if args.force else load_index()

    if not args.dry_run:
        existing = get_existing_pages()
        print(f"Found {len(existing)} existing pages in Notion database")
    else:
        existing = {}

    # Categorize pages
    to_create = []
    to_update = []
    to_skip = []

    for wp in wiki_pages:
        wiki_path = wp.get("wiki_path", "")
        current_hash = wp.get("content_hash", "")
        file_path = wp.get("file_path", "")

        # Get current file mtime for fast change detection
        try:
            current_mtime = str(Path(file_path).stat().st_mtime) if file_path else None
        except OSError:
            current_mtime = None

        last_sync = index.get(wiki_path, {})
        last_hash = last_sync.get("content_hash")
        last_mtime = last_sync.get("file_mtime")
        last_notion_id = last_sync.get("notion_id")

        # Determine if page needs updating
        if wiki_path not in existing:
            to_create.append(wp)
        elif not args.force and last_hash == current_hash:
            to_skip.append(wp)
        else:
            wp["_notion_id"] = existing[wiki_path]
            wp["_content_hash"] = current_hash
            wp["_file_mtime"] = current_mtime
            to_update.append(wp)

    print(f"  {len(to_create)} new, {len(to_update)} changed, {len(to_skip)} unchanged\n")

    if args.dry_run:
        for wp in to_create:
            print(f"  [DRY RUN] Would create: {wp['name']} ({wp.get('type','?')}, {wp.get('claims',0)} claims)")
        for wp in to_update:
            print(f"  [DRY RUN] Would update: {wp['name']}")
        for wp in to_skip:
            print(f"  ⏭️  Skipping (unchanged): {wp['name']}")
        print(f"\n🎉 DRY RUN: {len(to_create)} would be created, {len(to_update)} updated, {len(to_skip)} skipped")
        return

    created = 0
    updated_count = 0
    failed = 0

    # Create new pages
    for wp in to_create:
        wiki_path = wp.get("wiki_path", "")
        success, page_id = create_page(wp)
        if success:
            created += 1
            if page_id:
                index[wiki_path] = {
                    "content_hash": wp.get("content_hash", ""),
                    "file_mtime": wp.get("file_mtime"),
                    "notion_id": page_id,
                    "last_synced": datetime.now().isoformat(),
                }
                save_index(index)  # Save after each page
        else:
            failed += 1
        time.sleep(0.4)

    # Update changed pages
    for wp in to_update:
        wiki_path = wp.get("wiki_path", "")
        notion_id = wp.get("_notion_id")
        if update_page_content(notion_id, wp):
            updated_count += 1
            index[wiki_path] = {
                "content_hash": wp.get("_content_hash", ""),
                "file_mtime": wp.get("_file_mtime"),
                "notion_id": notion_id,
                "last_synced": datetime.now().isoformat(),
            }
            save_index(index)  # Save after each page
        else:
            failed += 1
        time.sleep(0.4)

    # Also update index entries for skipped pages (ensure notion_id is current)
    for wp in to_skip:
        wiki_path = wp.get("wiki_path", "")
        if wiki_path in existing and wiki_path in index:
            index[wiki_path]["notion_id"] = existing[wiki_path]
        elif wiki_path in existing:
            index[wiki_path] = {
                "content_hash": wp.get("content_hash", ""),
                "file_mtime": str(Path(wp.get("file_path", "")).stat().st_mtime) if wp.get("file_path") else None,
                "notion_id": existing[wiki_path],
                "last_synced": datetime.now().isoformat(),
            }
    save_index(index)

    print(f"\n🎉 Sync complete: {created} created, {updated_count} updated, {len(to_skip)} skipped, {failed} failed")


if __name__ == "__main__":
    main()