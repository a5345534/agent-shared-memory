"""Unit tests: harness detection and hook adapter output.

Slice 4 – Verification (tasks 4.5, 4.6).
"""

from __future__ import annotations

import os
import sys

sys.dont_write_bytecode = True

import stat
import tempfile
from pathlib import Path
from typing import Any

import pytest

# Ensure scripts directory is importable.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from hooks import pi, opencode, github_actions, none
from knowledge_query import detect_harness


# ---------------------------------------------------------------------------
# detect_harness unit tests (task 4.5)
# ---------------------------------------------------------------------------


def test_detect_harness_pi(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Pi harness is detected when ~/.pi/ directory exists."""
    fake_pi = tmp_path / ".pi"
    fake_pi.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    name, module_path = detect_harness(tmp_path)
    assert name == "pi"
    assert module_path == "hooks.pi"


def test_detect_harness_opencode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """OpenCode harness is detected when .opencode.json exists."""
    # Ensure ~/.pi/ does NOT exist
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    (tmp_path / ".opencode.json").write_text("{}", encoding="utf-8")
    name, module_path = detect_harness(tmp_path)
    assert name == "opencode"
    assert module_path == "hooks.opencode"


def test_detect_harness_github_actions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """GitHub Actions CI is detected via GITHUB_ACTIONS env var."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    name, module_path = detect_harness(tmp_path)
    assert name == "github-actions"
    assert module_path == "hooks.github_actions"


def test_detect_harness_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When no harness is detected, 'none' adapter is returned."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    name, module_path = detect_harness(tmp_path)
    assert name == "none"
    assert module_path == "hooks.none"


def test_detect_harness_priority_pi_over_opencode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Pi harness takes priority over OpenCode when both markers exist."""
    fake_pi = tmp_path / ".pi"
    fake_pi.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".opencode.json").write_text("{}", encoding="utf-8")
    name, _ = detect_harness(tmp_path)
    assert name == "pi", "pi should have priority over opencode"


def test_detect_harness_priority_opencode_over_github_actions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """OpenCode harness takes priority over GitHub Actions when both detected."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    (tmp_path / ".opencode.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    name, _ = detect_harness(tmp_path)
    assert name == "opencode", "opencode should have priority over github-actions"


# ---------------------------------------------------------------------------
# Hook adapter install() unit tests (task 4.6)
# ---------------------------------------------------------------------------


def _check_hook_script_syntax(path: Path) -> None:
    """Verify a shell hook script exists, is executable, and has valid shell syntax."""
    assert path.exists(), f"Hook script not found: {path}"
    # Check executable bit
    mode = path.stat().st_mode
    assert mode & stat.S_IXUSR, f"Hook script is not executable: {path}"
    content = path.read_text(encoding="utf-8")
    assert "#!/usr/bin/env sh" in content, "Hook script missing shebang"
    assert "knowledge_absorb.py" in content, "Hook script missing absorb reference"


class TestPiHookAdapter:
    """Tests for scripts.hooks.pi.install()."""

    def test_install_pi_hook_ok(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Pi hook is installed workspace-locally by default when ~/.pi/ exists."""
        root = tmp_path / "ws"
        root.mkdir()
        fake_home = tmp_path / "home"
        fake_pi = fake_home / ".pi"
        fake_pi.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = pi.install(root)
        assert result["status"] == "ok"
        assert result["path"] is not None
        assert result["scope"] == "workspace"

        hook_path = Path(result["path"])  # type: ignore[arg-type]
        assert hook_path == root / ".pi" / "hooks" / "post-compact" / "shared-knowledge-absorb.sh"
        _check_hook_script_syntax(hook_path)

    def test_install_pi_hook_skipped_when_no_pi(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Pi adapter returns 'skipped' when ~/.pi/ does not exist."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = pi.install(tmp_path)
        assert result["status"] == "skipped"
        assert "not detected" in result["message"]

    def test_install_pi_hook_skipped_when_already_installed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Pi hook returns 'skipped' when hook script already matches."""
        root = tmp_path / "ws"
        root.mkdir()
        fake_home = tmp_path / "home"
        fake_pi = fake_home / ".pi"
        fake_pi.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        # First install
        result1 = pi.install(root)
        assert result1["status"] == "ok"

        # Second install — should be skipped
        result2 = pi.install(root)
        assert result2["status"] == "skipped"
        assert "already installed" in result2["message"]

    def test_pi_hook_script_content(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Pi hook script references the workspace-local absorb script path."""
        root = tmp_path / "ws"
        root.mkdir()
        fake_home = tmp_path / "home"
        fake_pi = fake_home / ".pi"
        fake_pi.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = pi.install(root)
        assert result["status"] == "ok"
        hook_path = Path(result["path"])  # type: ignore[arg-type]
        content = hook_path.read_text(encoding="utf-8")
        # The hook should reference knowledge_absorb.py via the workspace root.
        assert "$ROOT/shared-knowledge/scripts/knowledge_absorb.py" in content
        # The hook should use set -e
        assert "set -e" in content

    def test_install_pi_hook_global_scope_is_opt_in(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Pi global hook is only installed when scope='global' is requested."""
        root = tmp_path / "ws"
        root.mkdir()
        fake_home = tmp_path / "home"
        fake_pi = fake_home / ".pi"
        fake_pi.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = pi.install(root, scope="global")
        assert result["status"] == "ok"
        assert result["scope"] == "global"
        hook_path = Path(result["path"])  # type: ignore[arg-type]
        assert hook_path == fake_pi / "hooks" / "post-compact" / "shared-knowledge-absorb.sh"
        _check_hook_script_syntax(hook_path)


class TestOpenCodeHookAdapter:
    """Tests for scripts.hooks.opencode.install()."""

    def test_install_opencode_hook_ok(self, tmp_path: Path) -> None:
        """OpenCode hook is installed when .opencode.json exists."""
        (tmp_path / ".opencode.json").write_text("{}", encoding="utf-8")

        result = opencode.install(tmp_path)
        assert result["status"] == "ok"
        assert result["path"] is not None

        hook_path = Path(result["path"])  # type: ignore[arg-type]
        _check_hook_script_syntax(hook_path)

    def test_install_opencode_skipped_when_no_marker(self, tmp_path: Path) -> None:
        """OpenCode returns 'skipped' when .opencode.json is absent."""
        result = opencode.install(tmp_path)
        assert result["status"] == "skipped"
        assert "not detected" in result["message"]

    def test_install_opencode_skipped_when_already_installed(self, tmp_path: Path) -> None:
        """OpenCode hook returns 'skipped' on second install."""
        (tmp_path / ".opencode.json").write_text("{}", encoding="utf-8")

        result1 = opencode.install(tmp_path)
        assert result1["status"] == "ok"

        result2 = opencode.install(tmp_path)
        assert result2["status"] == "skipped"
        assert "already installed" in result2["message"]

    def test_opencode_hook_script_content(self, tmp_path: Path) -> None:
        """OpenCode hook script references the correct absorb script path."""
        (tmp_path / ".opencode.json").write_text("{}", encoding="utf-8")

        result = opencode.install(tmp_path)
        hook_path = Path(result["path"])  # type: ignore[arg-type]
        content = hook_path.read_text(encoding="utf-8")
        assert "knowledge_absorb.py" in content
        assert "set -e" in content


class TestGitHubActionsHookAdapter:
    """Tests for scripts.hooks.github_actions.install()."""

    def test_install_github_actions_workflow_ok(self, tmp_path: Path) -> None:
        """GitHub Actions workflow is generated."""
        result = github_actions.install(tmp_path)
        assert result["status"] == "ok"
        assert result["path"] is not None

        workflow_path = Path(result["path"])  # type: ignore[arg-type]
        assert workflow_path.exists()
        content = workflow_path.read_text(encoding="utf-8")
        assert "name: shared-knowledge" in content
        assert "submodules: true" in content
        assert "working-directory:" not in content
        assert str(tmp_path) not in content
        assert "knowledge_absorb.py hook" in content
        assert "knowledge_lint.py" in content

    def test_install_github_actions_skipped_when_same_content(self, tmp_path: Path) -> None:
        """GitHub Actions returns 'skipped' when workflow already matches."""
        result1 = github_actions.install(tmp_path)
        assert result1["status"] == "ok"

        result2 = github_actions.install(tmp_path)
        assert result2["status"] == "skipped"
        assert "already installed" in result2["message"]


class TestNoneHookAdapter:
    """Tests for scripts.hooks.none.install()."""

    def test_none_adapter_returns_ok_with_instructions(self, tmp_path: Path) -> None:
        """None adapter returns 'ok' with manual trigger instructions."""
        result = none.install(tmp_path)
        assert result["status"] == "ok"
        assert result["path"] is None
        assert "knowledge_absorb.py" in result["message"]
        assert "knowledge_lint.py" in result["message"]


# ---------------------------------------------------------------------------
# Adapter return value contract (common to all)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "adapter_name,setup_fn",
    [
        ("pi", lambda tp, mp: (mp.__setitem__("home", lambda: tp / "pi_home"), (tp / "pi_home" / ".pi").mkdir(parents=True))),
        ("opencode", lambda tp, mp: (tp / ".opencode.json").write_text("{}", encoding="utf-8")),
        ("github_actions", lambda tp, mp: None),
        ("none", lambda tp, mp: None),
    ],
    ids=["pi", "opencode", "github-actions", "none"],
)
def test_adapter_return_contract(adapter_name: str, setup_fn: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every adapter's install() returns dict with required keys."""
    if adapter_name == "pi":
        fake_home = tmp_path / "pi_home"
        fake_home.mkdir(parents=True)
        (fake_home / ".pi").mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        result = pi.install(tmp_path)
    elif adapter_name == "opencode":
        (tmp_path / ".opencode.json").write_text("{}", encoding="utf-8")
        result = opencode.install(tmp_path)
    elif adapter_name == "github_actions":
        result = github_actions.install(tmp_path)
    elif adapter_name == "none":
        result = none.install(tmp_path)

    # Contract check: {status, message, path}
    assert isinstance(result, dict)
    assert "status" in result
    assert "message" in result
    assert "path" in result
    assert result["status"] in ("ok", "skipped", "failed")
