from __future__ import annotations

from collections import defaultdict
from pathlib import Path


def render_notification(batch_id: str, results: list[dict], settings: dict) -> str:
    """Render scan results grouped by destination folder.

    Output format per group:
      📁 Proposed Destination (count files) — confidence badge
         Original: parent_folder/filename → Proposed name
         Up to 3 samples, with sequential suffix pattern for multi-file groups
    """
    # Group by destination folder
    grouped: dict[str, list[dict]] = defaultdict(list)
    failures = []
    for item in results:
        if item.get("status") == "success":
            dest = item.get("proposedDest", "03-Resources/Scans/")
            grouped[dest].append(item)
        else:
            failures.append(item)

    total = len(results)
    total_success = sum(1 for r in results if r.get("status") == "success")

    lines = [f"📋 **Scan Results** — Batch `{batch_id[:8]}`"]
    lines.append(f"Processed: {total} | ✅ {total_success} | ❌ {len(failures)}")
    lines.append("")

    # Sort destinations alphabetically
    for destination in sorted(grouped):
        items = grouped[destination]
        count = len(items)

        # Determine confidence level for the group
        # Use classification_confidence (how certain the TYPE is correct) when available,
        # fall back to rule-match confidence
        confidences = [item.get("classificationConfidence", item.get("confidence", 0)) for item in items]
        min_confidence = min(confidences) if confidences else 0

        # Classification confidence badge
        cls_confidences = [item.get("classificationConfidence", item.get("confidence", 0)) for item in items]
        min_cls = min(cls_confidences) if cls_confidences else 0
        if min_cls >= 0.85:
            cls_label = "🟢"
        elif min_cls >= 0.70:
            cls_label = "🟡"
        elif min_cls >= 0.50:
            cls_label = "🟠"
        else:
            cls_label = "🔴"

        # Rule-match confidence badge
        rule_confidences = [item.get("confidence", 0) for item in items]
        min_rule = min(rule_confidences) if rule_confidences else 0
        if min_rule >= 0.85:
            rule_label = "🟢"
        elif min_rule >= 0.70:
            rule_label = "🟡"
        elif min_rule >= 0.50:
            rule_label = "🟠"
        else:
            rule_label = "🔴"

        conf_label = f"{cls_label} cls:{min_cls:.0%}" if abs(min_cls - min_rule) > 0.10 else f"{rule_label} {min_confidence:.0%}"

        # Auto-approve indicator
        auto_items = [i for i in items if i.get("autoApproved")]
        auto_note = f" ✅ {len(auto_items)} auto-approved" if auto_items else ""

        # Group header
        lines.append(f"📁 **{destination}** ({count} file{'s' if count != 1 else ''}) — {conf_label}{auto_note}")

        # Show up to 3 sample files with original → proposed mapping
        if count <= 3:
            for item in items:
                orig = _format_original(item)
                proposed = item.get("proposedName", item.get("filename", "Unknown"))
                doc_type = item.get("docType", "")
                person = item.get("person", "")
                side_note = ""
                if item.get("needsSideConfirmation"):
                    side = item.get("side", "unknown")
                    side_conf = item.get("sideConfidence", 0)
                    side_note = f" ⚠️ **Side unclear** ({side_conf:.0%}): Front or Back?"
                lines.append(f"  `{orig}` → `{proposed}` ({doc_type}, {person}){side_note}")
        else:
            # Show first 2 and last 1, indicate pattern
            for item in items[:2]:
                orig = _format_original(item)
                proposed = item.get("proposedName", item.get("filename", "Unknown"))
                doc_type = item.get("docType", "")
                person = item.get("person", "")
                lines.append(f"  `{orig}` → `{proposed}` ({doc_type}, {person})")

            # Check if proposed names have sequential suffixes
            suffixes = _detect_sequential_suffixes(items)

            if suffixes:
                lines.append(f"  … ({count - 3} more: _{suffixes[0]} through _{suffixes[-1]})")
            else:
                lines.append(f"  … ({count - 3} more files in this group)")

            # Show last file
            last_item = items[-1]
            orig = _format_original(last_item)
            proposed = last_item.get("proposedName", last_item.get("filename", "Unknown"))
            lines.append(f"  `{orig}` → `{proposed}`")

        lines.append("")

    # Failures section
    if failures:
        lines.append("❌ **Errors:**")
        for failure in failures:
            fname = failure.get("filename", failure.get("path", "Unknown"))
            error = failure.get("error", "unknown error")
            lines.append(f"  - `{fname}`: {error}")
        lines.append("")

    # Ask for confirmation on low-confidence items and ambiguous items
    low_conf = [r for r in results if r.get("status") == "success" and r.get("classificationConfidence", r.get("confidence", 0)) < 0.70]
    needs_side = [r for r in results if r.get("status") == "success" and r.get("needsSideConfirmation")]
    ambiguous = [r for r in results if r.get("status") == "success" and r.get("ambiguous")]

    # Ambiguous items section first (high priority)
    if ambiguous:
        lines.append("❓ **Ambiguous - Need Your Input:**")
        for item in ambiguous:
            orig = _format_original(item)
            name = item.get("proposedName", item.get("filename", "Unknown"))
            question = item.get("question", "Review needed")
            lines.append(f"  - `{orig}` → `{name}`")
            lines.append(f"    ➤ {question}")
        lines.append("")

    if low_conf or needs_side:
        lines.append("⚠️ **Needs Review:**")
        if needs_side:
            for item in needs_side:
                orig = _format_original(item)
                name = item.get("proposedName", item.get("filename", "Unknown"))
                side = item.get("side", "unknown")
                side_conf = item.get("sideConfidence", 0)
                lines.append(f"  - `{orig}` → `{name}`: Is this **Front** or **Back**? ({side_conf:.0%} confidence it's {side})")
        if low_conf:
            # Group low-confidence items by destination to avoid flooding
            low_by_dest: dict[str, list[dict]] = defaultdict(list)
            for item in low_conf:
                if not item.get("needsSideConfirmation"):
                    dest = item.get("proposedDest", "Unknown")
                    low_by_dest[dest].append(item)
            for dest, litems in sorted(low_by_dest.items()):
                if len(litems) <= 3:
                    for item in litems:
                        orig = _format_original(item)
                        name = item.get("proposedName", item.get("filename", "Unknown"))
                        lines.append(f"  - `{orig}` → `{name}`: Review naming ({item.get('confidence', 0):.0%})")
                else:
                    first_orig = _format_original(litems[0])
                    first_name = litems[0].get("proposedName", "Unknown")
                    last_orig = _format_original(litems[-1])
                    last_name = litems[-1].get("proposedName", "Unknown")
                    lines.append(f"  - `{first_orig}` → `{first_name}` … `{last_orig}` → `{last_name}` (+{len(litems)-2} more): Review naming ({litems[0].get('confidence', 0):.0%})")

    return "\n".join(lines).strip()


def _format_original(item: dict) -> str:
    """Format original location as 'parent_folder/filename'."""
    path = item.get("path", "")
    filename = item.get("filename", "")
    if path:
        p = Path(path)
        parent = p.parent.name
        # Show grandparent/parent if grandparent exists and isn't the root inbox
        grandparent = p.parent.parent.name
        if grandparent and grandparent != "!!!Check":
            return f"{grandparent}/{parent}/{filename}"
        elif parent and parent != "!!!Check":
            return f"{parent}/{filename}"
    return filename


def _detect_sequential_suffixes(items: list[dict]) -> list[str]:
    """Extract sequential suffixes from proposed names, e.g. ['000', '001', '002']."""
    suffixes = []
    for item in items:
        name = item.get("proposedName", "")
        # Check for _NNN suffix pattern at end before extension
        stem = Path(name).stem
        if "_" in stem:
            last_part = stem.rsplit("_", 1)[-1]
            if last_part.isdigit():
                suffixes.append(last_part)
    # Only return if ALL items have sequential suffixes
    if len(suffixes) == len(items):
        return suffixes
    return []