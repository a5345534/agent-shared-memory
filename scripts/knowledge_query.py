#!/usr/bin/env python3
"""Deterministic query, resolve, and injection layer for agent-shared-memory.

Stdlib-only CLI that builds a local SQLite FTS5 index from curated Markdown
shared memory entries and supports rebuild-index, list, search, resolve,
inject, and explain subcommands.

The SQLite index is a rebuildable cache; Markdown entries in
knowledge/facts/ are the source of truth.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_MEMORY_TYPES = {
    "feedback",
    "project",
    "reference",
    "user",
    "architectural-invariant",
    "deprecated",
}

VALID_SCOPE_RE = re.compile(r"^(workspace|module:[a-z0-9][a-z0-9-]*|capability:[a-z0-9][a-z0-9-]*)$")

# Top-level directories under knowledge/facts to scan for curated entries.
SCAN_DIRS = ("workspace", "module", "capability")

# Directories / files to skip during scanning.
SKIP_DIRS = {"inbox", "followups", ".index"}
SKIP_FILES = {"README.md", "MEMORY.md"}

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MANIFEST_VERSION = "1"
QUERY_RESULT_VERSION = "1"
INJECTION_CONTEXT_VERSION = "1"

# Boost/penalty constants for ranking (design D3)
BOOST_NAME_MATCH = 0.30
BOOST_DESCRIPTION_MATCH = 0.20
BOOST_SCOPE_EXACT = 0.25
BOOST_ARCHITECTURAL_INVARIANT = 0.15
PENALTY_STALE = 0.10
DEFAULT_STALE_DAYS = 365  # entries older than this receive a staleness penalty


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_workspace_root(start: Path, allow_fallback: bool = False) -> Path:
    """Locate the workspace root.

    Strategy (in order):
    1. Walk up from *start* to find AGENTS.md (existing convention).
    2. Walk up to find knowledge/facts/ directory.
    3. Walk up to find goal-dag-spec.json.
    4. Fall back to *start* itself if nothing else matches.
    """
    current = start.resolve()
    if current.is_file():
        current = current.parent

    markers = ["AGENTS.md", "knowledge/facts", "goal-dag-spec.json"]
    for candidate in [current, *current.parents]:
        for marker in markers:
            marker_path = candidate / marker
            if marker_path.exists():
                return candidate
    if allow_fallback:
        return start.resolve() if start.resolve().is_dir() else start.resolve().parent
    raise SystemExit(
        f"Could not locate workspace root from {start}. "
        f"Expected one of: {', '.join(markers)}. "
        f"Use --allow-root-fallback to force the current directory."
    )


def rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def file_mtime_iso(path: Path) -> str:
    """Return ISO date string from file modification time."""
    ts = path.stat().st_mtime
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).strftime("%Y-%m-%d")


def is_valid_iso_date(value: str) -> bool:
    """Check if a string matches ISO date format YYYY-MM-DD."""
    return bool(ISO_DATE_RE.match(value))


def days_since(date_str: str) -> int | None:
    """Return days since an ISO date string, or None if invalid."""
    if not is_valid_iso_date(date_str):
        return None
    try:
        parsed = dt.date.fromisoformat(date_str)
        return (dt.date.today() - parsed).days
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Frontmatter parser
# ---------------------------------------------------------------------------

def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter subset from Markdown text.

    Handles top-level scalar string fields and simple list fields using
    ``  - item`` syntax. Returns (frontmatter_dict, body_text).
    """
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text.strip()

    frontmatter: dict[str, Any] = {}
    current_key: str | None = None
    for raw_line in match.group(1).splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        # List continuation: "  - value"
        if line.startswith("  - ") and current_key is not None:
            value = line[4:].strip().strip('"')
            existing = frontmatter.setdefault(current_key, [])
            if isinstance(existing, list):
                existing.append(value)
            continue
        # Key: value
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        current_key = key
        if value == "":
            frontmatter[key] = []
        else:
            frontmatter[key] = value.strip('"')
    return frontmatter, text[match.end():].strip()


# ---------------------------------------------------------------------------
# Scope parsing
# ---------------------------------------------------------------------------

def parse_scope(scope: str) -> tuple[str, str]:
    """Return (scope_type, scope_name) from a scope string.

    scope_type is one of: workspace, module, capability.
    scope_name is the module/capability name, or "" for workspace.
    """
    scope = scope.strip()
    if scope == "workspace":
        return "workspace", ""
    if scope.startswith("module:"):
        return "module", scope[len("module:"):].strip()
    if scope.startswith("capability:"):
        return "capability", scope[len("capability:"):].strip()
    return "workspace", ""  # fallback


# ---------------------------------------------------------------------------
# Entry scanning
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class MemoryEntry:
    """A single curated shared-memory entry parsed from a Markdown file."""
    id: str
    path: str          # relative path from workspace root
    scope: str         # e.g. "workspace", "module:workflow"
    scope_type: str    # "workspace", "module", "capability"
    scope_name: str    # module/capability name, or ""
    type: str          # e.g. "architectural-invariant", "reference"
    name: str
    description: str
    verified_at: str
    source: str
    status: str
    tags: list[str]
    body: str
    body_hash: str
    updated_at: str    # from file mtime


def entry_id(relative_path: str, scope: str) -> str:
    """Generate a deterministic entry ID from path and scope."""
    raw = f"{scope}:{relative_path}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def collect_curated_entries(root: Path) -> list[MemoryEntry]:
    """Scan knowledge/facts/{workspace,module,capability}/ for .md files.

    Skips README.md, MEMORY.md, inbox/, followups/ directories.
    """
    entries: list[MemoryEntry] = []
    shared_memory = root / "knowledge" / "facts"
    if not shared_memory.exists():
        return entries

    for scan_dir in SCAN_DIRS:
        scan_path = shared_memory / scan_dir
        if not scan_path.exists():
            continue
        # Walk subdirectories (module/<name>/, capability/<name>/) or flat (workspace/)
        for md_file in sorted(scan_path.rglob("*.md")):
            # Skip excluded directories
            if any(skip in md_file.parts for skip in SKIP_DIRS):
                continue
            # Skip excluded filenames
            if md_file.name in SKIP_FILES:
                continue

            text = md_file.read_text(encoding="utf-8")
            frontmatter, body = parse_frontmatter(text)
            if not frontmatter:
                continue  # no frontmatter, skip

            relative_path = rel(root, md_file)
            scope = frontmatter.get("scope", "workspace")
            scope_type, scope_name = parse_scope(scope)
            memory_type = frontmatter.get("type", "feedback")
            name = frontmatter.get("name", md_file.stem.replace("-", " ").title())
            description = frontmatter.get("description", "")
            verified_at = frontmatter.get("verified_at", "")
            source = frontmatter.get("source", "")
            status = frontmatter.get("status", "active")
            tags_raw = frontmatter.get("tags", [])
            if isinstance(tags_raw, str):
                tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
            elif isinstance(tags_raw, list):
                tags = [str(t).strip() for t in tags_raw if str(t).strip()]
            else:
                tags = []

            # Derive tags from type and scope if none explicitly provided
            if not tags:
                if memory_type:
                    tags.append(memory_type)
                if scope_type:
                    tags.append(scope_type)
                if scope_name:
                    tags.append(scope_name)

            body_text = body.strip()
            body_hash = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
            updated_at = file_mtime_iso(md_file)

            entries.append(MemoryEntry(
                id=entry_id(relative_path, scope),
                path=relative_path,
                scope=scope,
                scope_type=scope_type,
                scope_name=scope_name,
                type=memory_type,
                name=name,
                description=description,
                verified_at=verified_at,
                source=source,
                status=status,
                tags=tags,
                body=body_text,
                body_hash=body_hash,
                updated_at=updated_at,
            ))

    return entries


# ---------------------------------------------------------------------------
# Capability check
# ---------------------------------------------------------------------------

def check_capability() -> list[str]:
    """Verify sqlite3, FTS5, and trigram tokenizer are available.

    Returns a list of error messages. Empty list means all checks passed.
    """
    errors: list[str] = []

    # Check sqlite3 import
    try:
        import sqlite3 as _sqlite_check
    except ImportError:
        errors.append("sqlite3 module not available (required for FTS5 query index)")
        return errors

    # Check sqlite3 library version and FTS5/trigram support
    db = None
    try:
        db = sqlite3.connect(":memory:")
        cur = db.execute("SELECT sqlite_version()")
        ver = cur.fetchone()[0]
        cur.close()
    except Exception as exc:
        errors.append(f"sqlite3 connection failed: {exc}")
        return errors

    try:
        db.execute("CREATE VIRTUAL TABLE _cap_test_fts USING fts5(content, tokenize='trigram')")
        db.execute("DROP TABLE _cap_test_fts")
    except sqlite3.OperationalError as exc:
        msg = str(exc)
        if "fts5" in msg.lower() or "no such module" in msg.lower():
            errors.append("SQLite FTS5 extension not available (compile with -DSQLITE_ENABLE_FTS5)")
        elif "trigram" in msg.lower() or "tokenize" in msg.lower():
            errors.append("SQLite trigram tokenizer not available (compile with -DSQLITE_ENABLE_FTS3_FTS5 or similar)")
        else:
            errors.append(f"FTS5/trigram capability check failed: {exc}")
    finally:
        if db:
            try:
                db.close()
            except Exception:
                pass

    return errors


# ---------------------------------------------------------------------------
# SQLite schema helpers
# ---------------------------------------------------------------------------

CREATE_MEMORY_ENTRIES_SQL = """
CREATE TABLE IF NOT EXISTS memory_entries (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    scope TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_name TEXT,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    tags_json TEXT,
    body_hash TEXT NOT NULL,
    body TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

CREATE_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_entries_fts USING fts5(
    name,
    description,
    body,
    tags,
    path UNINDEXED,
    scope UNINDEXED,
    type UNINDEXED,
    content='memory_entries',
    content_rowid='rowid',
    tokenize='trigram'
)
"""


def build_index(root: Path) -> tuple[Path, int, str]:
    """Rebuild the SQLite FTS5 index from curated entries.

    Returns (sqlite_path, entry_count, content_hash).
    """
    entries = collect_curated_entries(root)

    index_dir = root / "knowledge" / ".index"
    index_dir.mkdir(parents=True, exist_ok=True)

    sqlite_path = index_dir / "memory.sqlite"

    # Remove existing database and rebuild fresh (idempotent rebuild)
    if sqlite_path.exists():
        sqlite_path.unlink()

    db = sqlite3.connect(str(sqlite_path))

    try:
        db.execute(CREATE_MEMORY_ENTRIES_SQL)
        db.execute(CREATE_FTS_SQL)

        for entry in entries:
            tags_json = json.dumps(entry.tags, ensure_ascii=False)
            tags_text = " ".join(entry.tags)

            db.execute(
                """INSERT INTO memory_entries
                   (id, path, scope, scope_type, scope_name, type, name,
                    description, verified_at, source, status, tags_json,
                    body_hash, body, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry.id,
                    entry.path,
                    entry.scope,
                    entry.scope_type,
                    entry.scope_name,
                    entry.type,
                    entry.name,
                    entry.description,
                    entry.verified_at,
                    entry.source,
                    entry.status,
                    tags_json,
                    entry.body_hash,
                    entry.body,
                    entry.updated_at,
                ),
            )

            # Insert into FTS content table via the external content approach.
            rowid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.execute(
                """INSERT INTO memory_entries_fts
                   (rowid, name, description, body, tags, path, scope, type)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    rowid,
                    entry.name,
                    entry.description,
                    entry.body,
                    tags_text,
                    entry.path,
                    entry.scope,
                    entry.type,
                ),
            )

        db.commit()
    finally:
        db.close()

    # Compute content hash (source-derived, excludes generatedAt)
    content_hash = compute_content_hash(entries)
    return sqlite_path, len(entries), content_hash


def compute_content_hash(entries: list[MemoryEntry]) -> str:
    """Compute a deterministic hash from entry data (source-derived).

    Uses sorted, normalized entry fields. Excludes wall-clock timestamps.
    """
    # Build canonical representation
    rows: list[str] = []
    for entry in sorted(entries, key=lambda e: (e.path, e.scope)):
        row_parts = [
            entry.path,
            entry.scope,
            entry.scope_type,
            entry.scope_name or "",
            entry.type,
            entry.name,
            entry.description,
            entry.verified_at,
            entry.source,
            entry.status,
            json.dumps(sorted(entry.tags), ensure_ascii=False),
            entry.body_hash,
            entry.body,
        ]
        rows.append("\t".join(row_parts))
    combined = "\n".join(rows)
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def write_manifest(root: Path, sqlite_path: Path, entry_count: int, content_hash: str) -> Path:
    """Write knowledge/.index/manifest.json."""
    manifest_dir = root / "knowledge" / ".index"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "version": MANIFEST_VERSION,
        "generatedAt": now_iso(),
        "entryCount": entry_count,
        "sourceRoot": rel(root, root / "knowledge" / "facts"),
        "sqlitePath": rel(root, sqlite_path),
        "hash": content_hash,
    }
    manifest_path = manifest_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest_path


# ---------------------------------------------------------------------------
# Rebuild-index subcommand
# ---------------------------------------------------------------------------

def cmd_rebuild_index(root: Path) -> int:
    """Run rebuild-index subcommand."""
    # Capability check
    errors = check_capability()
    if errors:
        print("ERROR: Startup capability check failed:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        print("\nAction: SQLite must be compiled with FTS5 and trigram tokenizer support.", file=sys.stderr)
        print("Example: pip install pysqlite3-binary (if available) or rebuild Python with appropriate flags.", file=sys.stderr)
        return 1

    sqlite_path, entry_count, content_hash = build_index(root)

    # Only rewrite manifest if the source-derived hash changed,
    # or if no manifest exists yet.  This keeps the repository clean
    # when rebuild-index is re-run against unchanged sources.
    manifest_dir = root / "knowledge" / ".index"
    existing_manifest = manifest_dir / "manifest.json"
    skip_manifest = False
    if existing_manifest.exists():
        try:
            existing = json.loads(existing_manifest.read_text(encoding="utf-8"))
            if existing.get("hash") == content_hash:
                skip_manifest = True
        except (json.JSONDecodeError, OSError):
            pass

    if skip_manifest:
        print(f"Index rebuilt: {entry_count} entries indexed (manifest unchanged, hash matches)")
        print(f"  Database: {rel(root, sqlite_path)}")
        print(f"  Manifest: {rel(root, existing_manifest)}  (unchanged)")
        print(f"  Hash:     {content_hash}")
    else:
        manifest_path = write_manifest(root, sqlite_path, entry_count, content_hash)
        print(f"Index rebuilt: {entry_count} entries indexed")
        print(f"  Database: {rel(root, sqlite_path)}")
        print(f"  Manifest: {rel(root, manifest_path)}")
        print(f"  Hash:     {content_hash}")
    return 0


# ---------------------------------------------------------------------------
# List subcommand
# ---------------------------------------------------------------------------

def cmd_list(root: Path, args: argparse.Namespace) -> int:
    """Run list subcommand with optional --scope and --type filters."""
    sqlite_path = root / "knowledge" / ".index" / "memory.sqlite"
    if not sqlite_path.exists():
        print("ERROR: No index found. Run 'rebuild-index' first.", file=sys.stderr)
        return 1

    db = sqlite3.connect(str(sqlite_path))
    db.row_factory = sqlite3.Row

    try:
        conditions: list[str] = []
        params: list[Any] = []

        # Exclude deprecated by default
        conditions.append("type != 'deprecated'")

        # Scope filter
        if args.scope:
            if args.scope == "workspace":
                conditions.append("scope = ?")
                params.append("workspace")
            elif args.scope.startswith("module:"):
                conditions.append("scope = ?")
                params.append(args.scope)
            elif args.scope.startswith("capability:"):
                conditions.append("scope = ?")
                params.append(args.scope)
            else:
                # Exact match for other scopes
                conditions.append("scope = ?")
                params.append(args.scope)

        # Type filter
        if args.type:
            conditions.append("type = ?")
            params.append(args.type)

        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        query = f"""SELECT id, path, scope, scope_type, scope_name, type,
                           name, description, verified_at, source, status,
                           tags_json, body_hash, body, updated_at
                    FROM memory_entries{where}
                    ORDER BY scope_type, scope_name, name"""

        rows = db.execute(query, params).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            tags_raw = row["tags_json"] or "[]"
            try:
                tags = json.loads(tags_raw)
            except json.JSONDecodeError:
                tags = []
            results.append({
                "id": row["id"],
                "path": row["path"],
                "scope": row["scope"],
                "scope_type": row["scope_type"],
                "scope_name": row["scope_name"],
                "type": row["type"],
                "name": row["name"],
                "description": row["description"],
                "verified_at": row["verified_at"],
                "source": row["source"],
                "status": row["status"],
                "tags": tags,
                "body_hash": row["body_hash"],
                "updated_at": row["updated_at"],
            })

        print(json.dumps(results, ensure_ascii=False, indent=2))
    finally:
        db.close()

    return 0


# ---------------------------------------------------------------------------
# Scoring and search
# ---------------------------------------------------------------------------

def compute_score(
    entry: dict[str, Any],
    fts_rank: float,
    query_text: str,
    scope_filter: str = "",
    stale_days_threshold: int = DEFAULT_STALE_DAYS,
    task_type: str = "",
) -> tuple[float, dict[str, Any], list[str]]:
    """Compute a composite relevance score for a memory entry.

    Base score is the normalized FTS5 BM25 rank. Boosts are added for:
    - name match (+0.30 if query text appears in name)
    - description match (+0.20 if query text appears in description)
    - scope exact match (+0.25 if scope matches a scope filter)
    - architectural-invariant type (+0.15)

    Penalty for stale entries (-0.10 if verified_at is older than threshold).

    Returns (final_score, score_breakdown, reasons).
    """
    breakdown: dict[str, Any] = {"fts": 0.0, "scope": 0.0, "type": 0.0}
    reasons: list[str] = []

    # Normalize FTS rank: rank is negative, closer to 0 is better
    # Map to [0, 1] where 1 = perfect match
    fts_score = 1.0 / (1.0 + abs(fts_rank)) if fts_rank <= 0 else 0.5
    breakdown["fts"] = round(fts_score, 4)
    if fts_rank < 0:
        reasons.append(f"fts rank: {fts_rank:.4f}")
        if query_text:
            reasons.append(f"query match: {query_text}")

    total = fts_score
    query_lower = query_text.lower() if query_text else ""

    # Name match boost
    name_lower = entry.get("name", "").lower()
    if query_lower and query_lower in name_lower:
        boost = BOOST_NAME_MATCH
        breakdown["name_match"] = round(boost, 4)
        total += boost
        reasons.append(f"name match: +{BOOST_NAME_MATCH:.2f}")

    # Description match boost
    desc_lower = entry.get("description", "").lower()
    if query_lower and query_lower in desc_lower:
        boost = BOOST_DESCRIPTION_MATCH
        breakdown["description_match"] = round(boost, 4)
        total += boost
        reasons.append(f"description match: +{BOOST_DESCRIPTION_MATCH:.2f}")

    # Scope exact match boost
    if scope_filter and scope_filter == entry.get("scope", ""):
        boost = BOOST_SCOPE_EXACT
        breakdown["scope"] = round(boost, 4)
        total += boost
        reasons.append(f"scope exact ({scope_filter}): +{BOOST_SCOPE_EXACT:.2f}")
    elif scope_filter:
        # Check partial scope match (e.g., --module workflow matches module:workflow)
        entry_scope = entry.get("scope", "")
        scope_filter_lower = scope_filter.lower()
        if scope_filter_lower in entry_scope.lower() or entry_scope.lower().endswith(":" + scope_filter_lower):
            boost = BOOST_SCOPE_EXACT * 0.8  # 80% boost for partial match
            breakdown["scope"] = round(boost, 4)
            total += boost
            reasons.append(f"scope partial ({scope_filter}): +{boost:.2f}")

    # Architectural-invariant type boost
    entry_type = entry.get("type", "")
    if entry_type == "architectural-invariant":
        boost = BOOST_ARCHITECTURAL_INVARIANT
        breakdown["type"] = round(boost, 4)
        total += boost
        reasons.append(f"architectural-invariant: +{BOOST_ARCHITECTURAL_INVARIANT:.2f}")

    # Staleness penalty
    verified_at = entry.get("verified_at", "")
    age_days = days_since(verified_at)
    if age_days is not None and age_days > stale_days_threshold:
        penalty = PENALTY_STALE
        breakdown["staleness_penalty"] = round(-penalty, 4)
        total -= penalty
        reasons.append(f"stale ({age_days}d > {stale_days_threshold}d): -{PENALTY_STALE:.2f}")

    # Ensure score is non-negative
    final_score = max(0.0, total)

    # Task-type tag boost (ranking signal only, not a hard filter).
    # Checks both tags list and tags_json string for compatibility.
    tags = entry.get("tags") or []
    tags_json = entry.get("tags_json", "") or ""
    if task_type:
        task_lower = task_type.lower()
        in_tags = any(task_lower in (t.lower() if isinstance(t, str) else str(t).lower()) for t in (tags if isinstance(tags, list) else []))
        in_json = task_lower in tags_json.lower()
        if in_tags or in_json:
            boost = 0.10
            breakdown["task_type"] = round(boost, 4)
            total += boost
            reasons.append(f"task-type tag ({task_type}): +{boost:.2f}")

    # Re-compute final after task_type boost
    final_score = max(0.0, total)
    return round(final_score, 4), breakdown, reasons


def read_entry_from_db(db: sqlite3.Connection, entry_id: str) -> dict[str, Any] | None:
    """Read a single entry from the memory_entries table."""
    row = db.execute(
        """SELECT id, path, scope, scope_type, scope_name, type,
                  name, description, verified_at, source, status,
                  tags_json, body_hash, body, updated_at
           FROM memory_entries WHERE id = ?""",
        (entry_id,),
    ).fetchone()
    if not row:
        return None
    tags_raw = row["tags_json"] or "[]"
    try:
        tags = json.loads(tags_raw)
    except json.JSONDecodeError:
        tags = []
    return {
        "id": row["id"],
        "path": row["path"],
        "scope": row["scope"],
        "scope_type": row["scope_type"],
        "scope_name": row["scope_name"],
        "type": row["type"],
        "name": row["name"],
        "description": row["description"],
        "verified_at": row["verified_at"],
        "source": row["source"],
        "status": row["status"],
        "tags": tags,
        "body_hash": row["body_hash"],
        "body": row["body"],
        "updated_at": row["updated_at"],
    }


# ---------------------------------------------------------------------------
# Search subcommand
# ---------------------------------------------------------------------------

def cmd_search(root: Path, args: argparse.Namespace) -> int:
    """Run search subcommand: FTS5 query with boost/penalty ranking."""
    sqlite_path = root / "knowledge" / ".index" / "memory.sqlite"
    if not sqlite_path.exists():
        print("ERROR: No index found. Run 'rebuild-index' first.", file=sys.stderr)
        return 1

    db = sqlite3.connect(str(sqlite_path))
    db.row_factory = sqlite3.Row

    try:
        query_text = args.query.strip()
        if not query_text:
            print("ERROR: query text is required", file=sys.stderr)
            return 1

        scope_filter = args.scope or ""

        # Build FTS5 query: search name, description, body, tags
        # Use the FTS5 MATCH syntax with trigram tokenizer
        conditions: list[str] = []
        params: list[Any] = []

        # Exclude deprecated by default
        conditions.append("me.type != 'deprecated'")

        # Scope filter
        if scope_filter:
            if scope_filter == "workspace":
                conditions.append("me.scope = 'workspace'")
            elif scope_filter.startswith("module:"):
                conditions.append("me.scope = ?")
                params.append(scope_filter)
            elif scope_filter.startswith("capability:"):
                conditions.append("me.scope = ?")
                params.append(scope_filter)
            else:
                conditions.append("(me.scope = ? OR me.scope_type = ?)")
                params.append(scope_filter)
                params.append(scope_filter)

        # Type filter
        if args.type:
            conditions.append("me.type = ?")
            params.append(args.type)

        where_clause = " AND ".join(conditions)

        # Query FTS5 with rank; join to memory_entries for metadata
        # FTS5 MATCH syntax for trigram tokenizer supports plain text
        fts_query = f"""SELECT me.*, mefts.rank
                        FROM memory_entries me
                        JOIN memory_entries_fts mefts ON me.rowid = mefts.rowid
                        WHERE memory_entries_fts MATCH ?
                        AND {where_clause}
                        ORDER BY mefts.rank
                        LIMIT ?"""

        limit = args.limit if args.limit else 50
        result_rows = db.execute(fts_query, [query_text, *params, limit]).fetchall()

        # Score and build results
        results: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        for row in result_rows:
            entry_id = row["id"]
            if entry_id in seen_ids:
                continue
            seen_ids.add(entry_id)

            entry_dict = {
                "id": row["id"],
                "path": row["path"],
                "scope": row["scope"],
                "scope_type": row["scope_type"],
                "scope_name": row["scope_name"],
                "type": row["type"],
                "name": row["name"],
                "description": row["description"],
                "verified_at": row["verified_at"],
                "source": row["source"],
                "status": row["status"],
                "tags": json.loads(row["tags_json"] or "[]"),
                "body_hash": row["body_hash"],
                "body": row["body"],
                "updated_at": row["updated_at"],
            }

            fts_rank = row["rank"] if row["rank"] is not None else -100.0
            score, breakdown, reasons = compute_score(
                entry_dict, fts_rank, query_text, scope_filter, task_type=getattr(args, "task_type", "") or ""
            )

            result_entry = {
                "id": entry_dict["id"],
                "path": entry_dict["path"],
                "scope": entry_dict["scope"],
                "scope_type": entry_dict["scope_type"],
                "scope_name": entry_dict["scope_name"],
                "type": entry_dict["type"],
                "name": entry_dict["name"],
                "description": entry_dict["description"],
                "verified_at": entry_dict["verified_at"],
                "source": entry_dict["source"],
                "status": entry_dict["status"],
                "tags": entry_dict["tags"],
                "body_hash": entry_dict["body_hash"],
                "updated_at": entry_dict["updated_at"],
                "score": score,
                "reasons": reasons,
                "scoreBreakdown": breakdown,
            }
            results.append(result_entry)

        # Sort by score descending
        results.sort(key=lambda r: r["score"], reverse=True)

        # Collect excluded entries (deprecated, filtered by scope, etc.)
        excluded: list[dict[str, Any]] = []
        if args.verbose:
            # Show deprecated entries that matched the query
            dep_conditions = []
            dep_params = [query_text]
            if scope_filter:
                dep_conditions.append("me.scope = ?")
                dep_params.append(scope_filter)
            dep_where = " AND " + " AND ".join(dep_conditions) if dep_conditions else ""
            dep_query = f"""SELECT me.*
                            FROM memory_entries me
                            JOIN memory_entries_fts mefts ON me.rowid = mefts.rowid
                            WHERE memory_entries_fts MATCH ?
                            AND me.type = 'deprecated'
                            {f'AND {dep_where}' if dep_where else ''}
                            LIMIT 20"""
            dep_rows = db.execute(dep_query, dep_params).fetchall()
            for row in dep_rows:
                excluded.append({
                    "id": row["id"],
                    "path": row["path"],
                    "name": row["name"],
                    "scope": row["scope"],
                    "type": row["type"],
                    "reasons": ["type deprecated"],
                })

        # Build query result
        query_result = {
            "version": QUERY_RESULT_VERSION,
            "generatedAt": now_iso(),
            "query": {
                "text": query_text,
                "command": "search",
                "filters": {
                    "scope": scope_filter or None,
                    "type": args.type or None,
                },
            },
            "results": results,
            "excluded": excluded,
        }

        print(json.dumps(query_result, ensure_ascii=False, indent=2))

    except sqlite3.OperationalError as exc:
        print(f"ERROR: Search query failed: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()

    return 0


# ---------------------------------------------------------------------------
# Resolve subcommand
# ---------------------------------------------------------------------------

def cmd_resolve(root: Path, args: argparse.Namespace) -> int:
    """Run resolve subcommand: filter entries by module/capability scope."""
    sqlite_path = root / "knowledge" / ".index" / "memory.sqlite"
    if not sqlite_path.exists():
        print("ERROR: No index found. Run 'rebuild-index' first.", file=sys.stderr)
        return 1

    db = sqlite3.connect(str(sqlite_path))
    db.row_factory = sqlite3.Row

    try:
        conditions: list[str] = []
        params: list[Any] = []

        # Exclude deprecated
        conditions.append("type != 'deprecated'")

        # Exclude entries with invalid verified_at
        # We build conditions in Python since SQLite doesn't have regex
        # Post-filter after query

        scope_conditions: list[str] = []

        # Always include workspace-scoped entries
        scope_conditions.append("scope = 'workspace'")

        if args.module:
            module_scope = f"module:{args.module}"
            scope_conditions.append("scope = ?")
            params.append(module_scope)

        if args.capability:
            capability_scope = f"capability:{args.capability}"
            scope_conditions.append("scope = ?")
            params.append(capability_scope)

        if scope_conditions:
            conditions.append(f"({' OR '.join(scope_conditions)})")

        # Task type is a ranking signal, not a hard filter.
        # Passed through to compute_score via _task_type metadata
        # so entries without the tag are still included but not boosted.

        # Query text filter (search across name, description with LIKE)
        if args.query:
            conditions.append("(name LIKE ? OR description LIKE ? OR body LIKE ?)")
            query_like = f"%{args.query}%"
            params.extend([query_like, query_like, query_like])

        where = " WHERE " + " AND ".join(conditions) if conditions else ""

        query_sql = f"""SELECT id, path, scope, scope_type, scope_name, type,
                               name, description, verified_at, source, status,
                               tags_json, body_hash, body, updated_at
                        FROM memory_entries{where}
                        ORDER BY
                          CASE type WHEN 'architectural-invariant' THEN 0 ELSE 1 END,
                          scope_type, scope_name, name"""

        rows = db.execute(query_sql, params).fetchall()

        results: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []

        for row in rows:
            # Filter out entries with invalid verified_at
            verified_at = row["verified_at"]
            if verified_at and not is_valid_iso_date(verified_at):
                excluded.append({
                    "id": row["id"],
                    "path": row["path"],
                    "name": row["name"],
                    "scope": row["scope"],
                    "type": row["type"],
                    "reasons": [f"invalid verified_at: '{verified_at}'"],
                })
                continue

            tags_raw = row["tags_json"] or "[]"
            try:
                tags = json.loads(tags_raw)
            except json.JSONDecodeError:
                tags = []

            entry_dict = {
                "id": row["id"],
                "path": row["path"],
                "scope": row["scope"],
                "scope_type": row["scope_type"],
                "scope_name": row["scope_name"],
                "type": row["type"],
                "name": row["name"],
                "description": row["description"],
                "verified_at": row["verified_at"],
                "source": row["source"],
                "status": row["status"],
                "tags": tags,
                "body_hash": row["body_hash"],
                "body": row["body"],
                "updated_at": row["updated_at"],
            }

            reasons: list[str] = []
            reasons.append(f"scope included: {row['scope']}")
            if row["type"] == "architectural-invariant":
                reasons.append("architectural-invariant priority")

            results.append({
                **entry_dict,
                "score": 1.0 if row["type"] == "architectural-invariant" else 0.5,
                "reasons": reasons,
                "scoreBreakdown": {
                    "fts": 0.0,
                    "scope": BOOST_SCOPE_EXACT if any(
                        row["scope"].endswith(f":{f}") for f in [args.module, args.capability] if f
                    ) else 0.1,
                    "type": BOOST_ARCHITECTURAL_INVARIANT if row["type"] == "architectural-invariant" else 0.0,
                },
            })

        query_result = {
            "version": QUERY_RESULT_VERSION,
            "generatedAt": now_iso(),
            "query": {
                "text": args.query or "",
                "command": "resolve",
                "filters": {
                    "module": args.module or None,
                    "capability": args.capability or None,
                    "taskType": args.task_type or None,
                },
            },
            "results": results,
            "excluded": excluded,
        }

        print(json.dumps(query_result, ensure_ascii=False, indent=2))

    finally:
        db.close()

    return 0


# ---------------------------------------------------------------------------
# Inject subcommand
# ---------------------------------------------------------------------------

def priority_key(entry: dict[str, Any]) -> tuple[int, int, float]:
    """Compute a priority sort key for injection ordering.

    Priority: architectural-invariant first, then scope exact matches,
    then by score descending.
    """
    is_arch = 0 if entry.get("type") == "architectural-invariant" else 1
    score = -entry.get("score", 0.0)  # negative for descending sort
    return (is_arch, score)


def render_injection_markdown(
    entries: list[dict[str, Any]],
    budget_chars: int,
    context: dict[str, str],
) -> tuple[str, int, list[dict[str, Any]]]:
    """Render entries as Markdown injection context within budget.

    Returns (rendered_markdown, used_chars, entry_metadata).
    """
    # Build header
    header = "## Shared Memory Injection Context\n\n"
    context_parts = []
    if context.get("module"):
        context_parts.append(f"Module: {context['module']}")
    if context.get("capability"):
        context_parts.append(f"Capability: {context['capability']}")
    if context.get("taskType"):
        context_parts.append(f"Task: {context['taskType']}")
    if context_parts:
        header += "**Context:** " + " | ".join(context_parts) + "\n\n"
    header += f"_Budget: {budget_chars} chars_\n\n"

    used = len(header)
    if budget_chars > 0 and used >= budget_chars:
        return header[:budget_chars], min(used, budget_chars), []

    # Group entries by scope type
    workspace_entries = [e for e in entries if e.get("scope_type") == "workspace"]
    module_entries = [e for e in entries if e.get("scope_type") == "module"]
    capability_entries = [e for e in entries if e.get("scope_type") == "capability"]

    sections: list[tuple[str, list[dict[str, Any]]]] = [
        ("Workspace Shared Memory", workspace_entries),
        ("Module Shared Memory", module_entries),
        ("Capability Shared Memory", capability_entries),
    ]

    rendered = header
    entry_meta: list[dict[str, Any]] = []
    budget_exceeded = False

    for section_title, section_entries in sections:
        if budget_exceeded or not section_entries:
            continue

        section_header = f"### {section_title}\n\n"
        if used + len(section_header) > budget_chars and budget_chars > 0:
            budget_exceeded = True
            continue

        rendered += section_header
        used += len(section_header)

        for entry in section_entries:
            if budget_exceeded:
                break

            # Entry header line
            entry_header = f"#### {entry['name']}\n"
            entry_header += f"- **Path:** `{entry['path']}`\n"
            entry_header += f"- **Type:** {entry['type']}\n"
            entry_header += f"- **Verified:** {entry.get('verified_at', 'N/A')}\n"
            if entry.get("description"):
                entry_header += f"- **Description:** {entry['description']}\n"
            entry_header += "\n"

            if used + len(entry_header) > budget_chars and budget_chars > 0:
                budget_exceeded = True
                break

            rendered += entry_header
            used += len(entry_header)

            # Body text with truncation
            body = entry.get("body", "")
            body_len = len(body)
            truncated = False
            truncated_at = 0

            remaining = budget_chars - used if budget_chars > 0 else len(body)
            if budget_chars == 0:
                # Zero budget: no bodies
                truncated = True
                truncated_at = 0
                meta = {
                    "id": entry["id"],
                    "name": entry["name"],
                    "path": entry["path"],
                    "type": entry["type"],
                    "scope": entry["scope"],
                    "description": entry.get("description", ""),
                    "verified_at": entry.get("verified_at", ""),
                    "score": entry.get("score", 0.0),
                    "included": False,
                    "truncated": True,
                    "truncatedAt": 0,
                    "bodyLength": body_len,
                    "reasons": ["excluded: budget zero"],
                }
                entry_meta.append(meta)
                continue
            elif budget_chars > 0 and remaining < body_len:
                truncated = True
                # Leave room for "[truncated]" marker
                truncation_marker = "\n_[truncated]_"
                available = remaining - len(truncation_marker) - 2  # -2 for newlines
                if available <= 0:
                    # Not enough room for body at all
                    truncated_at = 0
                    meta = {
                        "id": entry["id"],
                        "name": entry["name"],
                        "path": entry["path"],
                        "type": entry["type"],
                        "scope": entry["scope"],
                        "description": entry.get("description", ""),
                        "verified_at": entry.get("verified_at", ""),
                        "score": entry.get("score", 0.0),
                        "included": True,
                        "truncated": True,
                        "truncatedAt": 0,
                        "bodyLength": body_len,
                        "reasons": ["body truncated: budget exceeded"],
                    }
                    entry_meta.append(meta)
                    continue
                else:
                    truncated_at = available
                    truncated_body = body[:available] + f"\n{truncation_marker}\n"
            else:
                truncated_body = body + "\n"

            if used + len(truncated_body) > budget_chars and budget_chars > 0:
                budget_exceeded = True
                meta = {
                    "id": entry["id"],
                    "name": entry["name"],
                    "path": entry["path"],
                    "type": entry["type"],
                    "scope": entry["scope"],
                    "description": entry.get("description", ""),
                    "verified_at": entry.get("verified_at", ""),
                    "score": entry.get("score", 0.0),
                    "included": False,
                    "truncated": False,
                    "truncatedAt": 0,
                    "bodyLength": body_len,
                    "reasons": ["excluded: budget exceeded before body"],
                }
                entry_meta.append(meta)
                break

            rendered += truncated_body
            used += len(truncated_body)

            meta = {
                "id": entry["id"],
                "name": entry["name"],
                "path": entry["path"],
                "type": entry["type"],
                "scope": entry["scope"],
                "description": entry.get("description", ""),
                "verified_at": entry.get("verified_at", ""),
                "score": entry.get("score", 0.0),
                "included": True,
                "truncated": truncated,
                "truncatedAt": truncated_at if truncated else None,
                "bodyLength": body_len,
                "reasons": ["body trimmed: budget" if truncated else "included"],
            }
            entry_meta.append(meta)

    return rendered, used, entry_meta


def cmd_inject(root: Path, args: argparse.Namespace) -> int:
    """Run inject subcommand: produce prompt-ready context from resolved entries."""
    sqlite_path = root / "knowledge" / ".index" / "memory.sqlite"
    if not sqlite_path.exists():
        print("ERROR: No index found. Run 'rebuild-index' first.", file=sys.stderr)
        return 1

    db = sqlite3.connect(str(sqlite_path))
    db.row_factory = sqlite3.Row

    try:
        conditions: list[str] = []
        params: list[Any] = []

        # Exclude deprecated
        conditions.append("type != 'deprecated'")

        scope_conditions: list[str] = []

        # Always include workspace-scoped entries
        scope_conditions.append("scope = 'workspace'")

        if args.module:
            module_scope = f"module:{args.module}"
            scope_conditions.append("scope = ?")
            params.append(module_scope)

        if args.capability:
            capability_scope = f"capability:{args.capability}"
            scope_conditions.append("scope = ?")
            params.append(capability_scope)

        if scope_conditions:
            conditions.append(f"({' OR '.join(scope_conditions)})")

        # Task type is a ranking signal (passed via _task_type metadata to
        # compute_score for boost), not a hard filter. All entries matching
        # scope are included regardless of task_type tags.

        where = " WHERE " + " AND ".join(conditions) if conditions else ""

        # Query entries with priority ordering for resolve + scoring
        query_sql = f"""SELECT id, path, scope, scope_type, scope_name, type,
                               name, description, verified_at, source, status,
                               tags_json, body_hash, body, updated_at
                        FROM memory_entries{where}
                        ORDER BY
                          CASE type WHEN 'architectural-invariant' THEN 0 ELSE 1 END,
                          CASE
                            WHEN scope LIKE ? THEN 0
                            WHEN scope LIKE ? THEN 1
                            WHEN scope = 'workspace' THEN 2
                            ELSE 3
                          END,
                          name"""

        module_like = f"module:{args.module}%" if args.module else "%"
        capability_like = f"capability:{args.capability}%" if args.capability else "%"
        query_params = [*params, module_like, capability_like]

        rows = db.execute(query_sql, query_params).fetchall()

        entries: list[dict[str, Any]] = []
        for row in rows:
            # Skip entries with invalid verified_at
            verified_at = row["verified_at"]
            if verified_at and not is_valid_iso_date(verified_at):
                continue

            tags_raw = row["tags_json"] or "[]"
            try:
                tags = json.loads(tags_raw)
            except json.JSONDecodeError:
                tags = []

            entry_dict = {
                "id": row["id"],
                "path": row["path"],
                "scope": row["scope"],
                "scope_type": row["scope_type"],
                "scope_name": row["scope_name"],
                "type": row["type"],
                "name": row["name"],
                "description": row["description"],
                "verified_at": row["verified_at"],
                "source": row["source"],
                "status": row["status"],
                "tags": tags,
                "body_hash": row["body_hash"],
                "body": row["body"],
                "updated_at": row["updated_at"],
                "score": 1.0 if row["type"] == "architectural-invariant" else 0.5,
            }
            entries.append(entry_dict)

        # Sort by priority: architectural-invariant first, then score
        entries.sort(key=priority_key)

        budget_chars = args.budget_chars if args.budget_chars is not None else 1000000

        context = {
            "module": args.module or "",
            "capability": args.capability or "",
            "taskType": args.task_type or "",
        }

        if args.format == "markdown":
            rendered, used_chars, entry_meta = render_injection_markdown(
                entries, budget_chars, context
            )
            injection_output = rendered
        elif args.format == "json":
            # For JSON format, include entries and metadata directly
            injection_entries: list[dict[str, Any]] = []
            used_chars = 0
            for entry in entries:
                body_len = len(entry.get("body", ""))
                truncated = False
                truncated_at = 0
                included = True

                if budget_chars > 0:
                    entry_json = json.dumps({
                        "id": entry["id"],
                        "name": entry["name"],
                        "type": entry["type"],
                        "scope": entry["scope"],
                        "body": entry.get("body", ""),
                    }, ensure_ascii=False)
                    if used_chars + len(entry_json) > budget_chars:
                        # Truncate
                        available = budget_chars - used_chars - 50  # overhead
                        if available < len(entry.get("body", "")):
                            truncated = True
                            truncated_at = available

                meta = {
                    "id": entry["id"],
                    "name": entry["name"],
                    "path": entry["path"],
                    "type": entry["type"],
                    "scope": entry["scope"],
                    "description": entry.get("description", ""),
                    "verified_at": entry.get("verified_at", ""),
                    "score": entry.get("score", 0.0),
                    "included": included,
                    "truncated": truncated,
                    "truncatedAt": truncated_at,
                    "bodyLength": body_len,
                    "reasons": ["included"] if not truncated else ["body truncated: budget exceeded"],
                }
                injection_entries.append(meta)

            injection_output = json.dumps({
                "entries": [
                    {
                        "id": e["id"],
                        "name": e["name"],
                        "path": e["path"],
                        "type": e["type"],
                        "scope": e["scope"],
                        "description": e.get("description", ""),
                        "verified_at": e.get("verified_at", ""),
                        "score": e["score"],
                        "body": e.get("body", ""),
                    }
                    for e in entries
                ],
            }, ensure_ascii=False, indent=2)
            used_chars = len(injection_output)
            entry_meta = injection_entries

        injection_context = {
            "version": INJECTION_CONTEXT_VERSION,
            "generatedAt": now_iso(),
            "budget": {
                "budgetChars": budget_chars,
                "usedChars": used_chars,
                "remainingChars": max(0, budget_chars - used_chars),
                "approximateTokens": used_chars // 4,
            },
            "context": context,
            "entries": entry_meta,
            "rendered": injection_output,
        }

        # Output just the rendered text for markdown mode (for agent consumption)
        if args.format == "markdown":
            if args.output_json_meta:
                # Print metadata to stderr, markdown to stdout
                print(injection_output, end="")
                print(
                    json.dumps(
                        {k: v for k, v in injection_context.items() if k != "rendered"},
                        ensure_ascii=False,
                        indent=2,
                    ),
                    file=sys.stderr,
                )
            else:
                print(injection_output, end="")
        else:
            print(json.dumps(injection_context, ensure_ascii=False, indent=2))

    finally:
        db.close()

    return 0


# ---------------------------------------------------------------------------
# Explain subcommand
# ---------------------------------------------------------------------------

def cmd_explain(root: Path, args: argparse.Namespace) -> int:
    """Run explain subcommand: show score breakdown and selection/exclusion reasons."""
    sqlite_path = root / "knowledge" / ".index" / "memory.sqlite"
    if not sqlite_path.exists():
        print("ERROR: No index found. Run 'rebuild-index' first.", file=sys.stderr)
        return 1

    db = sqlite3.connect(str(sqlite_path))
    db.row_factory = sqlite3.Row

    try:
        query_text = args.query.strip() if args.query else ""
        scope_filter = args.scope or ""
        module_filter = args.module or ""
        capability_filter = args.capability or ""

        # Build a comprehensive scope filter
        resolve_scope = ""
        if module_filter:
            resolve_scope = f"module:{module_filter}"
        elif capability_filter:
            resolve_scope = f"capability:{capability_filter}"
        elif scope_filter:
            resolve_scope = scope_filter

        selected: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []

        if query_text:
            # Use FTS5 search for explain
            conditions: list[str] = []
            params: list[Any] = []

            conditions.append("me.type != 'deprecated'")

            if resolve_scope:
                conditions.append("(me.scope = ? OR me.scope = 'workspace')")
                params.append(resolve_scope)

            where_clause = " AND ".join(conditions)
            fts_query = f"""SELECT me.*, mefts.rank
                            FROM memory_entries me
                            JOIN memory_entries_fts mefts ON me.rowid = mefts.rowid
                            WHERE memory_entries_fts MATCH ?
                            AND {where_clause}
                            ORDER BY mefts.rank
                            LIMIT 50"""
            rows = db.execute(fts_query, [query_text, *params]).fetchall()

            for row in rows:
                entry_dict = {
                    "id": row["id"],
                    "path": row["path"],
                    "scope": row["scope"],
                    "scope_type": row["scope_type"],
                    "scope_name": row["scope_name"],
                    "type": row["type"],
                    "name": row["name"],
                    "description": row["description"],
                    "verified_at": row["verified_at"],
                    "source": row["source"],
                    "status": row["status"],
                    "tags": json.loads(row["tags_json"] or "[]"),
                    "body_hash": row["body_hash"],
                    "updated_at": row["updated_at"],
                }
                fts_rank = row["rank"] if row["rank"] is not None else -100.0
                score, breakdown, reasons = compute_score(
                    entry_dict, fts_rank, query_text, resolve_scope, task_type=getattr(args, "task_type", "") or ""
                )
                selected.append({
                    "id": entry_dict["id"],
                    "path": entry_dict["path"],
                    "name": entry_dict["name"],
                    "scope": entry_dict["scope"],
                    "type": entry_dict["type"],
                    "score": score,
                    "reasons": reasons,
                    "scoreBreakdown": breakdown,
                })

            # Find excluded entries
            dep_query = f"""SELECT me.*
                            FROM memory_entries me
                            JOIN memory_entries_fts mefts ON me.rowid = mefts.rowid
                            WHERE memory_entries_fts MATCH ?
                            AND me.type = 'deprecated'
                            LIMIT 20"""
            dep_rows = db.execute(dep_query, [query_text]).fetchall()
            for row in dep_rows:
                excluded.append({
                    "id": row["id"],
                    "path": row["path"],
                    "name": row["name"],
                    "scope": row["scope"],
                    "type": row["type"],
                    "reasons": ["type deprecated: excluded from results"],
                })

        else:
            # Resolve-based explain
            conditions: list[str] = []
            params: list[Any] = []

            conditions.append("type != 'deprecated'")

            scope_conditions: list[str] = ["scope = 'workspace'"]
            if module_filter:
                scope_conditions.append("scope = ?")
                params.append(f"module:{module_filter}")
            if capability_filter:
                scope_conditions.append("scope = ?")
                params.append(f"capability:{capability_filter}")
            if scope_filter and not module_filter and not capability_filter:
                scope_conditions.append("scope = ?")
                params.append(scope_filter)

            conditions.append(f"({' OR '.join(scope_conditions)})")

            where = " WHERE " + " AND ".join(conditions)
            query_sql = f"""SELECT id, path, scope, scope_type, scope_name, type,
                                   name, description, verified_at, source, status,
                                   tags_json, body_hash, body, updated_at
                            FROM memory_entries{where}
                            ORDER BY scope_type, scope_name, name"""

            rows = db.execute(query_sql, params).fetchall()
            for row in rows:
                reasons: list[str] = []
                breakdown: dict[str, Any] = {"fts": 0.0, "scope": 0.0, "type": 0.0}

                if not is_valid_iso_date(row["verified_at"]) and row["verified_at"]:
                    excluded.append({
                        "id": row["id"],
                        "path": row["path"],
                        "name": row["name"],
                        "scope": row["scope"],
                        "type": row["type"],
                        "reasons": [f"invalid verified_at: '{row['verified_at']}'"],
                    })
                    continue

                reasons.append(f"scope match: {row['scope']}")
                if resolve_scope and row["scope"] == resolve_scope:
                    breakdown["scope"] = BOOST_SCOPE_EXACT
                    reasons.append(f"scope exact: +{BOOST_SCOPE_EXACT:.2f}")
                elif row["scope"] == "workspace":
                    breakdown["scope"] = 0.1
                    reasons.append("scope workspace: +0.10")

                if row["type"] == "architectural-invariant":
                    breakdown["type"] = BOOST_ARCHITECTURAL_INVARIANT
                    reasons.append(f"architectural-invariant: +{BOOST_ARCHITECTURAL_INVARIANT:.2f}")

                score = 0.5 + breakdown.get("scope", 0.0) + breakdown.get("type", 0.0)
                selected.append({
                    "id": row["id"],
                    "path": row["path"],
                    "name": row["name"],
                    "scope": row["scope"],
                    "type": row["type"],
                    "score": round(score, 4),
                    "reasons": reasons,
                    "scoreBreakdown": breakdown,
                })

        # Sort selected by score descending
        selected.sort(key=lambda r: r["score"], reverse=True)

        explain_result = {
            "version": QUERY_RESULT_VERSION,
            "generatedAt": now_iso(),
            "query": {
                "text": query_text,
                "command": "explain",
                "filters": {
                    "scope": scope_filter or None,
                    "module": module_filter or None,
                    "capability": capability_filter or None,
                },
            },
            "results": selected,
            "excluded": excluded,
        }

        print(json.dumps(explain_result, ensure_ascii=False, indent=2))

    finally:
        db.close()

    return 0


# ---------------------------------------------------------------------------
# Init subcommand helpers (Slice 1)
# ---------------------------------------------------------------------------


def _init_create_directories(root: Path) -> dict:
    """Create the knowledge directory structure.

    Creates all required directories under root. Skips any that already
    exist (idempotent). Returns {status, message, created, existed}.
    """
    dirs = [
        "knowledge/facts/workspace",
        "knowledge/facts/module",
        "knowledge/facts/capability",
        "knowledge/inbox",
        "knowledge/followups/skill",
        "knowledge/followups/module-doc",
        "knowledge/.index",
    ]
    created = 0
    existed = 0
    failures: list[str] = []
    for d in dirs:
        target = root / d
        if target.exists():
            existed += 1
            continue
        try:
            target.mkdir(parents=True, exist_ok=True)
            created += 1
        except OSError as exc:
            failures.append(f"{d}: {exc}")

    status = "ok" if not failures else "failed"
    parts: list[str] = []
    if created:
        parts.append(f"{created} created")
    if existed:
        parts.append(f"{existed} already existed")
    if failures:
        parts.append(f"{len(failures)} failed")
    return {
        "status": status,
        "message": ", ".join(parts) if parts else "(no change)",
        "created": created,
        "existed": existed,
        "failures": failures,
    }


def _init_copy_starters(root: Path) -> dict:
    """Copy starter files from the installed starter directory.

    Pattern:
      starter/knowledge/facts/README.md -> knowledge/facts/README.md
      starter/knowledge/facts/{workspace,module,capability}/README.md
        -> knowledge/facts/{workspace,module,capability}/README.md
      starter/knowledge/facts/workspace/MEMORY.md
        -> knowledge/facts/workspace/MEMORY.md
      starter/knowledge/facts/inbox/README.md -> knowledge/inbox/README.md
      starter/knowledge/facts/followups/** -> knowledge/followups/**

    Skips any target file that already exists (idempotent).
    Returns {status, message, copies, skipped}.
    """
    # Try script-relative path first (when script is inside the repo),
    # then fall back to workspace-relative path (when script is at root).
    starter_candidates = [
        Path(__file__).resolve().parent.parent / "starter" / "knowledge" / "facts",
        root / "shared-knowledge" / "starter" / "knowledge" / "facts",
    ]
    starter_base: Path | None = None
    for candidate in starter_candidates:
        if candidate.is_dir():
            starter_base = candidate
            break

    if starter_base is None:
        tried = ", ".join(str(c) for c in starter_candidates)
        return {
            "status": "failed",
            "message": f"Starter directory not found (tried: {tried})",
            "copies": [],
            "skipped": [],
        }

    copies: list[str] = []
    skipped: list[str] = []
    failures: list[str] = []

    def copy_file(src: Path, dst: Path, label: str) -> None:
        if not src.exists():
            return
        if dst.exists():
            skipped.append(label)
            return
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            copies.append(label)
        except OSError as exc:
            failures.append(f"{label}: {exc}")

    # Curated facts convention docs.
    copy_file(starter_base / "README.md", root / "knowledge" / "facts" / "README.md", "facts/README.md")
    for scope_dir in ("workspace", "module", "capability"):
        copy_file(
            starter_base / scope_dir / "README.md",
            root / "knowledge" / "facts" / scope_dir / "README.md",
            f"facts/{scope_dir}/README.md",
        )

    # Workspace always-on index.
    copy_file(
        starter_base / "workspace" / "MEMORY.md",
        root / "knowledge" / "facts" / "workspace" / "MEMORY.md",
        "facts/workspace/MEMORY.md",
    )

    # Inbox and follow-up artifacts live under knowledge/, not knowledge/facts/.
    copy_file(starter_base / "inbox" / "README.md", root / "knowledge" / "inbox" / "README.md", "inbox/README.md")
    copy_file(starter_base / "followups" / "README.md", root / "knowledge" / "followups" / "README.md", "followups/README.md")
    for kind in ("skill", "module-doc"):
        copy_file(
            starter_base / "followups" / kind / ".gitkeep",
            root / "knowledge" / "followups" / kind / ".gitkeep",
            f"followups/{kind}/.gitkeep",
        )

    status = "skipped" if (not copies and not failures) else ("ok" if not failures else "failed")
    parts: list[str] = []
    if copies:
        parts.append(f"{len(copies)} copied")
    if skipped:
        parts.append(f"{len(skipped)} skipped")
    if failures:
        parts.append(f"{len(failures)} failed")
    return {
        "status": status,
        "message": ", ".join(parts) if parts else "(no starter files found)",
        "copies": copies,
        "skipped": skipped,
        "failures": failures,
    }


def _init_inject_b1(root: Path) -> dict:
    """Ensure AGENTS.md contains the B1 shared-knowledge section.

    Uses `<!-- shared-knowledge B1 -->` sentinel to detect existing injection.
    If AGENTS.md exists without sentinel, appends B1 section.
    If AGENTS.md does not exist, creates it with B1 section.
    """
    agents_md = root / "AGENTS.md"
    sentinel = "<!-- shared-knowledge B1 -->"
    b1_section = f"""
{sentinel}
## Workspace Shared Knowledge

See `knowledge/facts/workspace/MEMORY.md` for the workspace always-on shared-memory index.
"""

    if agents_md.exists():
        content = agents_md.read_text(encoding="utf-8")
        if sentinel in content:
            return {"status": "skipped", "message": "B1 section already present"}
        # Append, ensuring newline before
        with agents_md.open("a", encoding="utf-8") as f:
            if not content.endswith("\n"):
                f.write("\n")
            f.write(b1_section.lstrip("\n"))
        return {"status": "ok", "message": "B1 section appended"}
    else:
        agents_md.write_text(f"# AGENTS.md\n{b1_section}", encoding="utf-8")
        return {"status": "ok", "message": "AGENTS.md created with B1 section"}


def _init_ensure_gitignore(root: Path) -> dict:
    """Ensure .gitignore contains the knowledge/.index/ entry.

    The SQLite index cache at knowledge/.index/ must not be tracked in git.
    If .gitignore does not exist, creates it with the entry.
    If .gitignore exists without the entry, appends it.
    """
    gitignore = root / ".gitignore"
    line = "knowledge/.index/"

    if gitignore.exists():
        content = gitignore.read_text(encoding="utf-8")
        lines = content.splitlines()
        if line in lines:
            return {"status": "skipped", "message": "knowledge/.index/ already in .gitignore"}
        # Append at end, ensuring newline separator
        with gitignore.open("a", encoding="utf-8") as f:
            if content and not content.endswith("\n"):
                f.write("\n")
            f.write(f"{line}\n")
        return {"status": "ok", "message": "knowledge/.index/ added to .gitignore"}
    else:
        gitignore.write_text(f"{line}\n", encoding="utf-8")
        return {"status": "ok", "message": ".gitignore created with knowledge/.index/"}


def _init_rebuild_index(root: Path) -> dict:
    """Run rebuild-index internally and report the result.

    Zero entries is a valid result (user adds facts later).
    """
    try:
        exit_code = cmd_rebuild_index(root)
        if exit_code == 0:
            # Count entries in the fresh index
            sqlite_path = root / "knowledge" / ".index" / "memory.sqlite"
            count = 0
            if sqlite_path.exists():
                try:
                    db = sqlite3.connect(str(sqlite_path))
                    count = db.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0]
                    db.close()
                except (sqlite3.OperationalError, Exception):
                    pass
            return {"status": "ok", "message": f"{count} entries indexed"}
        return {"status": "failed", "message": f"rebuild-index exited with code {exit_code}"}
    except Exception as exc:
        return {"status": "failed", "message": f"rebuild-index failed: {exc}"}


def detect_harness(root: Path) -> tuple[str, str]:
    """Detect the active agent harness environment.

    Checks well-known markers in priority order:
      1. Pi: ~/.pi/ directory exists
      2. OpenCode: .opencode.json in workspace root
      3. GitHub Actions: GITHUB_ACTIONS environment variable set
      4. None: fallback

    Returns (harness_name, module_path) where module_path is the dotted
    import path for the matching adapter (e.g. "hooks.pi").
    """
    # Priority 1: Pi harness
    if (Path.home() / ".pi").is_dir():
        return "pi", "hooks.pi"

    # Priority 2: OpenCode harness
    if (root / ".opencode.json").is_file():
        return "opencode", "hooks.opencode"

    # Priority 3: GitHub Actions CI
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return "github-actions", "hooks.github_actions"

    # Priority 4: No harness detected
    return "none", "hooks.none"


def _init_install_hook(root: Path, dry_run: bool = False, hook_scope: str = "workspace") -> dict:
    """Detect the active harness and install the matching hook adapter.

    Uses detect_harness() to find the active harness, then dynamically
    imports the matching adapter module and calls its install() function.

    In dry_run mode, returns what would happen without making changes.
    Returns {status, message, path, harness}.
    """
    harness_name, module_path = detect_harness(root)

    if dry_run:
        if harness_name == "none":
            return {
                "status": "ok",
                "message": f"[dry-run] would print manual hook instructions (harness: {harness_name})",
                "path": None,
                "harness": harness_name,
            }
        scope_suffix = f" ({hook_scope} scope)" if harness_name == "pi" else ""
        return {
            "status": "ok",
            "message": f"[dry-run] would install {harness_name} hook adapter{scope_suffix}",
            "path": None,
            "harness": harness_name,
            "scope": hook_scope if harness_name == "pi" else None,
        }

    # Verified safe: importlib.import_module is stdlib.
    import importlib
    try:
        adapter = importlib.import_module(module_path)
    except (ImportError, ModuleNotFoundError) as exc:
        return {
            "status": "failed",
            "message": f"Failed to load adapter {module_path}: {exc}",
            "path": None,
            "harness": harness_name,
        }

    if harness_name == "pi":
        result = adapter.install(root, scope=hook_scope)
    else:
        result = adapter.install(root)
    result["harness"] = harness_name
    return result


def cmd_init(root: Path, args: argparse.Namespace | None = None) -> int:
    """Run the init subcommand: bootstrap the shared-knowledge workspace.

    Orchestrates all init steps in order:
      1. Create directory structure
      2. Copy starter files
      3. B1 injection into AGENTS.md
      4. .gitignore check
      5. First rebuild-index
      6. Hook adapter installation (via harness detection)

    Supports --skip-hook (skip step 6) and --dry-run (print what would
    be done without making changes). Each step checks preconditions before
    acting, making init idempotent (unless --dry-run is used).
    """
    skip_hook = getattr(args, "skip_hook", False) if args else False
    dry_run = getattr(args, "dry_run", False) if args else False
    hook_scope = getattr(args, "hook_scope", "workspace") if args else "workspace"

    print("knowledge init")
    if dry_run:
        print("  [dry-run] No changes will be made.")
    print()

    steps: list[tuple[str, dict]] = []

    if dry_run:
        # Dry-run: report what would happen without making changes
        steps.append(("📁  directories", {
            "status": "ok",
            "message": "[dry-run] would create knowledge/ directory structure",
        }))
        steps.append(("📄  starter files", {
            "status": "ok",
            "message": "[dry-run] would copy starter files from templates",
        }))
        # Check if B1 already exists
        agents_md = root / "AGENTS.md"
        sentinel = "<!-- shared-knowledge B1 -->"
        if agents_md.exists() and sentinel in agents_md.read_text(encoding="utf-8"):
            b1_msg = "[dry-run] B1 section already present (would skip)"
        else:
            b1_msg = "[dry-run] would inject B1 section into AGENTS.md"
        steps.append(("📋  AGENTS.md", {"status": "ok", "message": b1_msg}))

        gitignore = root / ".gitignore"
        if gitignore.exists() and "knowledge/.index/" in gitignore.read_text(encoding="utf-8").splitlines():
            gi_msg = "[dry-run] knowledge/.index/ already in .gitignore (would skip)"
        else:
            gi_msg = "[dry-run] would add knowledge/.index/ to .gitignore"
        steps.append(("🙈  .gitignore", {"status": "ok", "message": gi_msg}))

        steps.append(("🔍  query index", {
            "status": "ok",
            "message": "[dry-run] would rebuild query index",
        }))

        if not skip_hook:
            harness_name, _ = detect_harness(root)
            if harness_name == "none":
                hook_msg = f"[dry-run] would print manual hook instructions (harness: {harness_name})"
            else:
                scope_suffix = f" ({hook_scope} scope)" if harness_name == "pi" else ""
                hook_msg = f"[dry-run] would install {harness_name} hook adapter{scope_suffix}"
            steps.append(("🪝  hook", {"status": "ok", "message": hook_msg, "path": None, "harness": harness_name}))
        else:
            steps.append(("🪝  hook", {"status": "skipped", "message": "skipped (--skip-hook)"}))
    else:
        # Step 1: Create directories
        result = _init_create_directories(root)
        steps.append(("📁  directories", result))

        # Step 2: Copy starter files
        result = _init_copy_starters(root)
        steps.append(("📄  starter files", result))

        # Step 3: B1 injection into AGENTS.md
        result = _init_inject_b1(root)
        steps.append(("📋  AGENTS.md", result))

        # Step 4: .gitignore check
        result = _init_ensure_gitignore(root)
        steps.append(("🙈  .gitignore", result))

        # Step 5: Rebuild index
        result = _init_rebuild_index(root)
        steps.append(("🔍  query index", result))

        # Step 6: Hook adapter (Slice 3 — harness detection + dispatch)
        if not skip_hook:
            result = _init_install_hook(root, hook_scope=hook_scope)
            steps.append(("🪝  hook", result))
        else:
            steps.append(("🪝  hook", {"status": "skipped", "message": "skipped (--skip-hook)"}))

    # Status report
    all_ok = True
    for label, result in steps:
        s = result["status"]
        if s == "ok":
            icon = "✅"
        elif s == "skipped":
            icon = "⚠️"
        else:
            icon = "❌"
            all_ok = False
        print(f"  {icon}  {label:25s} {result['message']}")

    print()
    if dry_run:
        print("Dry-run complete. Use `knowledge init` (without --dry-run) to apply changes.")
    elif all_ok:
        print("Ready. Add facts to knowledge/facts/, then `knowledge_query.py rebuild-index`.")
        print("Run `knowledge_absorb.py hook` to absorb inbox candidates.")
    else:
        print("Some steps failed. Review the errors above.")

    return 0 if all_ok else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministic shared-memory query index CLI (stdlib-only)"
    )
    parser.add_argument(
        "--root", default=".",
        help="Workspace root or path inside it (default: current directory)",
    )
    parser.add_argument(
        "--allow-root-fallback",
        action="store_true",
        help="Fall back to the current directory if workspace root cannot be found (default: exit with error)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # rebuild-index
    rebuild = subparsers.add_parser(
        "rebuild-index",
        help="Rebuild SQLite FTS5 query index from curated shared-memory entries",
    )

    # list
    list_parser = subparsers.add_parser(
        "list",
        help="List entries in the query index",
    )
    list_parser.add_argument(
        "--scope",
        help="Filter by scope (workspace, module:<name>, capability:<name>)",
    )
    list_parser.add_argument(
        "--type",
        help="Filter by entry type (feedback, reference, architectural-invariant, deprecated, etc.)",
    )

    # search
    search_parser = subparsers.add_parser(
        "search",
        help="Search the query index with BM25+boost/penalty ranking",
    )
    search_parser.add_argument(
        "query",
        help="Search query text",
    )
    search_parser.add_argument(
        "--scope",
        help="Filter by scope (workspace, module:<name>, capability:<name>)",
    )
    search_parser.add_argument(
        "--type",
        help="Filter by entry type",
    )
    search_parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum number of results (default: 50)",
    )
    search_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show excluded entries in output",
    )
    search_parser.add_argument(
        "--task-type",
        help="Task type for ranking boost (not a hard filter)",
    )

    # resolve
    resolve_parser = subparsers.add_parser(
        "resolve",
        help="Resolve entries by module/capability scope filtering",
    )
    resolve_parser.add_argument(
        "--module",
        help="Filter by module name",
    )
    resolve_parser.add_argument(
        "--capability",
        help="Filter by capability name",
    )
    resolve_parser.add_argument(
        "--task-type",
        help="Task type for ranking boost (not a hard filter)",
    )
    resolve_parser.add_argument(
        "--query",
        help="Optional query text for relevance matching",
    )

    # inject
    inject_parser = subparsers.add_parser(
        "inject",
        help="Produce prompt-ready injection context",
    )
    inject_parser.add_argument(
        "--module",
        help="Target module scope",
    )
    inject_parser.add_argument(
        "--capability",
        help="Target capability scope",
    )
    inject_parser.add_argument(
        "--task-type",
        help="Target task type",
    )
    inject_parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="Output format (default: markdown)",
    )
    inject_parser.add_argument(
        "--budget-chars",
        type=int,
        default=None,
        help="Character budget for injection output",
    )
    inject_parser.add_argument(
        "--output-json-meta",
        action="store_true",
        help="Output JSON metadata to stderr alongside markdown",
    )

    # explain
    explain_parser = subparsers.add_parser(
        "explain",
        help="Explain score breakdown and selection/exclusion reasons",
    )
    explain_parser.add_argument(
        "--query",
        help="Query text for search-based explain",
    )
    explain_parser.add_argument(
        "--scope",
        help="Scope filter",
    )
    explain_parser.add_argument(
        "--module",
        help="Module filter",
    )
    explain_parser.add_argument(
        "--capability",
        help="Capability filter",
    )
    explain_parser.add_argument(
        "--task-type",
        help="Task type for ranking boost (not a hard filter)",
    )

    # init
    init_parser = subparsers.add_parser(
        "init",
        help="Bootstrap the shared-knowledge workspace (directories, starters, B1, gitignore, index, hook)",
    )
    init_parser.add_argument(
        "--skip-hook",
        action="store_true",
        help="Skip harness detection and hook adapter installation",
    )
    init_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without making any changes",
    )
    init_parser.add_argument(
        "--hook-scope",
        choices=("workspace", "global"),
        default="workspace",
        help="Pi hook install scope: workspace-local by default; use global to write under ~/.pi",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    root = find_workspace_root(
        Path(args.root),
        allow_fallback=args.allow_root_fallback or args.command == "init",
    )

    if args.command == "rebuild-index":
        return cmd_rebuild_index(root)
    elif args.command == "list":
        return cmd_list(root, args)
    elif args.command == "search":
        return cmd_search(root, args)
    elif args.command == "resolve":
        return cmd_resolve(root, args)
    elif args.command == "inject":
        return cmd_inject(root, args)
    elif args.command == "explain":
        return cmd_explain(root, args)
    elif args.command == "init":
        return cmd_init(root, args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
