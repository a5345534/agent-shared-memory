"""Unit tests for follow-up artifact creation."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import _write_inbox_candidate, _write_followup_artifact


# ---------------------------------------------------------------------------
# Import the module under test for direct function testing
# ---------------------------------------------------------------------------

# knowledge_absorb is imported via sys.path manipulation in conftest
import knowledge_absorb as ka


# ---------------------------------------------------------------------------
# Follow-up artifact data tests (unit)
# ---------------------------------------------------------------------------

class TestApplyFollowupArtifact:
    """Unit tests for apply_followup_artifact function."""

    def test_creates_skill_followup_from_inbox(self, workspace):
        """GIVEN an inbox candidate with promote_to_skill action
        WHEN apply_followup_artifact is called
        THEN a skill followup JSON is created."""
        inbox = _write_inbox_candidate(
            workspace,
            "example.md",
            name="Example Skill Candidate",
            description="A candidate for skill promotion.",
            suggested_action="promote_to_skill",
            candidate_id="example-001",
        )

        action = {
            "candidatePath": "knowledge/inbox/example.md",
            "action": "promote_to_skill",
            "destination": "agent-workspace/skills/example/",
            "reason": "Looks like a reusable skill.",
            "evidence": ["evidence 1"],
            "confidence": 0.7,
        }

        result = ka.apply_followup_artifact(workspace, action)

        assert "path" in result
        assert result.get("existing") is not True
        followup_path = workspace / result["path"]
        assert followup_path.exists()
        data = json.loads(followup_path.read_text(encoding="utf-8"))
        assert data["kind"] == "skill_followup"
        assert data["status"] == "open"
        assert data["safeToAutoApply"] is False
        assert data["handoffTo"] == "skill-creator"
        assert data["sourceCandidate"] == "knowledge/inbox/example.md"
        assert data["sourceAction"] == "promote_to_skill"

    def test_creates_module_doc_followup(self, workspace):
        """GIVEN an inbox candidate with promote_to_module_doc action
        WHEN apply_followup_artifact is called
        THEN a module-doc followup JSON is created."""
        inbox = _write_inbox_candidate(
            workspace,
            "ops-guide.md",
            name="Ops Guide",
            description="Operations guide candidate.",
            suggested_action="promote_to_module_doc",
            candidate_id="ops-guide",
        )

        action = {
            "candidatePath": "knowledge/inbox/ops-guide.md",
            "action": "promote_to_module_doc",
            "reason": "Looks like module documentation.",
            "evidence": ["evidence 1"],
            "confidence": 0.6,
        }

        result = ka.apply_followup_artifact(workspace, action)

        assert "path" in result
        assert result.get("existing") is not True
        followup_path = workspace / result["path"]
        assert followup_path.exists()
        data = json.loads(followup_path.read_text(encoding="utf-8"))
        assert data["kind"] == "module_doc_followup"
        assert data["handoffTo"] == "doc-writer"

    def test_uses_filename_stem_when_no_candidate_id(self, workspace):
        """GIVEN a candidate without candidate_id
        WHEN apply_followup_artifact is called
        THEN the followup filename uses the slugified filename stem."""
        _write_inbox_candidate(
            workspace,
            "postcompact-memory.md",
            name="Post-compact Memory",
            suggested_action="promote_to_skill",
        )

        action = {
            "candidatePath": "knowledge/inbox/postcompact-memory.md",
            "action": "promote_to_skill",
            "reason": "Test.",
            "evidence": [],
            "confidence": 0.5,
        }

        result = ka.apply_followup_artifact(workspace, action)
        followup_path = workspace / result["path"]
        assert followup_path.exists()
        assert "postcompact-memory" in followup_path.name

    def test_idempotent_exact_match(self, workspace):
        """GIVEN an existing followup for the same sourceCandidate+sourceAction
        WHEN apply_followup_artifact is called again
        THEN it returns existing=True and does not create a duplicate."""
        inbox = _write_inbox_candidate(
            workspace,
            "example.md",
            name="Example",
            suggested_action="promote_to_skill",
            candidate_id="example-001",
        )

        action = {
            "candidatePath": "knowledge/inbox/example.md",
            "action": "promote_to_skill",
            "reason": "Test.",
            "evidence": [],
            "confidence": 0.5,
        }

        result1 = ka.apply_followup_artifact(workspace, action)
        assert result1.get("existing") is not True

        result2 = ka.apply_followup_artifact(workspace, action)
        assert result2.get("existing") is True
        # Path should be the same
        assert result2["path"] == result1["path"]

    def test_numeric_suffix_on_id_collision(self, workspace):
        """GIVEN an existing followup with same candidateId but different sourceCandidate
        WHEN a new candidate with colliding ID is processed
        THEN a numeric suffix is appended to the filename."""
        # Create first inbox + followup
        inbox1 = _write_inbox_candidate(
            workspace,
            "example.md",
            name="First Example",
            suggested_action="promote_to_skill",
            candidate_id="example-001",
        )
        action1 = {
            "candidatePath": "knowledge/inbox/example.md",
            "action": "promote_to_skill",
            "reason": "First.",
            "evidence": [],
            "confidence": 0.5,
        }
        result1 = ka.apply_followup_artifact(workspace, action1)
        assert "example-001" in result1["path"]

        # Create second inbox with different filename but same candidate_id
        inbox2 = _write_inbox_candidate(
            workspace,
            "other-example.md",
            name="Other Example",
            suggested_action="promote_to_skill",
            candidate_id="example-001",
        )
        action2 = {
            "candidatePath": "knowledge/inbox/other-example.md",
            "action": "promote_to_skill",
            "reason": "Second, colliding.",
            "evidence": [],
            "confidence": 0.5,
        }
        result2 = ka.apply_followup_artifact(workspace, action2)
        # Should have a numeric suffix like example-001-2.json
        assert "example-001-2" in result2["path"] or "example-001-2" in Path(result2["path"]).stem

    def test_inbox_candidate_not_deleted_after_followup(self, workspace):
        """GIVEN an inbox candidate that triggers a followup
        WHEN apply_followup_artifact runs
        THEN the inbox candidate still exists."""
        inbox = _write_inbox_candidate(
            workspace,
            "surviving.md",
            name="Surviving Candidate",
            suggested_action="promote_to_skill",
            candidate_id="surviving-001",
        )

        action = {
            "candidatePath": "knowledge/inbox/surviving.md",
            "action": "promote_to_skill",
            "reason": "Test.",
            "evidence": [],
            "confidence": 0.5,
        }

        ka.apply_followup_artifact(workspace, action)
        assert inbox.exists(), "Inbox candidate should survive follow-up creation"

    def test_does_not_create_skill_directory(self, workspace):
        """GIVEN a promote_to_skill action
        WHEN apply_followup_artifact runs
        THEN no skill directory is created under agent-workspace/skills/."""
        _write_inbox_candidate(
            workspace,
            "no-skill.md",
            name="No Skill Creation",
            suggested_action="promote_to_skill",
        )
        action = {
            "candidatePath": "knowledge/inbox/no-skill.md",
            "action": "promote_to_skill",
            "reason": "Test.",
            "evidence": [],
            "confidence": 0.5,
        }
        ka.apply_followup_artifact(workspace, action)
        # Verify no agent-workspace/skills directory was created
        skills_dir = workspace / "agent-workspace" / "skills"
        assert not skills_dir.exists(), "Should NOT create agent-workspace/skills/"

    def test_does_not_write_module_docs(self, workspace):
        """GIVEN a promote_to_module_doc action
        WHEN apply_followup_artifact runs
        THEN no module docs are written."""
        _write_inbox_candidate(
            workspace,
            "no-mod-docs.md",
            name="No Module Docs Creation",
            suggested_action="promote_to_module_doc",
        )
        action = {
            "candidatePath": "knowledge/inbox/no-mod-docs.md",
            "action": "promote_to_module_doc",
            "reason": "Test.",
            "evidence": [],
            "confidence": 0.5,
        }
        ka.apply_followup_artifact(workspace, action)
        # Check common module-docs paths
        for p in workspace.rglob("**/docs/"):
            if "followups" not in str(p):
                assert False, f"Should NOT create module docs at {p}"


# ---------------------------------------------------------------------------
# render_followup_artifact unit tests
# ---------------------------------------------------------------------------

class TestRenderFollowupArtifact:
    """Unit tests for render_followup_artifact."""

    def test_renders_complete_artifact(self):
        """The rendered artifact has all required fields."""
        artifact = ka.render_followup_artifact(
            source_candidate="knowledge/inbox/test.md",
            source_action="promote_to_skill",
            frontmatter={"name": "Test", "description": "A test"},
            body="Test body",
            suggested_destination="agent-workspace/skills/test/",
            reason="Testing.",
            evidence=["ev1"],
            confidence=0.75,
        )
        for field in ka.FOLLOWUP_REQUIRED_FIELDS if hasattr(ka, "FOLLOWUP_REQUIRED_FIELDS") else [
            "version", "kind", "status", "createdAt", "sourceCandidate", "sourceAction",
            "suggestedDestination", "title", "reason", "evidence", "confidence",
            "safeToAutoApply", "handoffTo",
        ]:
            assert field in artifact, f"Missing required field: {field}"

    def test_confidence_between_zero_and_one(self):
        """Confidence is a float between 0 and 1."""
        artifact = ka.render_followup_artifact(
            source_candidate="test.md",
            source_action="promote_to_skill",
            frontmatter={},
            body="",
            suggested_destination="dest/",
            reason="test",
            evidence=[],
            confidence=0.65,
        )
        assert 0 <= artifact["confidence"] <= 1

    def test_handoff_matches_action(self):
        """handoffTo matches the source action."""
        artifact_skill = ka.render_followup_artifact(
            source_candidate="test.md",
            source_action="promote_to_skill",
            frontmatter={},
            body="",
            suggested_destination="dest/",
            reason="test",
            evidence=[],
            confidence=0.5,
        )
        assert artifact_skill["handoffTo"] == "skill-creator"

        artifact_doc = ka.render_followup_artifact(
            source_candidate="test.md",
            source_action="promote_to_module_doc",
            frontmatter={},
            body="",
            suggested_destination="dest/",
            reason="test",
            evidence=[],
            confidence=0.5,
        )
        assert artifact_doc["handoffTo"] == "doc-writer"


# ---------------------------------------------------------------------------
# Idempotency / edge cases
# ---------------------------------------------------------------------------

class TestFollowupEdgeCases:
    """Edge case tests for follow-up artifact handling."""

    def test_missing_source_file_returns_error(self, workspace):
        """If the source candidate file doesn't exist, an error is returned."""
        action = {
            "candidatePath": "knowledge/inbox/nonexistent.md",
            "action": "promote_to_skill",
            "reason": "Test.",
        }
        result = ka.apply_followup_artifact(workspace, action)
        assert "error" in result

    def test_invalid_action_returns_error(self, workspace):
        """If the action is not a promote action, an error is returned."""
        _write_inbox_candidate(workspace, "test.md")
        action = {
            "candidatePath": "knowledge/inbox/test.md",
            "action": "retain_memory",
            "reason": "Test.",
        }
        result = ka.apply_followup_artifact(workspace, action)
        assert "error" in result

    def test_multiple_followup_creations_different_actions(self, workspace):
        """Same source candidate can produce both skill and module-doc followups."""
        _write_inbox_candidate(
            workspace,
            "dual.md",
            name="Dual purpose",
            suggested_action="promote_to_skill",
            candidate_id="dual-001",
        )

        action_skill = {
            "candidatePath": "knowledge/inbox/dual.md",
            "action": "promote_to_skill",
            "reason": "Skill promotion.",
            "evidence": [],
            "confidence": 0.5,
        }
        action_doc = {
            "candidatePath": "knowledge/inbox/dual.md",
            "action": "promote_to_module_doc",
            "reason": "Doc promotion.",
            "evidence": [],
            "confidence": 0.5,
        }

        result_skill = ka.apply_followup_artifact(workspace, action_skill)
        result_doc = ka.apply_followup_artifact(workspace, action_doc)

        # Both should create distinct files
        assert result_skill.get("existing") is not True
        assert result_doc.get("existing") is not True
        assert result_skill["path"] != result_doc["path"]

        # Verify files exist in correct directories
        skill_path = workspace / result_skill["path"]
        doc_path = workspace / result_doc["path"]
        assert skill_path.exists()
        assert doc_path.exists()
        assert "skill/" in str(skill_path)
        assert "module-doc/" in str(doc_path)
