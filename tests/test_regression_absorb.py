"""Regression tests confirming existing absorb/lint behavior is preserved."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pytest

import knowledge_absorb as ka

from tests.conftest import _write_inbox_candidate, _write_curated_entry

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _run_absorb(workspace: Path, *args: str):
    result = subprocess.run(
        [sys.executable, "-B", str(SCRIPTS_DIR / "knowledge_absorb.py"), "--root", str(workspace), *args],
        cwd=workspace, capture_output=True, text=True, timeout=60,
    )
    return result.returncode, result.stdout, result.stderr


def _run_lint(workspace: Path, *args: str):
    result = subprocess.run(
        [sys.executable, "-B", str(SCRIPTS_DIR / "knowledge_lint.py"), "--root", str(workspace), *args],
        cwd=workspace, capture_output=True, text=True, timeout=60,
    )
    return result.returncode, result.stdout, result.stderr


# ---------------------------------------------------------------------------
# Absorb regression tests
# ---------------------------------------------------------------------------

class TestAbsorbRegression:
    """Regression tests for knowledge_absorb.py existing behaviors."""

    def test_pressure_reports_correct_counts(self, workspace):
        """Pressure subcommand reports correct inbox and workspace counts."""
        _write_inbox_candidate(workspace, "a.md")
        _write_inbox_candidate(workspace, "b.md")

        pressure = ka.compute_pressure(workspace, ka.Thresholds(
            inbox_max_age_days=14, inbox_max_count=10, workspace_max_count=20
        ))
        assert pressure.metrics["inboxCount"] == 2

    def test_pressure_triggers_on_inbox_count(self, workspace):
        """Pressure triggers when inbox count exceeds threshold."""
        for i in range(5):
            _write_inbox_candidate(workspace, f"entry{i}.md")

        pressure = ka.compute_pressure(workspace, ka.Thresholds(
            inbox_max_age_days=14, inbox_max_count=2, workspace_max_count=20
        ))
        assert pressure.triggered is True
        assert any("inbox_count" in r for r in pressure.reasons)

    def test_find_workspace_root_finds_agents_md(self, workspace):
        """find_workspace_root locates workspace via AGENTS.md."""
        root = ka.find_workspace_root(workspace)
        assert (root / "AGENTS.md").exists()

    def test_classify_candidate_retain_memory(self, workspace):
        """Inbox candidate with retain_memory is classified correctly."""
        p = _write_inbox_candidate(
            workspace, "retain.md",
            name="Test", description="A test memory",
            suggested_action="retain_memory",
            suggested_scope="workspace",
        )
        action = ka.classify_candidate(workspace, p)
        assert action.action == "retain_memory"
        assert action.safeToApply is True

    def test_classify_candidate_promote_to_skill(self, workspace):
        """Inbox candidate with promote_to_skill is classified correctly."""
        p = _write_inbox_candidate(
            workspace, "skill.md",
            name="Deploy Script",
            description="A reusable deploy script template for CI/CD.",
            suggested_action="promote_to_skill",
        )
        action = ka.classify_candidate(workspace, p)
        assert action.action == "promote_to_skill"

    def test_retain_memory_creates_curated_entry(self, workspace):
        """retain_memory creates a curated Markdown entry with frontmatter."""
        inbox = _write_inbox_candidate(
            workspace, "keep.md",
            name="Important Fact",
            description="A fact worth keeping.",
            suggested_action="retain_memory",
            suggested_scope="workspace",
        )
        action = ka.classify_candidate(workspace, inbox)
        assert action.safeToApply is True

        action_dict = {
            "candidatePath": action.candidatePath,
            "action": action.action,
            "destination": action.destination,
            "metadata": {
                "suggestedScope": "workspace",
            },
        }
        changed, error = ka.apply_retain_memory(workspace, action_dict)
        assert error is None
        assert len(changed) > 0
        # Verify the curated file exists
        curated = workspace / changed[0]
        assert curated.exists()
        content = curated.read_text(encoding="utf-8")
        assert "Important Fact" in content
        assert "type: feedback" in content

    def test_retain_memory_updates_workspace_index(self, workspace):
        """retain_memory updates MEMORY.md index for workspace scope."""
        inbox = _write_inbox_candidate(
            workspace, "indexed.md",
            name="Indexed Memory",
            description="Should appear in MEMORY.md.",
            suggested_action="retain_memory",
            suggested_scope="workspace",
        )
        action = ka.classify_candidate(workspace, inbox)

        action_dict = {
            "candidatePath": action.candidatePath,
            "action": action.action,
            "destination": action.destination,
            "metadata": {"suggestedScope": "workspace"},
        }
        changed, error = ka.apply_retain_memory(workspace, action_dict)
        assert error is None

        # Check MEMORY.md was updated
        memory_index = workspace / "knowledge" / "facts" / "workspace" / "MEMORY.md"
        content = memory_index.read_text(encoding="utf-8")
        assert "Indexed Memory" in content or "indexed-memory" in content

    def test_plan_generates_valid_structure(self, workspace):
        """Plan generation has correct version and structure."""
        _write_inbox_candidate(workspace, "test.md")
        plan = ka.build_plan(
            workspace,
            ka.Thresholds(inbox_max_age_days=14, inbox_max_count=10, workspace_max_count=20),
            "test",
            False,
        )
        assert plan["version"] == "1"
        assert "generatedAt" in plan
        assert "pressure" in plan
        assert "actions" in plan
        assert len(plan["actions"]) == 1

    def test_markdown_plan_render(self, workspace):
        """Markdown plan rendering produces readable output."""
        _write_inbox_candidate(workspace, "test.md")
        plan = ka.build_plan(
            workspace,
            ka.Thresholds(inbox_max_age_days=14, inbox_max_count=10, workspace_max_count=20),
            "test",
            False,
        )
        md = ka.render_markdown_plan(plan)
        assert "# Shared-memory Absorption Plan" in md
        assert "## Actions" in md

    def test_slugify_handles_special_chars(self):
        """slugify normalizes various inputs."""
        assert ka.slugify("Hello World") == "hello-world"
        assert ka.slugify("Test.md") == "test"
        assert ka.slugify("") == "memory"
        assert ka.slugify("a" * 200)  # Long input is truncated

    def test_clean_line_truncates(self):
        """clean_line truncates to max_len."""
        assert len(ka.clean_line("a" * 500, 180)) <= 180
        assert ka.clean_line(None) == ""

    def test_today_returns_iso_date(self):
        """today() returns ISO date string."""
        d = ka.today()
        assert len(d) == 10
        assert "-" in d

    def test_now_iso_returns_utc(self):
        """now_iso() returns UTC ISO datetime."""
        ts = ka.now_iso()
        assert "T" in ts
        assert ts.endswith("Z")

    def test_env_int_with_default(self, monkeypatch):
        """env_int returns default when env var is not set."""
        monkeypatch.delenv("NONEXISTENT_VAR", raising=False)
        assert ka.env_int("NONEXISTENT_VAR", 42) == 42

    def test_env_int_parses_number(self, monkeypatch):
        """env_int parses env var to int."""
        monkeypatch.setenv("TEST_VAR", "77")
        assert ka.env_int("TEST_VAR", 0) == 77


# ---------------------------------------------------------------------------
# Absorb CLI regression tests
# ---------------------------------------------------------------------------

class TestAbsorbCLIRegression:
    """CLI-based regression tests for knowledge_absorb.py."""

    def test_pressure_cli(self, workspace):
        """pressure --format json works via CLI."""
        rc, stdout, stderr = _run_absorb(workspace, "pressure", "--format", "json")
        assert rc == 0
        data = json.loads(stdout)
        assert "metrics" in data

    def test_plan_cli_json(self, workspace):
        """plan --format json works via CLI."""
        _write_inbox_candidate(workspace, "test.md")
        rc, stdout, stderr = _run_absorb(
            workspace, "plan", "--format", "json", "--trigger", "test"
        )
        assert rc == 0
        data = json.loads(stdout)
        assert len(data["actions"]) >= 1

    def test_plan_cli_text(self, workspace):
        """plan --format text works via CLI."""
        _write_inbox_candidate(workspace, "test.md")
        rc, stdout, stderr = _run_absorb(
            workspace, "plan", "--format", "text", "--trigger", "test"
        )
        assert rc == 0
        assert "# Shared-memory Absorption Plan" in stdout

    def test_report_cli(self, workspace):
        """report subcommand works via CLI (alias for plan --format text)."""
        _write_inbox_candidate(workspace, "test.md")
        rc, stdout, stderr = _run_absorb(
            workspace, "report", "--trigger", "test"
        )
        assert rc == 0
        assert "# Shared-memory Absorption Plan" in stdout

    def test_apply_cli_safe_only(self, workspace):
        """apply --safe-only works via CLI."""
        _write_inbox_candidate(
            workspace, "keep.md",
            name="Keep Me", suggested_action="retain_memory",
            suggested_scope="workspace",
        )
        rc, stdout, stderr = _run_absorb(
            workspace, "apply", "--safe-only", "--format", "json", "--trigger", "test"
        )
        assert rc == 0
        data = json.loads(stdout)
        assert "changedPaths" in data

    def test_hook_cli(self, workspace):
        """hook subcommand works via CLI."""
        rc, stdout, stderr = _run_absorb(
            workspace, "hook", "--format", "json"
        )
        assert rc == 0
        data = json.loads(stdout)
        assert "pressure" in data
        assert "triggered" in data

    def test_missing_command(self, workspace):
        """Missing command exits with non-zero."""
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "knowledge_absorb.py"), "--root", str(workspace)],
            cwd=workspace, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# Lint regression tests
# ---------------------------------------------------------------------------

class TestLintRegression:
    """Regression tests for knowledge_lint.py existing behavior."""

    def test_lint_no_files_no_issues(self, workspace):
        """Lint with no followup files produces no errors."""
        rc, stdout, stderr = _run_lint(workspace, "--format", "json")
        assert rc == 0
        data = json.loads(stdout)
        assert data["errorCount"] == 0
        assert data["warningCount"] == 0
        assert data["followupFilesChecked"] == 0

    def test_lint_valid_artifact_passes(self, workspace):
        """Lint on a valid followup artifact produces no errors when sourceCandidate exists."""
        from tests.conftest import _write_followup_artifact

        # The default followup artifact references knowledge/shared-memory/inbox/test.md
        # We must create this inbox candidate for the lint to pass
        _write_inbox_candidate(workspace, "test.md", name="Test Memory")
        _write_followup_artifact(workspace, "skill_followup", "test-001.json")

        rc, stdout, stderr = _run_lint(workspace, "--format", "json")
        assert rc == 0, f"Lint failed: stdout={stdout} stderr={stderr}"
        data = json.loads(stdout)
        assert data["errorCount"] == 0

    def test_lint_invalid_json_reports_error(self, workspace):
        """Lint on invalid JSON reports an error."""
        fups_dir = workspace / "knowledge" / "followups" / "skill"
        fups_dir.mkdir(parents=True, exist_ok=True)
        (fups_dir / "bad.json").write_text("this is not json", encoding="utf-8")

        rc, stdout, stderr = _run_lint(workspace, "--format", "json")
        assert rc == 1  # Should exit non-zero for errors
        data = json.loads(stdout)
        assert data["errorCount"] >= 1

    def test_lint_text_output(self, workspace):
        """Lint --format text produces readable output."""
        rc, stdout, stderr = _run_lint(workspace, "--format", "text")
        assert "Shared Memory Lint Report" in stdout

    def test_lint_followup_max_age_env(self, workspace, monkeypatch):
        """Lint respects --followup-max-age-days flag."""
        from tests.conftest import _write_followup_artifact

        # Make followup 60 days old
        import datetime as dt
        old_date = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=60)).isoformat() + "Z"

        _write_followup_artifact(workspace, "skill_followup", "old.json", data={
            "version": "1",
            "kind": "skill_followup",
            "status": "open",
            "createdAt": old_date,
            "sourceCandidate": "knowledge/inbox/test.md",
            "sourceAction": "promote_to_skill",
            "suggestedDestination": "agent-workspace/skills/test/",
            "title": "Old Followup",
            "reason": "Aging test.",
            "evidence": ["Evidence 1"],
            "confidence": 0.7,
            "safeToAutoApply": False,
            "handoffTo": "skill-creator",
        })

        # With 90 day threshold, no aging warning
        rc, stdout, stderr = _run_lint(
            workspace, "--format", "json", "--followup-max-age-days", "90"
        )
        data90 = json.loads(stdout)

        # Check for aging warnings
        aging_warnings_90 = [w for w in data90.get("warnings", []) if w["code"] == "followup-aging"]
        # With 90 day threshold, should be no aging warning since it's only 60 days old
        assert len(aging_warnings_90) == 0

        # With 30 day threshold (default), aging warning
        rc, stdout, stderr = _run_lint(
            workspace, "--format", "json", "--followup-max-age-days", "30"
        )
        data30 = json.loads(stdout)
        aging_warnings_30 = [w for w in data30.get("warnings", []) if w["code"] == "followup-aging"]
        assert len(aging_warnings_30) >= 1
