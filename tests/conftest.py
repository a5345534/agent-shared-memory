"""Shared fixtures for all test modules."""

from __future__ import annotations

import os
import sys

# Suppress .pyc bytecode generation to keep workspace clean
sys.dont_write_bytecode = True

import datetime as dt
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

# Ensure scripts directory is importable
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_workspace(base: Path) -> Path:
    """Create a valid workspace root with AGENTS.md and necessary directories."""
    ws = base / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "AGENTS.md").write_text("# Test Workspace\n", encoding="utf-8")
    # Create shared-memory directories
    (ws / "knowledge" / "inbox").mkdir(parents=True, exist_ok=True)
    (ws / "knowledge" / "facts" / "workspace").mkdir(parents=True, exist_ok=True)
    (ws / "knowledge" / "facts" / "module" / "testmod").mkdir(parents=True, exist_ok=True)
    (ws / "knowledge" / "facts" / "capability" / "testcap").mkdir(parents=True, exist_ok=True)
    (ws / "knowledge" / "followups" / "skill").mkdir(parents=True, exist_ok=True)
    (ws / "knowledge" / "followups" / "module-doc").mkdir(parents=True, exist_ok=True)
    (ws / "knowledge" / ".index").mkdir(parents=True, exist_ok=True)
    # Create MEMORY.md workspace index
    (ws / "knowledge" / "facts" / "workspace" / "MEMORY.md").write_text(
        "## Pitfalls / Operational Boundaries\n\n", encoding="utf-8"
    )
    return ws


def _write_inbox_candidate(
    ws: Path,
    filename: str,
    name: str = "Test Memory",
    description: str = "A test memory entry.",
    suggested_action: str = "retain_memory",
    suggested_scope: str = "workspace",
    memory_type: str = "feedback",
    candidate_id: str = "",
    extra_frontmatter: dict | None = None,
    body: str = "",
) -> Path:
    """Write a Markdown inbox candidate with valid frontmatter."""
    lines = ["---"]
    lines.append(f"name: {name}")
    lines.append(f"description: {description}")
    lines.append(f"type: {memory_type}")
    lines.append(f"suggested_action: {suggested_action}")
    lines.append(f"suggested_scope: {suggested_scope}")
    lines.append(f"captured_at: {dt.date.today().isoformat()}")
    lines.append("source: agent:test")
    if candidate_id:
        lines.append(f"candidate_id: {candidate_id}")
    if extra_frontmatter:
        for k, v in extra_frontmatter.items():
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    if body:
        lines.append(body)
    else:
        lines.append("This is the body of the test memory entry.")
    inbox = ws / "knowledge" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    p = inbox / filename
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _write_curated_entry(
    ws: Path,
    scope_dir: str,
    filename: str,
    name: str = "Curated Entry",
    description: str = "A curated test entry.",
    scope: str = "workspace",
    memory_type: str = "reference",
    verified_at: str = "",
    body: str = "",
    source: str = "agent:test",
    tags: list[str] | None = None,
    extra_frontmatter: dict | None = None,
) -> Path:
    """Write a curated shared-memory Markdown entry."""
    if not verified_at:
        verified_at = dt.date.today().isoformat()
    lines = ["---"]
    lines.append(f"name: {name}")
    lines.append(f"description: {description}")
    lines.append(f"type: {memory_type}")
    lines.append(f"scope: {scope}")
    lines.append(f"verified_at: {verified_at}")
    lines.append(f"source: {source}")
    if tags:
        lines.append("tags:")
        for t in tags:
            lines.append(f"  - {t}")
    if extra_frontmatter:
        for k, v in extra_frontmatter.items():
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    if body:
        lines.append(body)
    else:
        lines.append(f"This is the body of {name}.")
    # Determine target dir
    parts = scope_dir.split("/")
    target = ws
    for part in parts:
        target = target / part
    target.mkdir(parents=True, exist_ok=True)
    p = target / filename
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _write_followup_artifact(
    ws: Path,
    kind: str,
    filename: str,
    data: dict[str, Any] | None = None,
) -> Path:
    """Write a follow-up JSON artifact."""
    if data is None:
        data = {
            "version": "1",
            "kind": kind,
            "status": "open",
            "createdAt": dt.datetime.now(dt.timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            + "Z",
            "sourceCandidate": "knowledge/inbox/test.md",
            "sourceAction": (
                "promote_to_skill" if kind == "skill_followup" else "promote_to_module_doc"
            ),
            "suggestedDestination": "agent-workspace/skills/test/",
            "title": "Test: A test follow-up artifact",
            "reason": "Testing lint validation.",
            "evidence": ["Test evidence 1"],
            "confidence": 0.7,
            "safeToAutoApply": False,
            "handoffTo": "skill-creator" if kind == "skill_followup" else "doc-writer",
        }
    kind_dir = {
        "skill_followup": "skill",
        "module_doc_followup": "module-doc",
    }.get(kind, kind)
    followups_root = ws / "knowledge" / "followups" / kind_dir
    followups_root.mkdir(parents=True, exist_ok=True)
    p = followups_root / filename
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def workspace():
    """Create a fresh temporary workspace with AGENTS.md and shared-memory dirs."""
    with tempfile.TemporaryDirectory() as tmp:
        ws = _make_workspace(Path(tmp))
        yield ws


@pytest.fixture(scope="function")
def workspace_with_inbox(workspace):
    """Workspace with a sample inbox candidate."""
    _write_inbox_candidate(
        workspace,
        "test-memory.md",
        name="System Validation Hook",
        description="A validation hook that runs before every commit.",
        suggested_action="retain_memory",
        suggested_scope="workspace",
    )
    return workspace


@pytest.fixture(scope="function")
def workspace_with_curated(workspace):
    """Workspace with curated entries for index building."""
    _write_curated_entry(
        workspace,
        "knowledge/facts/workspace",
        "validation-hook.md",
        name="System Validation Hook",
        description="A validation hook that runs before every commit.",
        scope="workspace",
        memory_type="reference",
        body="This validation hook ensures that all critical checks pass before a git commit.",
    )
    _write_curated_entry(
        workspace,
        "knowledge/facts/module/testmod",
        "module-entry.md",
        name="Test Module Entry",
        description="A curated module entry.",
        scope="module:testmod",
        memory_type="architectural-invariant",
        body="Module testmod has a specific architecture constraint: all inputs must be validated.",
    )
    _write_curated_entry(
        workspace,
        "knowledge/facts/capability/testcap",
        "capability-entry.md",
        name="Test Capability Entry",
        description="A curated capability entry.",
        scope="capability:testcap",
        memory_type="reference",
        body="Capability testcap handles external API integration for the platform.",
    )
    return workspace
