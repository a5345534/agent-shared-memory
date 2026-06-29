#!/usr/bin/env python3
"""Unified Pi lifecycle hook adapter.

Installs a Pi TypeScript extension that handles both pre-compact candidate
generation (producer) and post-compact inbox absorption (absorber) in a single
extension, running both via detached spawn to avoid blocking the session.

Replaces the legacy ``pi.py`` which installed a post-compact shell hook only.
"""
from __future__ import annotations

import shutil
import stat
from pathlib import Path
from typing import Any


def install(root: Path, scope: str = "workspace", legacy_hook: bool = False) -> dict[str, Any]:
    """Install the unified lifecycle Pi extension.

    Args:
        root: Workspace root directory.
        scope: ``"workspace"`` (default, under ``<root>/.pi/``) or
            ``"global"`` (under ``~/.pi/``).
        legacy_hook: If True, also install the old post-compact shell hook
            (deprecated compatibility path).

    Returns:
        A dict with ``status``, ``message``, and ``path`` keys.
    """
    if scope not in {"workspace", "global"}:
        return {"status": "failed", "message": f"Unsupported Pi hook scope: {scope}", "path": None}

    pi_dir = Path.home() / ".pi"
    if not pi_dir.is_dir():
        return {"status": "skipped", "message": "Pi harness not detected (~/.pi/ not found).", "path": None}

    results: list[dict[str, Any]] = []

    # Install the TypeScript extension
    ext_result = _install_extension(root, scope)
    results.append(ext_result)

    # Optionally install legacy shell hook
    if legacy_hook:
        legacy_result = _install_legacy_hook(root, scope)
        results.append(legacy_result)

    # Aggregate
    errors = [r for r in results if r["status"] == "failed"]
    if errors:
        return {
            "status": "failed",
            "message": "; ".join(r["message"] for r in errors),
            "path": results[0].get("path"),
            "results": results,
        }

    all_skipped = all(r["status"] == "skipped" for r in results)
    if all_skipped:
        return {
            "status": "skipped",
            "message": "; ".join(r["message"] for r in results),
            "path": results[0].get("path"),
            "results": results,
        }

    return {
        "status": "ok",
        "message": "; ".join(r["message"] for r in results),
        "path": results[0].get("path"),
        "results": results,
    }


def _extension_dir(root: Path, scope: str) -> Path:
    """Return the Pi extension directory for the given scope."""
    if scope == "global":
        return Path.home() / ".pi" / "agent" / "extensions"
    return root.resolve() / ".pi" / "extensions"


def _install_extension(root: Path, scope: str) -> dict[str, Any]:
    """Install or update the shared-knowledge-lifecycle.ts extension."""
    ext_dir = _extension_dir(root, scope)
    ext_path = ext_dir / "shared-knowledge-lifecycle.ts"
    content = _extension_script(root)

    # Check if already installed with identical content
    if ext_path.is_file() and ext_path.read_text(encoding="utf-8").strip() == content.strip():
        return {
            "status": "skipped",
            "message": f"Pi {scope} lifecycle extension already installed: {ext_path}",
            "path": str(ext_path),
            "scope": scope,
        }

    try:
        ext_dir.mkdir(parents=True, exist_ok=True)
        ext_path.write_text(content, encoding="utf-8")
        ext_path.chmod(ext_path.stat().st_mode | stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        return {
            "status": "ok",
            "message": f"Pi {scope} lifecycle extension installed: {ext_path}",
            "path": str(ext_path),
            "scope": scope,
        }
    except (OSError, PermissionError) as exc:
        return {
            "status": "failed",
            "message": f"Failed to install Pi {scope} extension: {exc}",
            "path": str(ext_path) if ext_path.exists() else None,
            "scope": scope,
        }


def _extension_script(root: Path) -> str:
    """Generate the TypeScript extension source code."""
    root_path = root.resolve()
    scripts_dir = root_path / "shared-knowledge" / "scripts"
    return """\
/**
 * Shared Knowledge Lifecycle Extension
 *
 * Handles two Pi lifecycle events:
 *   1. session_before_compact → run candidate producer (detached)
 *   2. session_compact       → run inbox absorber (detached)
 *
 * Both use `child_process.spawn` with `detached: true` + `.unref()` so they
 * do NOT block Pi's session or event loop.
 *
 * Installed by: shared-knowledge/scripts/hooks/pi_lifecycle.py
 */
import { spawn, type ChildProcess } from "node:child_process";
import { writeFileSync, unlinkSync, existsSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const PRODUCER_SCRIPT = join(""""+str(scripts_dir)+"""", "knowledge_compact_producer.py");
const ABSORBER_SCRIPT = join(""""+str(scripts_dir)+"""", "knowledge_absorb.py");
const PRODUCER_TIMEOUT_MS = 120_000;
const ABSORBER_TIMEOUT_MS = 60_000;

export default function (pi: ExtensionAPI) {
  // -----------------------------------------------------------------------
  // Producer stage: generate inbox candidates before compaction
  // -----------------------------------------------------------------------
  pi.on("session_before_compact", async (event, ctx) => {
    const tempFile = join(ctx.cwd, ".sk-producer-context.jsonl");
    try {
      // Write context to temp file (synchronous, ~1ms)
      const contextJson = JSON.stringify(event.preparation?.messagesToSummarize ?? []);
      writeFileSync(tempFile, contextJson, "utf-8");

      // Spawn producer in detached background process
      const child = spawn(
        "python3",
        [
          PRODUCER_SCRIPT,
          "--root", ctx.cwd,
          "produce-stdin",
        ],
        {
          cwd: ctx.cwd,
          detached: true,
          stdio: ["pipe", "ignore", "pipe"],
          timeout: PRODUCER_TIMEOUT_MS,
        }
      );

      // Pipe context via stdin, then disconnect
      if (child.stdin) {
        child.stdin.end(contextJson);
      }
      child.unref();

      // Clean up temp file after a short delay (producer already has data via stdin)
      setTimeout(() => {
        try { unlinkSync(tempFile); } catch { /* ignore */ }
      }, 5_000);

    } catch (err) {
      console.error("[shared-knowledge-lifecycle] Producer failed:", err);
      try { unlinkSync(tempFile); } catch { /* ignore */ }
    }
    // Return undefined → let default compaction proceed
  });

  // -----------------------------------------------------------------------
  // Absorber stage: absorb inbox candidates after compaction
  // -----------------------------------------------------------------------
  pi.on("session_compact", async (_event, ctx) => {
    try {
      const child = spawn(
        "python3",
        [
          ABSORBER_SCRIPT,
          "--root", ctx.cwd,
          "hook",
          "--format", "json",
        ],
        {
          cwd: ctx.cwd,
          detached: true,
          stdio: "ignore",
          timeout: ABSORBER_TIMEOUT_MS,
        }
      );
      child.unref();
    } catch (err) {
      console.error("[shared-knowledge-lifecycle] Absorber failed:", err);
    }
  });
}
"""


def _legacy_hook_dir(root: Path, scope: str) -> Path:
    """Return the Pi hooks directory for the given scope."""
    if scope == "global":
        return Path.home() / ".pi" / "hooks"
    return root.resolve() / ".pi" / "hooks"


def _install_legacy_hook(root: Path, scope: str) -> dict[str, Any]:
    """Install the legacy post-compact shell hook (deprecated)."""
    hooks_dir = _legacy_hook_dir(root, scope) / "post-compact"
    hook_path = hooks_dir / "shared-knowledge-absorb.sh"
    root_path = root.resolve()
    absorb = root_path / "shared-knowledge" / "scripts" / "knowledge_absorb.py"

    content = f"""#!/usr/bin/env sh
# Pi post-compact hook -- DEPRECATED, use shared-knowledge-lifecycle.ts instead
# Installed by: shared-knowledge/scripts/hooks/pi_lifecycle.py --legacy-hook
set -e
cd "{root_path}"
python3 "{absorb}" hook
"""

    if hook_path.is_file() and hook_path.read_text(encoding="utf-8").strip() == content.strip():
        return {
            "status": "skipped",
            "message": f"Legacy post-compact hook already installed: {hook_path}",
            "path": str(hook_path),
        }

    try:
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook_path.write_text(content, encoding="utf-8")
        hook_path.chmod(hook_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return {
            "status": "ok",
            "message": f"Legacy post-compact hook installed: {hook_path}",
            "path": str(hook_path),
        }
    except (OSError, PermissionError) as exc:
        return {
            "status": "failed",
            "message": f"Failed to install legacy hook: {exc}",
            "path": str(hook_path) if hook_path.exists() else None,
        }
