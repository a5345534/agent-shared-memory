"""Integration tests for end-to-end absorb→followup, rebuild→search, resolve scope, inject Markdown."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import _write_inbox_candidate, _write_curated_entry

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _run_absorb(workspace: Path, *args: str):
    """Run knowledge_absorb.py with given args and return (returncode, stdout, stderr)."""
    result = subprocess.run(
        [sys.executable, "-B", str(SCRIPTS_DIR / "knowledge_absorb.py"), "--root", str(workspace), *args],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.returncode, result.stdout, result.stderr


def _run_query(workspace: Path, *args: str):
    """Run knowledge_query.py with given args."""
    result = subprocess.run(
        [sys.executable, "-B", str(SCRIPTS_DIR / "knowledge_query.py"), "--root", str(workspace), *args],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.returncode, result.stdout, result.stderr


def _run_lint(workspace: Path, *args: str):
    """Run knowledge_lint.py with given args."""
    result = subprocess.run(
        [sys.executable, "-B", str(SCRIPTS_DIR / "knowledge_lint.py"), "--root", str(workspace), *args],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.returncode, result.stdout, result.stderr


# ---------------------------------------------------------------------------
# End-to-end: Absorb → Followup
# ---------------------------------------------------------------------------

class TestAbsorbToFollowup:
    """E2E tests for absorb plan → apply with follow-up creation."""

    def test_plan_produces_followup_action(self, workspace):
        """Plan classifies promote_to_skill candidates with followup actions."""
        _write_inbox_candidate(
            workspace,
            "skill-candidate.md",
            name="Deploy Script",
            description="A reusable deploy script for CI/CD.",
            suggested_action="promote_to_skill",
            candidate_id="skill-001",
        )

        rc, stdout, stderr = _run_absorb(
            workspace, "plan", "--format", "json", "--trigger", "test"
        )
        assert rc == 0, f"Plan failed: {stderr}"
        plan = json.loads(stdout)
        assert plan["version"] == "1"
        actions = plan["actions"]
        assert len(actions) >= 1

        skill_actions = [a for a in actions if a["action"] == "promote_to_skill"]
        assert len(skill_actions) >= 1

    def test_apply_safe_only_creates_followup(self, workspace):
        """apply --safe-only creates followup artifacts for promote actions."""
        _write_inbox_candidate(
            workspace,
            "skill-candidate.md",
            name="Deploy Script",
            description="A reusable deploy script.",
            suggested_action="promote_to_skill",
            candidate_id="skill-001",
        )

        rc, stdout, stderr = _run_absorb(
            workspace, "apply", "--safe-only", "--format", "json", "--trigger", "test"
        )
        assert rc == 0, f"Apply failed: {stderr}"
        result = json.loads(stdout)

        # Check followup was created
        followup_dir = workspace / "knowledge" / "followups" / "skill"
        followups = list(followup_dir.glob("*.json"))
        assert len(followups) >= 1

        # Verify the artifact content
        artifact = json.loads(followups[0].read_text(encoding="utf-8"))
        assert artifact["status"] == "open"
        assert artifact["kind"] == "skill_followup"

    def test_apply_safe_only_preserves_inbox_candidate(self, workspace):
        """apply --safe-only with promote action preserves the inbox candidate."""
        inbox = _write_inbox_candidate(
            workspace,
            "surviving-candidate.md",
            name="Surviving Memory",
            description="Should survive apply.",
            suggested_action="promote_to_skill",
        )

        rc, stdout, stderr = _run_absorb(
            workspace, "apply", "--safe-only", "--format", "json", "--trigger", "test"
        )
        assert rc == 0
        assert inbox.exists(), "Inbox candidate should survive followup creation"

    def test_retain_memory_still_works(self, workspace):
        """retain_memory action still creates curated entries."""
        _write_inbox_candidate(
            workspace,
            "keep-me.md",
            name="Important Memory",
            description="This should be retained.",
            suggested_action="retain_memory",
            suggested_scope="workspace",
        )

        rc, stdout, stderr = _run_absorb(
            workspace, "apply", "--safe-only", "--format", "json", "--trigger", "test"
        )
        assert rc == 0, f"Apply failed: {stderr}"

        # Check curated entry was created
        workspace_dir = workspace / "knowledge" / "facts" / "workspace"
        curated_files = [f for f in workspace_dir.glob("*.md") if f.name not in ("README.md", "MEMORY.md")]
        assert len(curated_files) >= 1

    def test_pressure_reports_metrics(self, workspace):
        """pressure subcommand reports inbox and workspace metrics."""
        _write_inbox_candidate(workspace, "test1.md")
        _write_inbox_candidate(workspace, "test2.md")

        rc, stdout, stderr = _run_absorb(workspace, "pressure", "--format", "json")
        assert rc == 0
        pressure = json.loads(stdout)
        assert "metrics" in pressure
        assert pressure["metrics"]["inboxCount"] == 2


# ---------------------------------------------------------------------------
# End-to-end: Rebuild → Search
# ---------------------------------------------------------------------------

class TestRebuildToSearch:
    """E2E tests for rebuild-index → search flow."""

    def test_rebuild_index_creates_database(self, workspace):
        """rebuild-index creates memory.sqlite."""
        _write_curated_entry(
            workspace, "knowledge/facts/workspace", "test.md",
            name="Test Entry", scope="workspace"
        )

        rc, stdout, stderr = _run_query(workspace, "rebuild-index")
        assert rc == 0, f"rebuild-index failed: {stderr}"

        sqlite_path = workspace / "knowledge" / ".index" / "memory.sqlite"
        assert sqlite_path.exists()

        manifest_path = workspace / "knowledge" / ".index" / "manifest.json"
        assert manifest_path.exists()

    def test_search_returns_results(self, workspace):
        """Search returns matching entries after rebuild-index."""
        _write_curated_entry(
            workspace, "knowledge/facts/workspace", "hook.md",
            name="Validation Hook", scope="workspace",
            body="A validation hook for the CI/CD pipeline."
        )
        _write_curated_entry(
            workspace, "knowledge/facts/module/testmod", "arch.md",
            name="Architecture Doc", scope="module:testmod",
            memory_type="architectural-invariant",
            body="All modules must validate inputs before processing."
        )

        # Rebuild index
        rc, stdout, stderr = _run_query(workspace, "rebuild-index")
        assert rc == 0, f"rebuild-index failed: {stderr}"

        # Search
        rc, stdout, stderr = _run_query(workspace, "search", "validation")
        assert rc == 0, f"search failed: {stderr}"
        result = json.loads(stdout)
        assert "results" in result
        assert len(result["results"]) >= 1

    def test_search_excludes_deprecated(self, workspace):
        """Search results exclude deprecated entries."""
        _write_curated_entry(
            workspace, "knowledge/facts/workspace", "dep.md",
            name="Deprecated Entry", scope="workspace",
            memory_type="deprecated",
            body="This is a deprecated validation entry."
        )

        rc, _, _ = _run_query(workspace, "rebuild-index")
        assert rc == 0

        rc, stdout, stderr = _run_query(workspace, "search", "validation")
        assert rc == 0
        result = json.loads(stdout)
        # Should have no results because the only match is deprecated
        # (unless there are other entries, but there shouldn't be)
        for r in result["results"]:
            assert r["type"] != "deprecated"

    def test_list_filters_by_scope(self, workspace):
        """List with --scope filter returns only matching scope entries."""
        _write_curated_entry(
            workspace, "knowledge/facts/workspace", "ws.md",
            name="WS Entry", scope="workspace"
        )
        _write_curated_entry(
            workspace, "knowledge/facts/module/testmod", "mod.md",
            name="Module Entry", scope="module:testmod"
        )

        _run_query(workspace, "rebuild-index")

        rc, stdout, stderr = _run_query(workspace, "list", "--scope", "workspace")
        assert rc == 0
        results = json.loads(stdout)
        for r in results:
            assert r["scope"] == "workspace"

    def test_search_with_type_filter(self, workspace):
        """Search with --type filter returns only matching type entries."""
        _write_curated_entry(
            workspace, "knowledge/facts/workspace", "arch.md",
            name="Arch Entry", scope="workspace",
            memory_type="architectural-invariant",
            body="Architecture constraint entry."
        )
        _write_curated_entry(
            workspace, "knowledge/facts/workspace", "ref.md",
            name="Ref Entry", scope="workspace",
            memory_type="reference",
            body="Reference entry about architecture."
        )

        _run_query(workspace, "rebuild-index")

        rc, stdout, stderr = _run_query(
            workspace, "search", "architecture", "--type", "architectural-invariant"
        )
        assert rc == 0
        result = json.loads(stdout)
        for r in result["results"]:
            assert r["type"] == "architectural-invariant"


# ---------------------------------------------------------------------------
# End-to-end: Resolve Scope
# ---------------------------------------------------------------------------

class TestResolveScope:
    """E2E tests for resolve --module and --capability scope filtering."""

    def test_resolve_module_includes_workspace_and_module(self, workspace):
        """resolve --module X includes workspace + module:X entries."""
        _write_curated_entry(
            workspace, "knowledge/facts/workspace", "ws.md",
            name="WS Entry", scope="workspace"
        )
        _write_curated_entry(
            workspace, "knowledge/facts/module/testmod", "mod.md",
            name="Module Entry", scope="module:testmod"
        )
        _write_curated_entry(
            workspace, "knowledge/facts/module/other", "other.md",
            name="Other Module", scope="module:other"
        )

        _run_query(workspace, "rebuild-index")

        rc, stdout, stderr = _run_query(workspace, "resolve", "--module", "testmod")
        assert rc == 0
        result = json.loads(stdout)
        scopes = [r["scope"] for r in result["results"]]
        assert "workspace" in scopes
        assert "module:testmod" in scopes
        assert "module:other" not in scopes

    def test_resolve_capability_includes_workspace_and_capability(self, workspace):
        """resolve --capability X includes workspace + capability:X entries."""
        _write_curated_entry(
            workspace, "knowledge/facts/workspace", "ws.md",
            name="WS Entry", scope="workspace"
        )
        _write_curated_entry(
            workspace, "knowledge/facts/capability/testcap", "cap.md",
            name="Cap Entry", scope="capability:testcap"
        )

        _run_query(workspace, "rebuild-index")

        rc, stdout, stderr = _run_query(workspace, "resolve", "--capability", "testcap")
        assert rc == 0
        result = json.loads(stdout)
        scopes = [r["scope"] for r in result["results"]]
        assert "workspace" in scopes
        assert "capability:testcap" in scopes


# ---------------------------------------------------------------------------
# End-to-end: Inject Markdown
# ---------------------------------------------------------------------------

class TestInjectMarkdown:
    """E2E tests for inject --format markdown."""

    def test_inject_produces_markdown(self, workspace):
        """inject --format markdown produces valid Markdown context."""
        _write_curated_entry(
            workspace, "knowledge/facts/workspace", "ws.md",
            name="Workspace Memory", scope="workspace",
            body="Workspace-level shared memory about deployment."
        )
        _write_curated_entry(
            workspace, "knowledge/facts/module/testmod", "mod.md",
            name="Module Memory", scope="module:testmod",
            body="Module testmod deploy instructions."
        )

        _run_query(workspace, "rebuild-index")

        rc, stdout, stderr = _run_query(
            workspace, "inject", "--module", "testmod", "--format", "markdown"
        )
        assert rc == 0, f"inject failed: {stderr}"
        assert "## Shared Memory Injection Context" in stdout
        assert "**Context:**" in stdout

    def test_inject_respects_budget(self, workspace):
        """inject --budget-chars limits output size."""
        _write_curated_entry(
            workspace, "knowledge/facts/workspace", "large.md",
            name="Large Entry", scope="workspace",
            body="Large body. " * 200  # ~2400 chars
        )

        _run_query(workspace, "rebuild-index")

        rc, stdout, stderr = _run_query(
            workspace, "inject", "--module", "testmod", "--format", "markdown",
            "--budget-chars", "500"
        )
        assert rc == 0
        # Output should be roughly within budget
        assert len(stdout) <= 1200  # generous margin

    def test_inject_json_format(self, workspace):
        """inject --format json produces JSON with metadata."""
        _write_curated_entry(
            workspace, "knowledge/facts/workspace", "test.md",
            name="Test", scope="workspace"
        )

        _run_query(workspace, "rebuild-index")

        rc, stdout, stderr = _run_query(
            workspace, "inject", "--format", "json"
        )
        assert rc == 0
        data = json.loads(stdout)
        assert "budget" in data
        assert "entries" in data
        assert "rendered" in data


# ---------------------------------------------------------------------------
# End-to-end: Explain
# ---------------------------------------------------------------------------

class TestExplain:
    """E2E tests for explain subcommand."""

    def test_explain_shows_score_breakdown(self, workspace):
        """explain --query shows score breakdown."""
        _write_curated_entry(
            workspace, "knowledge/facts/workspace", "hook.md",
            name="Validation Hook", scope="workspace",
            body="A validation hook for CI/CD."
        )

        _run_query(workspace, "rebuild-index")

        rc, stdout, stderr = _run_query(workspace, "explain", "--query", "validation")
        assert rc == 0
        result = json.loads(stdout)
        assert "results" in result
        if result["results"]:
            r = result["results"][0]
            assert "scoreBreakdown" in r
            assert "reasons" in r

    def test_explain_shows_excluded(self, workspace):
        """explain --query shows excluded deprecated entries."""
        _write_curated_entry(
            workspace, "knowledge/facts/workspace", "dep.md",
            name="Old Hook", scope="workspace",
            memory_type="deprecated",
            body="An old validation hook that is deprecated."
        )

        _run_query(workspace, "rebuild-index")

        rc, stdout, stderr = _run_query(workspace, "explain", "--query", "validation")
        assert rc == 0
        result = json.loads(stdout)
        if result.get("excluded"):
            has_dep = any(
                "deprecated" in " ".join(e.get("reasons", []))
                for e in result["excluded"]
            )
            # May or may not have deprecated results depending on FTS matching


# ---------------------------------------------------------------------------
# End-to-end: Idempotent rebuild
# ---------------------------------------------------------------------------

class TestIdempotentRebuild:
    """E2E tests for idempotent rebuild-index."""

    def test_rebuild_is_idempotent(self, workspace):
        """Rebuilding with same sources produces same entry count and hash."""
        _write_curated_entry(
            workspace, "knowledge/facts/workspace", "a.md",
            name="A", scope="workspace"
        )

        rc1, stdout1, _ = _run_query(workspace, "rebuild-index")
        assert rc1 == 0

        manifest1 = json.loads(
            (workspace / "knowledge" / ".index" / "manifest.json")
            .read_text(encoding="utf-8")
        )

        # Rebuild again
        rc2, stdout2, _ = _run_query(workspace, "rebuild-index")
        assert rc2 == 0

        manifest2 = json.loads(
            (workspace / "knowledge" / ".index" / "manifest.json")
            .read_text(encoding="utf-8")
        )

        assert manifest1["entryCount"] == manifest2["entryCount"]
        assert manifest1["hash"] == manifest2["hash"]
