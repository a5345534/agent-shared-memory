"""Unit tests for SQLite FTS5 search functionality."""

from __future__ import annotations

import json
import sqlite3

import pytest

import knowledge_query as kq


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_minimal_index(db_path, entries=None):
    """Build a minimal SQLite FTS5 index for testing."""
    if entries is None:
        entries = [
            {
                "id": "a1b2c3d4e5f6a7b8",
                "path": "knowledge/facts/workspace/test.md",
                "scope": "workspace",
                "scope_type": "workspace",
                "scope_name": "",
                "type": "reference",
                "name": "System Validation Hook",
                "description": "A validation hook for CI/CD pipeline.",
                "verified_at": "2026-06-22",
                "source": "agent:test",
                "status": "active",
                "tags": ["reference", "workspace"],
                "body_hash": "abc123",
                "body": "This validation hook ensures that all critical checks pass.",
                "updated_at": "2026-06-22",
            },
            {
                "id": "b2c3d4e5f6a7b8c9",
                "path": "knowledge/facts/module/testmod/arch.md",
                "scope": "module:testmod",
                "scope_type": "module",
                "scope_name": "testmod",
                "type": "architectural-invariant",
                "name": "Test Module Architecture",
                "description": "Architecture invariant for test module.",
                "verified_at": "2026-06-22",
                "source": "agent:test",
                "status": "active",
                "tags": ["architectural-invariant", "module", "testmod"],
                "body_hash": "def456",
                "body": "All inputs to testmod must be validated before processing.",
                "updated_at": "2026-06-22",
            },
            {
                "id": "c3d4e5f6a7b8c9d0",
                "path": "knowledge/facts/workspace/deprecated.md",
                "scope": "workspace",
                "scope_type": "workspace",
                "scope_name": "",
                "type": "deprecated",
                "name": "Deprecated Entry",
                "description": "A deprecated entry.",
                "verified_at": "2026-01-01",
                "source": "agent:test",
                "status": "active",
                "tags": ["deprecated"],
                "body_hash": "ghi789",
                "body": "This entry is outdated and should not appear in default results.",
                "updated_at": "2026-01-01",
            },
        ]

    db = sqlite3.connect(str(db_path))
    try:
        db.execute(kq.CREATE_MEMORY_ENTRIES_SQL)
        db.execute(kq.CREATE_FTS_SQL)

        for entry in entries:
            tags_json = json.dumps(entry["tags"], ensure_ascii=False)
            tags_text = " ".join(entry["tags"])
            db.execute(
                """INSERT INTO memory_entries
                   (id, path, scope, scope_type, scope_name, type, name,
                    description, verified_at, source, status, tags_json,
                    body_hash, body, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry["id"], entry["path"], entry["scope"],
                    entry["scope_type"], entry["scope_name"], entry["type"],
                    entry["name"], entry["description"], entry["verified_at"],
                    entry["source"], entry["status"], tags_json,
                    entry["body_hash"], entry["body"], entry["updated_at"],
                ),
            )
            rowid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.execute(
                """INSERT INTO memory_entries_fts
                   (rowid, name, description, body, tags, path, scope, type)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (rowid, entry["name"], entry["description"], entry["body"],
                 tags_text, entry["path"], entry["scope"], entry["type"]),
            )
        db.commit()
    finally:
        db.close()
    return db_path


# ---------------------------------------------------------------------------
# FTS5 Search Tests
# ---------------------------------------------------------------------------

class TestFTSSearch:
    """Unit tests for FTS5 search queries."""

    def test_search_matches_body_text(self, tmp_path):
        """Search for text in body returns matching entries."""
        db_path = tmp_path / "test.sqlite"
        _build_minimal_index(db_path)

        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                """SELECT me.*, mefts.rank
                   FROM memory_entries me
                   JOIN memory_entries_fts mefts ON me.rowid = mefts.rowid
                   WHERE memory_entries_fts MATCH ?
                   AND me.type != 'deprecated'""",
                ("validation hook",),
            ).fetchall()
            assert len(rows) >= 1
            # Should contain the System Validation Hook entry
            names = [r["name"] for r in rows]
            assert "System Validation Hook" in names
        finally:
            db.close()

    def test_search_excludes_deprecated(self, tmp_path):
        """Deprecated entries are excluded from search results."""
        db_path = tmp_path / "test.sqlite"
        _build_minimal_index(db_path)

        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                """SELECT me.*, mefts.rank
                   FROM memory_entries me
                   JOIN memory_entries_fts mefts ON me.rowid = mefts.rowid
                   WHERE memory_entries_fts MATCH ?
                   AND me.type != 'deprecated'""",
                ("outdated",),
            ).fetchall()
            # Deprecated entry matches "outdated" but should be excluded
            # Since the deprecated entry is the only one with "outdated",
            # there might be no rows (or only non-deprecated matches)
            deprecated_names = [r["name"] for r in rows if r["type"] == "deprecated"]
            assert len(deprecated_names) == 0
        finally:
            db.close()

    def test_search_matches_name_field(self, tmp_path):
        """Search text in name field returns matching entries."""
        db_path = tmp_path / "test.sqlite"
        _build_minimal_index(db_path)

        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                """SELECT me.*, mefts.rank
                   FROM memory_entries me
                   JOIN memory_entries_fts mefts ON me.rowid = mefts.rowid
                   WHERE memory_entries_fts MATCH ?
                   AND me.type != 'deprecated'""",
                ("Architecture",),
            ).fetchall()
            names = [r["name"] for r in rows]
            assert "Test Module Architecture" in names
        finally:
            db.close()

    def test_search_matches_description_field(self, tmp_path):
        """Search text in description field returns matching entries."""
        db_path = tmp_path / "test.sqlite"
        _build_minimal_index(db_path)

        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                """SELECT me.*, mefts.rank
                   FROM memory_entries me
                   JOIN memory_entries_fts mefts ON me.rowid = mefts.rowid
                   WHERE memory_entries_fts MATCH ?
                   AND me.type != 'deprecated'""",
                ("pipeline",),
            ).fetchall()
            # Should match the description of validation hook
            names = [r["name"] for r in rows]
            assert "System Validation Hook" in names
        finally:
            db.close()

    def test_search_with_scope_filter(self, tmp_path):
        """Search with a scope filter returns only matching scope entries."""
        db_path = tmp_path / "test.sqlite"
        _build_minimal_index(db_path)

        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                """SELECT me.*, mefts.rank
                   FROM memory_entries me
                   JOIN memory_entries_fts mefts ON me.rowid = mefts.rowid
                   WHERE memory_entries_fts MATCH ?
                   AND me.type != 'deprecated'
                   AND me.scope = 'workspace'""",
                ("process",),
            ).fetchall()
            # The architecture entry says "processing" but is module:testmod
            # workspace entries should still match if they contain "process"
            for row in rows:
                assert row["scope"] == "workspace"
        finally:
            db.close()

    def test_search_with_type_filter(self, tmp_path):
        """Search with type filter returns only matching type entries."""
        db_path = tmp_path / "test.sqlite"
        _build_minimal_index(db_path)

        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                """SELECT me.*, mefts.rank
                   FROM memory_entries me
                   JOIN memory_entries_fts mefts ON me.rowid = mefts.rowid
                   WHERE memory_entries_fts MATCH ?
                   AND me.type != 'deprecated'
                   AND me.type = 'architectural-invariant'""",
                ("architect",),
            ).fetchall()
            for row in rows:
                assert row["type"] == "architectural-invariant"
        finally:
            db.close()

    def test_search_returns_rank(self, tmp_path):
        """FTS5 search returns a rank value for scoring."""
        db_path = tmp_path / "test.sqlite"
        _build_minimal_index(db_path)

        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                """SELECT mefts.rank
                   FROM memory_entries me
                   JOIN memory_entries_fts mefts ON me.rowid = mefts.rowid
                   WHERE memory_entries_fts MATCH ?
                   AND me.type != 'deprecated'""",
                ("validation",),
            ).fetchall()
            for row in rows:
                assert row["rank"] is not None
                assert row["rank"] < 0  # FTS5 BM25 ranks are negative
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Rebuild-index tests
# ---------------------------------------------------------------------------

class TestRebuildIndex:
    """Unit tests for the rebuild-index pipeline (direct function test)."""

    def test_build_index_creates_sqlite_file(self, workspace):
        """build_index creates a memory.sqlite file."""
        from tests.conftest import _write_curated_entry

        _write_curated_entry(
            workspace,
            "knowledge/facts/workspace",
            "entry.md",
            name="Test Entry",
            scope="workspace",
        )

        sqlite_path, count, content_hash = kq.build_index(workspace)
        assert sqlite_path.exists()
        assert count == 1
        assert len(content_hash) == 12

    def test_collect_curated_entries_finds_files(self, workspace):
        """collect_curated_entries finds curated Markdown files."""
        from tests.conftest import _write_curated_entry

        _write_curated_entry(
            workspace,
            "knowledge/facts/workspace",
            "entry1.md",
            name="Entry 1",
            scope="workspace",
        )
        _write_curated_entry(
            workspace,
            "knowledge/facts/module/testmod",
            "entry2.md",
            name="Entry 2",
            scope="module:testmod",
        )

        entries = kq.collect_curated_entries(workspace)
        assert len(entries) == 2

    def test_collect_curated_skips_inbox(self, workspace):
        """Inbox files are not collected as curated entries."""
        from tests.conftest import _write_inbox_candidate

        _write_inbox_candidate(workspace, "not-curated.md")
        entries = kq.collect_curated_entries(workspace)
        inbox_paths = [e.path for e in entries if "inbox" in e.path]
        assert len(inbox_paths) == 0

    def test_collect_curated_skips_followups(self, workspace):
        """Followup files are not collected as curated entries."""
        # Write a Markdown file in followups (unusual but possible)
        fups = workspace / "knowledge" / "followups" / "test.md"
        fups.parent.mkdir(parents=True, exist_ok=True)
        fups.write_text("---\nname: Test\n---\nBody.\n", encoding="utf-8")

        entries = kq.collect_curated_entries(workspace)
        followup_paths = [e.path for e in entries if "followups" in e.path]
        assert len(followup_paths) == 0

    def test_build_index_skips_README_and_MEMORY(self, workspace):
        """README.md and MEMORY.md are not indexed."""
        from tests.conftest import _write_curated_entry

        # Create a README.md in workspace
        (workspace / "knowledge" / "facts" / "workspace" / "README.md").write_text(
            "---\nname: Readme\n---\nShould not be indexed.\n", encoding="utf-8"
        )
        _write_curated_entry(
            workspace, "knowledge/facts/workspace", "real.md",
            name="Real", scope="workspace"
        )

        entries = kq.collect_curated_entries(workspace)
        readme_entries = [e for e in entries if "README" in e.path]
        assert len(readme_entries) == 0
        assert len(entries) == 1

    def test_capability_check_passes_with_fts5(self):
        """Capability check passes when FTS5 and trigram are available."""
        errors = kq.check_capability()
        assert errors == [], f"Capability check failed: {errors}"


# ---------------------------------------------------------------------------
# Manifest tests
# ---------------------------------------------------------------------------

class TestManifest:
    """Tests for manifest.json generation and content hash."""

    def test_manifest_has_required_fields(self, workspace):
        """Manifest includes version, entryCount, hash, generatedAt."""
        from tests.conftest import _write_curated_entry

        _write_curated_entry(
            workspace, "knowledge/facts/workspace", "e.md",
            name="E", scope="workspace"
        )

        sqlite_path, entry_count, content_hash = kq.build_index(workspace)
        manifest_path = kq.write_manifest(workspace, sqlite_path, entry_count, content_hash)

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["version"] == "1"
        assert manifest["entryCount"] == 1
        assert "hash" in manifest
        assert "generatedAt" in manifest

    def test_content_hash_is_stable(self, workspace):
        """Content hash is stable across builds with same entries."""
        from tests.conftest import _write_curated_entry

        _write_curated_entry(
            workspace, "knowledge/facts/workspace", "e.md",
            name="E", scope="workspace", body="Same body content."
        )

        entries1 = kq.collect_curated_entries(workspace)
        hash1 = kq.compute_content_hash(entries1)

        # Re-read entries (no changes)
        entries2 = kq.collect_curated_entries(workspace)
        hash2 = kq.compute_content_hash(entries2)

        assert hash1 == hash2
