"""Read-only tools for the Coverage Discrepancy Audit, strictly scoped to policy-archive/."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

ARCHIVE_ROOT = Path(__file__).resolve().parent / "policy-archive"
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9\-]*")
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_\-\.]+$")


def _resolve_packet_dir(packet_id: str, archive_root: Path = ARCHIVE_ROOT) -> Path | None:
    if not _SAFE_SEGMENT_RE.match(packet_id) or ".." in packet_id:
        return None
    pdir = (archive_root / packet_id).resolve()
    root_resolved = archive_root.resolve()
    if not str(pdir).startswith(str(root_resolved)) or not pdir.is_dir():
        return None
    return pdir


def load_packet_documents(packet_id: str, archive_root: Path = ARCHIVE_ROOT) -> dict[str, str]:
    """Load all .txt documents inside `policy-archive/<packet_id>/`."""
    pdir = _resolve_packet_dir(packet_id, archive_root)
    if pdir is None:
        return {}
    return {
        p.name: p.read_text(encoding="utf-8")
        for p in sorted(pdir.iterdir())
        if p.is_file() and p.suffix == ".txt"
    }


def build_coverage_audit_tools(
    *,
    allowed_packet_id: str,
    archive_root: Path = ARCHIVE_ROOT,
    read_log: list[dict[str, Any]] | None = None,
) -> list[Any]:
    """Build read-only archive tools strictly pinned to `allowed_packet_id`."""

    @tool
    def list_packet_documents() -> str:
        """List all policy archive documents in the current packet with line and char counts."""
        docs = load_packet_documents(allowed_packet_id, archive_root)
        rows = [
            {
                "doc_id": doc_id,
                "line_count": len(text.splitlines()),
                "char_count": len(text),
            }
            for doc_id, text in sorted(docs.items())
        ]
        if read_log is not None:
            read_log.append({"tool": "list_packet_documents", "count": len(rows)})
        return json.dumps(rows)

    @tool
    def search_packet_documents(query: str, top_k: int = 5) -> str:
        """Keyword-ranked search across all documents in the current packet."""
        docs = load_packet_documents(allowed_packet_id, archive_root)
        q_tokens = [w for w in _WORD_RE.findall(query.lower()) if len(w) >= 2]
        scored: list[tuple[int, str, list[dict[str, Any]]]] = []
        for doc_id, text in sorted(docs.items()):
            lines = text.splitlines()
            matches: list[dict[str, Any]] = []
            doc_score = 0
            for idx, line in enumerate(lines, 1):
                low = line.lower()
                hits = sum(1 for tok in q_tokens if tok in low)
                if hits > 0:
                    doc_score += hits
                    matches.append({"line": idx, "text": line[:200]})
            if doc_score > 0:
                scored.append((doc_score, doc_id, matches[:4]))
        scored.sort(key=lambda item: (-item[0], item[1]))
        results = [
            {"doc_id": doc_id, "score": sc, "snippets": snips}
            for sc, doc_id, snips in scored[: max(1, min(top_k, 10))]
        ]
        if read_log is not None:
            read_log.append(
                {
                    "tool": "search_packet_documents",
                    "query": query,
                    "hits": [r["doc_id"] for r in results],
                }
            )
        return json.dumps(results)

    @tool
    def read_document_section(doc_id: str, start_line: int = 1, end_line: int = 60) -> str:
        """Read a bounded 1-indexed line range (max 80 lines) of a document in this packet."""
        docs = load_packet_documents(allowed_packet_id, archive_root)
        text = docs.get(doc_id)
        if text is None:
            return json.dumps({"error": f"Document '{doc_id}' not found in '{allowed_packet_id}'"})
        lines = text.splitlines()
        lo = max(1, start_line)
        hi = min(len(lines), max(lo, min(end_line, lo + 79)))
        numbered = [f"{i}: {lines[i - 1]}" for i in range(lo, hi + 1)]
        if read_log is not None:
            read_log.append(
                {
                    "tool": "read_document_section",
                    "doc_id": doc_id,
                    "start_line": lo,
                    "end_line": hi,
                }
            )
        return json.dumps(
            {
                "doc_id": doc_id,
                "start_line": lo,
                "end_line": hi,
                "total_lines": len(lines),
                "content": "\n".join(numbered),
            }
        )

    return [list_packet_documents, search_packet_documents, read_document_section]
