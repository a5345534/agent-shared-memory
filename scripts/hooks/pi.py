#!/usr/bin/env python3
"""DEPRECATED Pi harness hook adapter.

Use ``hooks.pi_lifecycle`` instead, which installs a unified TypeScript
extension for both producer (candidate generation) and absorber (inbox
absorption).

This module is kept for backward compatibility via ``init --legacy-hook``
and will be removed in a future release.
"""
from __future__ import annotations

import stat
from pathlib import Path
from typing import Any


def install(root: Path, scope: str = "workspace") -> dict[str, Any]:
    if scope not in {"workspace", "global"}:
        return {"status": "failed", "message": f"Unsupported Pi hook scope: {scope}", "path": None}

    pi_dir = Path.home() / ".pi"
    if not pi_dir.is_dir():
        return {"status": "skipped", "message": "Pi harness not detected (~/.pi/ not found).", "path": None}

    hooks_dir = (root.resolve() / ".pi" if scope == "workspace" else pi_dir) / "hooks" / "post-compact"
    hook_path = hooks_dir / "shared-knowledge-absorb.sh"
    hook_content = _hook_script(root, scope)

    if hook_path.is_file() and hook_path.read_text(encoding="utf-8").strip() == hook_content.strip():
        return {
            "status": "skipped",
            "message": f"Pi {scope} post-compact hook already installed: {hook_path}",
            "path": str(hook_path),
            "scope": scope,
        }

    try:
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook_path.write_text(hook_content, encoding="utf-8")
        hook_path.chmod(hook_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return {
            "status": "ok",
            "message": f"Pi {scope} post-compact hook installed: {hook_path}",
            "path": str(hook_path),
            "scope": scope,
        }
    except (OSError, PermissionError) as exc:
        return {
            "status": "failed",
            "message": f"Failed to install Pi {scope} hook: {exc}",
            "path": str(hook_path) if hook_path.exists() else None,
            "scope": scope,
        }


def _hook_script(root: Path, scope: str) -> str:
    root_path = root.resolve()
    if scope == "workspace":
        return """#!/usr/bin/env sh
# Pi workspace post-compact hook -- installed by shared-knowledge init
set -e
HOOK_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$HOOK_DIR/../../.." && pwd)
cd "$ROOT"
python3 "$ROOT/shared-knowledge/scripts/knowledge_absorb.py" hook
"""

    absorb = Path(__file__).resolve().parents[1] / "knowledge_absorb.py"
    return f"""#!/usr/bin/env sh
# Pi global post-compact hook -- installed by shared-knowledge init --hook-scope global
set -e
cd "{root_path}"
python3 "{absorb}" hook
"""
