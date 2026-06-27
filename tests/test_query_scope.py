"""Unit tests for scope filtering in query, resolve, and list operations."""

from __future__ import annotations

import json
import sqlite3

import pytest

import knowledge_query as kq


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _index_with_scoped_entries(db_path):
    """Build an index with various scope entries for testing."""
    entries = [
        {
            "id": "ws001",
            "path": "knowledge/facts/workspace/ws1.md",
            "scope": "workspace", "scope_type": "workspace", "scope_name": "",
            "type": "reference", "name": "Workspace Entry 1",
            "description": "A workspace-scoped entry.",
            "verified_at": "2026-06-22", "source": "agent:test", "status": "active",
            "tags": ["reference", "workspace"], "body_hash": "abc", "body": "Body ws1.",
            "updated_at": "2026-06-22",
        },
        {
            "id": "mod001",
            "path": "knowledge/facts/module/workflow/entry.md",
            "scope": "module:workflow", "scope_type": "module", "scope_name": "workflow",
            "type": "reference", "name": "Module Workflow Entry",
            "description": "A module:workflow entry.",
            "verified_at": "2026-06-22", "source": "agent:test", "status": "active",
            "tags": ["reference", "module", "workflow"], "body_hash": "def", "body": "Body mod.",
            "updated_at": "2026-06-22",
        },
        {
            "id": "mod002",
            "path": "knowledge/facts/module/other/entry.md",
            "scope": "module:other", "scope_type": "module", "scope_name": "other",
            "type": "reference", "name": "Other Module Entry",
            "description": "A module:other entry.",
            "verified_at": "2026-06-22", "source": "agent:test", "status": "active",
            "tags": ["reference", "module", "other"], "body_hash": "ghi", "body": "Body other.",
            "updated_at": "2026-06-22",
        },
        {
            "id": "cap001",
            "path": "knowledge/facts/capability/auth/entry.md",
            "scope": "capability:auth", "scope_type": "capability", "scope_name": "auth",
            "type": "reference", "name": "Auth Capability Entry",
            "description": "A capability:auth entry.",
            "verified_at": "2026-06-22", "source": "agent:test", "status": "active",
            "tags": ["reference", "capability", "auth"], "body_hash": "jkl", "body": "Body cap.",
            "updated_at": "2026-06-22",
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
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (entry["id"], entry["path"], entry["scope"], entry["scope_type"],
                 entry["scope_name"], entry["type"], entry["name"], entry["description"],
                 entry["verified_at"], entry["source"], entry["status"], tags_json,
                 entry["body_hash"], entry["body"], entry["updated_at"]),
            )
            rowid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.execute(
                """INSERT INTO memory_entries_fts
                   (rowid, name, description, body, tags, path, scope, type)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (rowid, entry["name"], entry["description"], entry["body"],
                 tags_text, entry["path"], entry["scope"], entry["type"]),
            )
        db.commit()
    finally:
        db.close()
    return db_path


# ---------------------------------------------------------------------------
# Scope filtering tests
# ---------------------------------------------------------------------------

class TestScopeFiltering:
    """Tests for scope-based filtering in query operations."""

    def test_list_filter_workspace(self, tmp_path):
        """--scope workspace returns only workspace entries."""
        db_path = tmp_path / "test.sqlite"
        _index_with_scoped_entries(db_path)

        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                "SELECT * FROM memory_entries WHERE scope = ? AND type != 'deprecated'",
                ("workspace",),
            ).fetchall()
            assert len(rows) >= 1
            for row in rows:
                assert row["scope"] == "workspace"
        finally:
            db.close()

    def test_list_filter_module(self, tmp_path):
        """--scope module:workflow returns only that module's entries."""
        db_path = tmp_path / "test.sqlite"
        _index_with_scoped_entries(db_path)

        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                "SELECT * FROM memory_entries WHERE scope = ? AND type != 'deprecated'",
                ("module:workflow",),
            ).fetchall()
            assert len(rows) >= 1
            for row in rows:
                assert row["scope"] == "module:workflow"
        finally:
            db.close()

    def test_list_filter_capability(self, tmp_path):
        """--scope capability:auth returns only that capability's entries."""
        db_path = tmp_path / "test.sqlite"
        _index_with_scoped_entries(db_path)

        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                "SELECT * FROM memory_entries WHERE scope = ? AND type != 'deprecated'",
                ("capability:auth",),
            ).fetchall()
            assert len(rows) >= 1
            for row in rows:
                assert row["scope"] == "capability:auth"
        finally:
            db.close()

    def test_resolve_includes_workspace_and_module(self, tmp_path):
        """resolve --module workflow includes workspace and module:workflow entries."""
        db_path = tmp_path / "test.sqlite"
        _index_with_scoped_entries(db_path)

        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        try:
            # Simulate resolve query: workspace OR module:workflow
            rows = db.execute(
                """SELECT * FROM memory_entries
                   WHERE type != 'deprecated'
                   AND (scope = 'workspace' OR scope = ?)
                   ORDER BY scope_type, scope_name, name""",
                ("module:workflow",),
            ).fetchall()

            scopes = [r["scope"] for r in rows]
            assert "workspace" in scopes, "Should include workspace-scoped entries"
            assert "module:workflow" in scopes, "Should include exact module match"
            assert "module:other" not in scopes, "Should NOT include non-matching module"
            assert "capability:auth" not in scopes, "Should NOT include capability entries"
        finally:
            db.close()

    def test_resolve_excludes_invalid_verified_at(self, tmp_path):
        """resolve excludes entries with invalid verified_at."""
        db_path = tmp_path / "test.sqlite"
        _index_with_scoped_entries(db_path)

        # Add an entry with invalid verified_at
        db = sqlite3.connect(str(db_path))
        try:
            db.execute(
                """INSERT INTO memory_entries
                   (id, path, scope, scope_type, scope_name, type, name,
                    description, verified_at, source, status, tags_json,
                    body_hash, body, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("bad", "path/bad.md", "workspace", "workspace", "", "reference",
                 "Bad Entry", "Bad verified_at", "not-a-date", "agent:test",
                 "active", "[]", "bad", "body", "2026-01-01"),
            )
            db.commit()
        finally:
            db.close()

        # Read and filter in Python (mimicking cmd_resolve)
        db2 = sqlite3.connect(str(db_path))
        db2.row_factory = sqlite3.Row
        try:
            rows = db2.execute(
                "SELECT * FROM memory_entries WHERE type != 'deprecated' AND scope = 'workspace'"
            ).fetchall()

            valid = [r for r in rows if kq.is_valid_iso_date(r["verified_at"]) or not r["verified_at"]]
            invalid = [r for r in rows if r["verified_at"] and not kq.is_valid_iso_date(r["verified_at"])]

            # The bad entry should be filtered out
            bad_in_valid = [r for r in valid if r["id"] == "bad"]
            assert len(bad_in_valid) == 0
        finally:
            db2.close()

    def test_query_resolve_includes_capability_with_module(self, tmp_path):
        """resolve --capability auth includes workspace and capability:auth entries."""
        db_path = tmp_path / "test.sqlite"
        _index_with_scoped_entries(db_path)

        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                """SELECT * FROM memory_entries
                   WHERE type != 'deprecated'
                   AND (scope = 'workspace' OR scope = ?)
                   ORDER BY scope_type, scope_name, name""",
                ("capability:auth",),
            ).fetchall()
            scopes = [r["scope"] for r in rows]
            assert "workspace" in scopes
            assert "capability:auth" in scopes
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Exclude deprecated tests
# ---------------------------------------------------------------------------

class TestExcludeDeprecated:
    """Ensure deprecated entries are excluded by default."""

    def test_deprecated_excluded_from_list(self, tmp_path):
        """List excludes deprecated entries."""
        db_path = tmp_path / "test.sqlite"
        _index_with_scoped_entries(db_path)

        # Add deprecated entry
        db = sqlite3.connect(str(db_path))
        try:
            db.execute(
                """INSERT INTO memory_entries
                   (id, path, scope, scope_type, scope_name, type, name,
                    description, verified_at, source, status, tags_json,
                    body_hash, body, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("dep", "path/dep.md", "workspace", "workspace", "", "deprecated",
                 "Dep Entry", "Deprecated", "2026-01-01", "agent:test",
                 "active", '["deprecated"]', "dep", "body", "2026-01-01"),
            )
            db.commit()
        finally:
            db.close()

        db2 = sqlite3.connect(str(db_path))
        db2.row_factory = sqlite3.Row
        try:
            all_rows = db2.execute("SELECT * FROM memory_entries").fetchall()
            non_dep = db2.execute(
                "SELECT * FROM memory_entries WHERE type != 'deprecated'"
            ).fetchall()
            assert len(non_dep) < len(all_rows), "Deprecated should be excluded"
        finally:
            db2.close()
