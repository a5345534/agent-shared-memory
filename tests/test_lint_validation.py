"""Lint validation tests for done-without-outputs, aging, missing index, and all followup contract checks."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pytest

import knowledge_lint as kl

from tests.conftest import _write_followup_artifact

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _run_lint(workspace: Path, *args: str):
    result = subprocess.run(
        [sys.executable, "-B", str(SCRIPTS_DIR / "knowledge_lint.py"), "--root", str(workspace), *args],
        cwd=workspace, capture_output=True, text=True, timeout=60,
    )
    return result.returncode, result.stdout, result.stderr


# ---------------------------------------------------------------------------
# Done without outputs
# ---------------------------------------------------------------------------

class TestDoneWithoutOutputs:
    """Tests for followup-done-without-outputs validation."""

    def test_done_with_empty_outputs_reports_error(self, workspace):
        """Status done with empty outputs array triggers error."""
        _write_followup_artifact(workspace, "skill_followup", "done-empty.json", data={
            "version": "1",
            "kind": "skill_followup",
            "status": "done",
            "createdAt": "2026-06-22T10:00:00Z",
            "sourceCandidate": "knowledge/inbox/test.md",
            "sourceAction": "promote_to_skill",
            "suggestedDestination": "agent-workspace/skills/test/",
            "title": "Done with no outputs",
            "reason": "Testing.",
            "evidence": ["Evidence 1"],
            "confidence": 0.8,
            "safeToAutoApply": False,
            "handoffTo": "skill-creator",
            "outputs": [],
        })

        rc, stdout, stderr = _run_lint(workspace, "--format", "json")
        assert rc == 1
        data = json.loads(stdout)
        done_errors = [e for e in data["errors"] if e["code"] == "followup-done-without-outputs"]
        assert len(done_errors) >= 1

    def test_done_with_missing_outputs_reports_error(self, workspace):
        """Status done with no outputs key triggers error."""
        _write_followup_artifact(workspace, "skill_followup", "done-no-key.json", data={
            "version": "1",
            "kind": "skill_followup",
            "status": "done",
            "createdAt": "2026-06-22T10:00:00Z",
            "sourceCandidate": "knowledge/inbox/test.md",
            "sourceAction": "promote_to_skill",
            "suggestedDestination": "agent-workspace/skills/test/",
            "title": "Done with missing outputs key",
            "reason": "Testing.",
            "evidence": ["Evidence 1"],
            "confidence": 0.8,
            "safeToAutoApply": False,
            "handoffTo": "skill-creator",
        })

        rc, stdout, stderr = _run_lint(workspace, "--format", "json")
        assert rc == 1
        data = json.loads(stdout)
        done_errors = [e for e in data["errors"] if e["code"] == "followup-done-without-outputs"]
        assert len(done_errors) >= 1

    def test_done_with_valid_outputs_passes(self, workspace):
        """Status done with valid outputs passes validation."""
        _write_followup_artifact(workspace, "skill_followup", "done-valid.json", data={
            "version": "1",
            "kind": "skill_followup",
            "status": "done",
            "createdAt": "2026-06-22T10:00:00Z",
            "sourceCandidate": "knowledge/inbox/test.md",
            "sourceAction": "promote_to_skill",
            "suggestedDestination": "agent-workspace/skills/test/",
            "title": "Done with valid outputs",
            "reason": "Completed.",
            "evidence": ["Evidence 1"],
            "confidence": 0.9,
            "safeToAutoApply": False,
            "handoffTo": "skill-creator",
            "outputs": [
                {"path": "agent-workspace/skills/test/SKILL.md", "description": "Created skill file"}
            ],
        })

        rc, stdout, stderr = _run_lint(workspace, "--format", "json")
        data = json.loads(stdout)
        done_errors = [e for e in data.get("errors", []) if e["code"] == "followup-done-without-outputs"]
        assert len(done_errors) == 0

    def test_done_with_invalid_outputs_entry(self, workspace):
        """Output entry missing path or description triggers error."""
        _write_followup_artifact(workspace, "skill_followup", "done-bad-output.json", data={
            "version": "1",
            "kind": "skill_followup",
            "status": "done",
            "createdAt": "2026-06-22T10:00:00Z",
            "sourceCandidate": "knowledge/inbox/test.md",
            "sourceAction": "promote_to_skill",
            "suggestedDestination": "agent-workspace/skills/test/",
            "title": "Done with bad output entry",
            "reason": "Testing.",
            "evidence": ["Evidence 1"],
            "confidence": 0.8,
            "safeToAutoApply": False,
            "handoffTo": "skill-creator",
            "outputs": [
                {"path": "", "description": ""},  # Missing required fields
                "just_a_string",  # Not an object
            ],
        })

        rc, stdout, stderr = _run_lint(workspace, "--format", "json")
        data = json.loads(stdout)
        output_errors = [e for e in data.get("errors", []) if e["code"] == "followup-outputs-invalid"]
        assert len(output_errors) >= 1


# ---------------------------------------------------------------------------
# Aging tests
# ---------------------------------------------------------------------------

class TestAging:
    """Tests for followup-aging warnings."""

    def test_open_followup_within_threshold_no_warning(self, workspace):
        """Open followup created recently produces no aging warning."""
        _write_followup_artifact(workspace, "skill_followup", "recent.json")

        rc, stdout, stderr = _run_lint(workspace, "--format", "json", "--followup-max-age-days", "30")
        data = json.loads(stdout)
        aging_warnings = [w for w in data.get("warnings", []) if w["code"] == "followup-aging"]
        assert len(aging_warnings) == 0

    def test_open_followup_exceeding_threshold_gets_warning(self, workspace):
        """Open followup older than threshold produces aging warning."""
        old_date = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=45)).isoformat() + "Z"
        _write_followup_artifact(workspace, "skill_followup", "old.json", data={
            "version": "1",
            "kind": "skill_followup",
            "status": "open",
            "createdAt": old_date,
            "sourceCandidate": "knowledge/inbox/test.md",
            "sourceAction": "promote_to_skill",
            "suggestedDestination": "agent-workspace/skills/test/",
            "title": "Old Open Followup",
            "reason": "Aging test.",
            "evidence": ["Evidence 1"],
            "confidence": 0.7,
            "safeToAutoApply": False,
            "handoffTo": "skill-creator",
        })

        rc, stdout, stderr = _run_lint(workspace, "--format", "json", "--followup-max-age-days", "30")
        data = json.loads(stdout)
        aging_warnings = [w for w in data.get("warnings", []) if w["code"] == "followup-aging"]
        assert len(aging_warnings) >= 1

    def test_done_followup_not_aged(self, workspace):
        """Done followup is not checked for aging regardless of age."""
        old_date = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=100)).isoformat() + "Z"
        _write_followup_artifact(workspace, "skill_followup", "done-old.json", data={
            "version": "1",
            "kind": "skill_followup",
            "status": "done",
            "createdAt": old_date,
            "sourceCandidate": "knowledge/inbox/test.md",
            "sourceAction": "promote_to_skill",
            "suggestedDestination": "agent-workspace/skills/test/",
            "title": "Old Done Followup",
            "reason": "Completed long ago.",
            "evidence": ["Evidence 1"],
            "confidence": 0.9,
            "safeToAutoApply": False,
            "handoffTo": "skill-creator",
            "outputs": [{"path": "path/file.md", "description": "Done"}],
        })

        rc, stdout, stderr = _run_lint(workspace, "--format", "json", "--followup-max-age-days", "30")
        data = json.loads(stdout)
        aging_warnings = [w for w in data.get("warnings", []) if w["code"] == "followup-aging"]
        assert len(aging_warnings) == 0

    def test_rejected_followup_not_aged(self, workspace):
        """Rejected followup is not checked for aging."""
        old_date = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=60)).isoformat() + "Z"
        _write_followup_artifact(workspace, "skill_followup", "rejected.json", data={
            "version": "1",
            "kind": "skill_followup",
            "status": "rejected",
            "createdAt": old_date,
            "sourceCandidate": "knowledge/inbox/test.md",
            "sourceAction": "promote_to_skill",
            "suggestedDestination": "agent-workspace/skills/test/",
            "title": "Rejected Followup",
            "reason": "Rejected.",
            "evidence": [],
            "confidence": 0.3,
            "safeToAutoApply": False,
            "handoffTo": "skill-creator",
        })

        rc, stdout, stderr = _run_lint(workspace, "--format", "json", "--followup-max-age-days", "30")
        data = json.loads(stdout)
        aging_warnings = [w for w in data.get("warnings", []) if w["code"] == "followup-aging"]
        assert len(aging_warnings) == 0

    def test_in_progress_followup_aged(self, workspace):
        """In-progress followup older than threshold gets aging warning."""
        old_date = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=50)).isoformat() + "Z"
        _write_followup_artifact(workspace, "skill_followup", "in-progress.json", data={
            "version": "1",
            "kind": "skill_followup",
            "status": "in_progress",
            "createdAt": old_date,
            "sourceCandidate": "knowledge/inbox/test.md",
            "sourceAction": "promote_to_skill",
            "suggestedDestination": "agent-workspace/skills/test/",
            "title": "In Progress Old",
            "reason": "Aging test.",
            "evidence": ["Evidence 1"],
            "confidence": 0.7,
            "safeToAutoApply": False,
            "handoffTo": "skill-creator",
        })

        rc, stdout, stderr = _run_lint(workspace, "--format", "json", "--followup-max-age-days", "30")
        data = json.loads(stdout)
        aging_warnings = [w for w in data.get("warnings", []) if w["code"] == "followup-aging"]
        assert len(aging_warnings) >= 1


# ---------------------------------------------------------------------------
# Missing index tests
# ---------------------------------------------------------------------------

class TestMissingIndex:
    """Tests for query-index-missing and query-index-stale warnings."""

    def test_missing_index_warning(self, workspace):
        """When query index doesn't exist, a warning is reported."""
        # Ensure no index exists
        index_dir = workspace / "knowledge" / ".index"
        for f in index_dir.glob("*"):
            f.unlink()

        rc, stdout, stderr = _run_lint(
            workspace, "--format", "json", "--check-query-index"
        )
        data = json.loads(stdout)
        missing_warnings = [w for w in data.get("warnings", []) if w["code"] == "query-index-missing"]
        assert len(missing_warnings) >= 1

    def test_missing_index_not_checked_by_default(self, workspace):
        """Without --check-query-index, missing index is not reported."""
        index_dir = workspace / "knowledge" / ".index"
        for f in index_dir.glob("*"):
            f.unlink()

        rc, stdout, stderr = _run_lint(workspace, "--format", "json")
        data = json.loads(stdout)
        missing_warnings = [w for w in data.get("warnings", []) if w["code"] == "query-index-missing"]
        assert len(missing_warnings) == 0

    def test_index_with_entry_count_check(self, workspace):
        """Check query index validates entry count."""
        from tests.conftest import _write_curated_entry
        import knowledge_query as kq

        # Write curated entries and build index
        _write_curated_entry(
            workspace, "knowledge/facts/workspace", "e1.md",
            name="E1", scope="workspace"
        )
        _write_curated_entry(
            workspace, "knowledge/facts/workspace", "e2.md",
            name="E2", scope="workspace"
        )

        kq.build_index(workspace)
        manifest_dir = workspace / "knowledge" / ".index"
        kq.write_manifest(
            workspace,
            manifest_dir / "memory.sqlite",
            2,
            kq.compute_content_hash(kq.collect_curated_entries(workspace)),
        )

        rc, stdout, stderr = _run_lint(
            workspace, "--format", "json", "--check-query-index"
        )
        data = json.loads(stdout)
        # Should be no errors/warnings since index matches
        count_warnings = [
            w for w in data.get("warnings", [])
            if w["code"] in ("query-index-entry-count-mismatch", "query-index-stale", "query-index-missing")
        ]
        assert len(count_warnings) == 0


# ---------------------------------------------------------------------------
# Followup contract validation
# ---------------------------------------------------------------------------

class TestFollowupContractValidation:
    """Tests for all followup contract validation checks."""

    def test_missing_required_field_error(self, workspace):
        """Missing required field produces error."""
        _write_followup_artifact(workspace, "skill_followup", "missing-kind.json", data={
            "version": "1",
            # "kind" intentionally missing
            "status": "open",
            "createdAt": "2026-06-22T10:00:00Z",
            "sourceCandidate": "knowledge/inbox/test.md",
            "sourceAction": "promote_to_skill",
            "suggestedDestination": "agent-workspace/skills/test/",
            "title": "Missing kind",
            "reason": "Testing.",
            "evidence": ["Evidence"],
            "confidence": 0.5,
            "safeToAutoApply": False,
            "handoffTo": "skill-creator",
        })

        rc, stdout, stderr = _run_lint(workspace, "--format", "json")
        data = json.loads(stdout)
        missing_errors = [e for e in data["errors"] if e["code"] == "followup-missing-field"]
        assert len(missing_errors) >= 1

    def test_invalid_kind_error(self, workspace):
        """Invalid kind produces error."""
        _write_followup_artifact(workspace, "skill_followup", "bad-kind.json", data={
            "version": "1",
            "kind": "invalid_kind",
            "status": "open",
            "createdAt": "2026-06-22T10:00:00Z",
            "sourceCandidate": "knowledge/inbox/test.md",
            "sourceAction": "promote_to_skill",
            "suggestedDestination": "agent-workspace/skills/test/",
            "title": "Bad Kind",
            "reason": "Testing.",
            "evidence": ["Evidence"],
            "confidence": 0.5,
            "safeToAutoApply": False,
            "handoffTo": "skill-creator",
        })

        rc, stdout, stderr = _run_lint(workspace, "--format", "json")
        data = json.loads(stdout)
        kind_errors = [e for e in data["errors"] if e["code"] == "followup-kind-valid"]
        assert len(kind_errors) >= 1

    def test_invalid_status_error(self, workspace):
        """Invalid status produces error."""
        _write_followup_artifact(workspace, "skill_followup", "bad-status.json", data={
            "version": "1",
            "kind": "skill_followup",
            "status": "invalid_status",
            "createdAt": "2026-06-22T10:00:00Z",
            "sourceCandidate": "knowledge/inbox/test.md",
            "sourceAction": "promote_to_skill",
            "suggestedDestination": "agent-workspace/skills/test/",
            "title": "Bad Status",
            "reason": "Testing.",
            "evidence": ["Evidence"],
            "confidence": 0.5,
            "safeToAutoApply": False,
            "handoffTo": "skill-creator",
        })

        rc, stdout, stderr = _run_lint(workspace, "--format", "json")
        data = json.loads(stdout)
        status_errors = [e for e in data["errors"] if e["code"] == "followup-status-valid"]
        assert len(status_errors) >= 1

    def test_invalid_source_action_error(self, workspace):
        """Invalid sourceAction produces error."""
        _write_followup_artifact(workspace, "skill_followup", "bad-action.json", data={
            "version": "1",
            "kind": "skill_followup",
            "status": "open",
            "createdAt": "2026-06-22T10:00:00Z",
            "sourceCandidate": "knowledge/inbox/test.md",
            "sourceAction": "invalid_action",
            "suggestedDestination": "agent-workspace/skills/test/",
            "title": "Bad Action",
            "reason": "Testing.",
            "evidence": ["Evidence"],
            "confidence": 0.5,
            "safeToAutoApply": False,
            "handoffTo": "skill-creator",
        })

        rc, stdout, stderr = _run_lint(workspace, "--format", "json")
        data = json.loads(stdout)
        action_errors = [e for e in data["errors"] if e["code"] == "followup-source-action-valid"]
        assert len(action_errors) >= 1

    def test_invalid_handoff_to_error(self, workspace):
        """Invalid handoffTo produces error."""
        _write_followup_artifact(workspace, "skill_followup", "bad-handoff.json", data={
            "version": "1",
            "kind": "skill_followup",
            "status": "open",
            "createdAt": "2026-06-22T10:00:00Z",
            "sourceCandidate": "knowledge/inbox/test.md",
            "sourceAction": "promote_to_skill",
            "suggestedDestination": "agent-workspace/skills/test/",
            "title": "Bad Handoff",
            "reason": "Testing.",
            "evidence": ["Evidence"],
            "confidence": 0.5,
            "safeToAutoApply": False,
            "handoffTo": "invalid_handoff",
        })

        rc, stdout, stderr = _run_lint(workspace, "--format", "json")
        data = json.loads(stdout)
        handoff_errors = [e for e in data["errors"] if e["code"] == "followup-handoff-valid"]
        assert len(handoff_errors) >= 1

    def test_confidence_out_of_range_warning(self, workspace):
        """Confidence outside [0,1] range produces warning."""
        _write_followup_artifact(workspace, "skill_followup", "bad-confidence.json", data={
            "version": "1",
            "kind": "skill_followup",
            "status": "open",
            "createdAt": "2026-06-22T10:00:00Z",
            "sourceCandidate": "knowledge/inbox/test.md",
            "sourceAction": "promote_to_skill",
            "suggestedDestination": "agent-workspace/skills/test/",
            "title": "Bad Confidence",
            "reason": "Testing.",
            "evidence": ["Evidence"],
            "confidence": 1.5,  # > 1.0
            "safeToAutoApply": False,
            "handoffTo": "skill-creator",
        })

        rc, stdout, stderr = _run_lint(workspace, "--format", "json")
        data = json.loads(stdout)
        confidence_warnings = [w for w in data.get("warnings", []) if w["code"] == "followup-confidence-range"]
        assert len(confidence_warnings) >= 1

    def test_negative_confidence_warning(self, workspace):
        """Negative confidence produces warning."""
        _write_followup_artifact(workspace, "skill_followup", "neg-confidence.json", data={
            "version": "1",
            "kind": "skill_followup",
            "status": "open",
            "createdAt": "2026-06-22T10:00:00Z",
            "sourceCandidate": "knowledge/inbox/test.md",
            "sourceAction": "promote_to_skill",
            "suggestedDestination": "agent-workspace/skills/test/",
            "title": "Negative Confidence",
            "reason": "Testing.",
            "evidence": ["Evidence"],
            "confidence": -0.5,  # < 0
            "safeToAutoApply": False,
            "handoffTo": "skill-creator",
        })

        rc, stdout, stderr = _run_lint(workspace, "--format", "json")
        data = json.loads(stdout)
        confidence_warnings = [w for w in data.get("warnings", []) if w["code"] == "followup-confidence-range"]
        assert len(confidence_warnings) >= 1

    def test_source_candidate_missing_error(self, workspace):
        """Non-existent sourceCandidate path produces error."""
        _write_followup_artifact(workspace, "skill_followup", "missing-source.json", data={
            "version": "1",
            "kind": "skill_followup",
            "status": "open",
            "createdAt": "2026-06-22T10:00:00Z",
            "sourceCandidate": "knowledge/inbox/nonexistent.md",
            "sourceAction": "promote_to_skill",
            "suggestedDestination": "agent-workspace/skills/test/",
            "title": "Missing Source",
            "reason": "Testing.",
            "evidence": ["Evidence"],
            "confidence": 0.5,
            "safeToAutoApply": False,
            "handoffTo": "skill-creator",
        })

        rc, stdout, stderr = _run_lint(workspace, "--format", "json")
        data = json.loads(stdout)
        source_errors = [e for e in data["errors"] if e["code"] == "followup-source-candidate-missing"]
        assert len(source_errors) >= 1

    def test_empty_evidence_warning(self, workspace):
        """Empty evidence array produces warning."""
        _write_followup_artifact(workspace, "skill_followup", "no-evidence.json", data={
            "version": "1",
            "kind": "skill_followup",
            "status": "open",
            "createdAt": "2026-06-22T10:00:00Z",
            "sourceCandidate": "knowledge/inbox/test.md",
            "sourceAction": "promote_to_skill",
            "suggestedDestination": "agent-workspace/skills/test/",
            "title": "No Evidence",
            "reason": "Testing.",
            "evidence": [],
            "confidence": 0.5,
            "safeToAutoApply": False,
            "handoffTo": "skill-creator",
        })

        # Need an actual inbox candidate to exist
        from tests.conftest import _write_inbox_candidate
        _write_inbox_candidate(workspace, "test.md")

        rc, stdout, stderr = _run_lint(workspace, "--format", "json")
        data = json.loads(stdout)
        evidence_warnings = [w for w in data.get("warnings", []) if w["code"] == "followup-evidence-empty"]
        assert len(evidence_warnings) >= 1

    def test_suggested_destination_empty_error(self, workspace):
        """Empty suggestedDestination produces error."""
        _write_followup_artifact(workspace, "skill_followup", "empty-dest.json", data={
            "version": "1",
            "kind": "skill_followup",
            "status": "open",
            "createdAt": "2026-06-22T10:00:00Z",
            "sourceCandidate": "knowledge/inbox/test.md",
            "sourceAction": "promote_to_skill",
            "suggestedDestination": "",
            "title": "Empty Destination",
            "reason": "Testing.",
            "evidence": ["Evidence"],
            "confidence": 0.5,
            "safeToAutoApply": False,
            "handoffTo": "skill-creator",
        })

        from tests.conftest import _write_inbox_candidate
        _write_inbox_candidate(workspace, "test.md")

        rc, stdout, stderr = _run_lint(workspace, "--format", "json")
        data = json.loads(stdout)
        dest_errors = [e for e in data.get("errors", []) if e["code"] == "followup-destination-invalid"]
        assert len(dest_errors) >= 1

    def test_invalid_json_root_not_object(self, workspace):
        """Root JSON that is not an object produces error (no crash)."""
        fups_dir = workspace / "knowledge" / "followups" / "skill"
        fups_dir.mkdir(parents=True, exist_ok=True)
        (fups_dir / "array.json").write_text('["not", "an", "object"]', encoding="utf-8")

        rc, stdout, stderr = _run_lint(workspace, "--format", "json")
        assert rc == 1, f"Expected rc=1, got rc={rc}, stdout={stdout}"
        data = json.loads(stdout)
        json_errors = [e for e in data["errors"] if e["code"] == "followup-json-valid"]
        assert any("object" in e["detail"].lower() for e in json_errors)

    def test_module_doc_followup_validates(self, workspace):
        """Module doc followup artifacts validate correctly when sourceCandidate exists."""
        from tests.conftest import _write_inbox_candidate
        _write_inbox_candidate(workspace, "test.md")
        _write_followup_artifact(workspace, "module_doc_followup", "doc-001.json")

        rc, stdout, stderr = _run_lint(workspace, "--format", "json")
        data = json.loads(stdout)
        assert data["errorCount"] == 0

    def test_multiple_artifacts_all_checked(self, workspace):
        """Multiple artifacts are all checked and counted."""
        _write_followup_artifact(workspace, "skill_followup", "a.json")
        _write_followup_artifact(workspace, "skill_followup", "b.json")
        _write_followup_artifact(workspace, "module_doc_followup", "c.json")

        rc, stdout, stderr = _run_lint(workspace, "--format", "json")
        data = json.loads(stdout)
        assert data["followupFilesChecked"] == 3


# ---------------------------------------------------------------------------
# Lint result structure tests
# ---------------------------------------------------------------------------

class TestLintResultStructure:
    """Tests for lint result JSON structure."""

    def test_result_has_required_top_level_keys(self, workspace):
        """Lint result JSON has expected top-level structure."""
        rc, stdout, stderr = _run_lint(workspace, "--format", "json")
        data = json.loads(stdout)
        for key in ["version", "generatedAt", "followupFilesChecked",
                     "queryIndexChecked", "errors", "warnings",
                     "errorCount", "warningCount"]:
            assert key in data, f"Missing key: {key}"

    def test_error_has_level_code_path_detail(self, workspace):
        """Each error entry has level, code, path, detail."""
        fups_dir = workspace / "knowledge" / "followups" / "skill"
        fups_dir.mkdir(parents=True, exist_ok=True)
        (fups_dir / "bad.json").write_text("invalid json", encoding="utf-8")

        rc, stdout, stderr = _run_lint(workspace, "--format", "json")
        data = json.loads(stdout)
        for err in data["errors"]:
            assert "level" in err
            assert "code" in err
            assert "path" in err
            assert "detail" in err
            assert err["level"] == "error"

    def test_warning_has_level_code_path_detail(self, workspace):
        """Each warning entry has level, code, path, detail."""
        from tests.conftest import _write_inbox_candidate

        _write_followup_artifact(workspace, "skill_followup", "warn.json", data={
            "version": "1",
            "kind": "skill_followup",
            "status": "open",
            "createdAt": "2026-06-22T10:00:00Z",
            "sourceCandidate": "knowledge/inbox/test.md",
            "sourceAction": "promote_to_skill",
            "suggestedDestination": "agent-workspace/skills/test/",
            "title": "Warning Test",
            "reason": "Testing.",
            "evidence": [],
            "confidence": 0.5,
            "safeToAutoApply": False,
            "handoffTo": "skill-creator",
        })
        _write_inbox_candidate(workspace, "test.md")

        rc, stdout, stderr = _run_lint(workspace, "--format", "json")
        data = json.loads(stdout)
        for warn in data["warnings"]:
            assert "level" in warn
            assert "code" in warn
            assert "path" in warn
            assert "detail" in warn
            assert warn["level"] == "warning"

    def test_lint_exits_nonzero_on_errors(self, workspace):
        """Lint exits with code 1 when there are errors."""
        fups_dir = workspace / "knowledge" / "followups" / "skill"
        fups_dir.mkdir(parents=True, exist_ok=True)
        (fups_dir / "bad.json").write_text("invalid json", encoding="utf-8")

        rc, stdout, stderr = _run_lint(workspace, "--format", "json")
        assert rc == 1

    def test_lint_exits_zero_when_only_warnings(self, workspace):
        """Lint exits with code 0 when there are only warnings."""
        from tests.conftest import _write_inbox_candidate

        _write_followup_artifact(workspace, "skill_followup", "warn.json", data={
            "version": "1",
            "kind": "skill_followup",
            "status": "open",
            "createdAt": "2026-06-22T10:00:00Z",
            "sourceCandidate": "knowledge/inbox/test.md",
            "sourceAction": "promote_to_skill",
            "suggestedDestination": "agent-workspace/skills/test/",
            "title": "Test",
            "reason": "Testing.",
            "evidence": [],
            "confidence": 0.5,
            "safeToAutoApply": False,
            "handoffTo": "skill-creator",
        })
        _write_inbox_candidate(workspace, "test.md")

        rc, stdout, stderr = _run_lint(workspace, "--format", "json")
        # Evidence-empty is a warning, so should exit 0
        assert rc == 0
