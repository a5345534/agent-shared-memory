#!/usr/bin/env python3
"""Unified Pi lifecycle hook adapter.

Installs a Pi TypeScript extension that handles both pre-compact candidate
generation (producer) and post-compact inbox absorption (absorber).

The producer calls the LLM directly through Pi's ``complete()`` provider
infrastructure (``@earendil-works/pi-ai/compat``) — no Python subprocess
needed.  The absorber spawns ``knowledge_absorb.py hook`` as a detached
background process.
"""
from __future__ import annotations

import stat
from pathlib import Path
from typing import Any


def install(root: Path, scope: str = "workspace", legacy_hook: bool = False) -> dict[str, Any]:
    if scope not in {"workspace", "global"}:
        return {"status": "failed", "message": f"Unsupported Pi hook scope: {scope}", "path": None}
    pi_dir = Path.home() / ".pi"
    if not pi_dir.is_dir():
        return {"status": "skipped", "message": "Pi harness not detected (~/.pi/ not found).", "path": None}
    results: list[dict[str, Any]] = []
    results.append(_install_extension(root, scope))
    if legacy_hook:
        results.append(_install_legacy_hook(root, scope))
    errors = [r for r in results if r["status"] == "failed"]
    if errors:
        return {"status": "failed", "message": "; ".join(r["message"] for r in errors), "path": results[0].get("path"), "results": results}
    if all(r["status"] == "skipped" for r in results):
        return {"status": "skipped", "message": "; ".join(r["message"] for r in results), "path": results[0].get("path"), "results": results}
    return {"status": "ok", "message": "; ".join(r["message"] for r in results), "path": results[0].get("path"), "results": results}


def _extension_dir(root: Path, scope: str) -> Path:
    if scope == "global":
        return Path.home() / ".pi" / "agent" / "extensions"
    return root.resolve() / ".pi" / "extensions"


def _install_extension(root: Path, scope: str) -> dict[str, Any]:
    ext_dir = _extension_dir(root, scope)
    ext_path = ext_dir / "shared-knowledge-lifecycle.ts"
    content = _extension_script(root)
    if ext_path.is_file() and ext_path.read_text(encoding="utf-8").strip() == content.strip():
        return {"status": "skipped", "message": f"Pi {scope} lifecycle extension already installed: {ext_path}", "path": str(ext_path), "scope": scope}
    try:
        ext_dir.mkdir(parents=True, exist_ok=True)
        ext_path.write_text(content, encoding="utf-8")
        ext_path.chmod(ext_path.stat().st_mode | stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        return {"status": "ok", "message": f"Pi {scope} lifecycle extension installed: {ext_path}", "path": str(ext_path), "scope": scope}
    except (OSError, PermissionError) as exc:
        return {"status": "failed", "message": f"Failed to install Pi {scope} extension: {exc}", "path": str(ext_path) if ext_path.exists() else None, "scope": scope}


def _extension_script(root: Path) -> str:
    """Generate the TypeScript extension source code."""
    scripts_dir = root.resolve() / "shared-knowledge" / "scripts"
    prompts_dir = root.resolve() / "shared-knowledge" / "prompts"
    template = r"""/**
 * Shared Knowledge Lifecycle Extension
 *
 * Lifecycle:
 *   1. session_before_compact -> call LLM via Pi's provider, write inbox
 *   2. session_compact       -> run inbox absorber (detached)
 *
 * Installed by: shared-knowledge/scripts/hooks/pi_lifecycle.py
 */
import { complete } from "@earendil-works/pi-ai/compat";
import {
  serializeConversation,
  convertToLlm,
} from "@earendil-works/pi-coding-agent";
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync, existsSync, readdirSync } from "node:fs";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const PROMPT_FILE = "__PROMPTS_DIR__/compact-review.md";
const ABSORBER_SCRIPT = "__SCRIPTS_DIR__/knowledge_absorb.py";
const ABSORBER_TIMEOUT_MS = 60_000;

// ---------------------------------------------------------------------------
// Candidate helpers (mirror knowledge_compact_producer logic)
// ---------------------------------------------------------------------------
const VALID_MEMORY_TYPES = new Set([
  "architectural-invariant", "reference", "project", "feedback",
]);
const VALID_SCOPE_RE = /^(workspace|module:[a-z0-9][a-z0-9-]*|capability:[a-z0-9][a-z0-9-]*)$/;
const SLUG_RE = /[^a-z0-9]+/g;

function slugify(value: string, fallback = "candidate"): string {
  return value.toLowerCase()
    .replace(/\.md$/, "")
    .replace(SLUG_RE, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80) || fallback;
}

function validateCandidate(c: Record<string, unknown>): string[] {
  const e: string[] = [];
  if (!String(c.name ?? "").trim()) e.push("missing name");
  if (!String(c.description ?? "").trim()) e.push("missing description");
  const t = String(c.type ?? "").trim();
  if (!VALID_MEMORY_TYPES.has(t)) e.push("invalid type: " + t);
  if (!VALID_SCOPE_RE.test(String(c.suggested_scope ?? "").trim())) e.push("invalid suggested_scope");
  if (String(c.body ?? "").trim().length < 20) e.push("body too short (<20 chars)");
  if (!String(c.reason ?? "").trim()) e.push("missing reason");
  if (!String(c.candidate_id ?? "").trim()) e.push("missing candidate_id");
  return e;
}

function renderCandidate(c: Record<string, unknown>): string {
  const today = new Date().toISOString().slice(0, 10);
  const ev = Array.isArray(c.evidence) ? c.evidence.map((x: unknown) => String(x).trim()).filter(Boolean) : [];
  const esc = (v: unknown) => JSON.stringify(String(v ?? "").replace(/\n/g, " ").trim());
  let md  = "---\n";
  md += "name: " + esc(c.name) + "\n";
  md += "description: " + esc(c.description) + "\n";
  md += "type: " + String(c.type ?? "feedback").trim() + "\n";
  md += "suggested_action: retain_memory\n";
  md += "suggested_scope: " + String(c.suggested_scope ?? "workspace").trim() + "\n";
  md += "candidate_id: " + slugify(String(c.candidate_id ?? ""), "candidate") + "\n";
  md += "captured_at: " + today + "\n";
  md += "capture_source: agent:compact-producer\n";
  md += "source: agent:compact-producer\n";
  md += "reason: " + esc(c.reason) + "\n";
  md += "---\n\n";
  md += String(c.body ?? "").trim() + "\n";
  if (ev.length > 0) {
    md += "\n## Evidence\n\n";
    for (const x of ev) md += "- " + x + "\n";
  }
  md += "\n";
  return md;
}

function candidateExists(inboxDir: string, cid: string): boolean {
  if (!existsSync(inboxDir)) return false;
  for (const f of readdirSync(inboxDir)) {
    if (!f.endsWith(".md") || f === "README.md") continue;
    const text = readFileSync(join(inboxDir, f), "utf-8");
    const m = text.match(/^---\n([\s\S]*?)\n---/);
    if (!m) continue;
    for (const line of m[1].split("\n")) {
      if (line.startsWith("candidate_id:")) {
        const id = line.split(":").slice(1).join(":").trim().replace(/^"|"$/g, "");
        if (id === cid) return true;
      }
    }
  }
  return false;
}

// ---------------------------------------------------------------------------
// Extension entry point
// ---------------------------------------------------------------------------
export default function (pi: ExtensionAPI) {
  // -----------------------------------------------------------------------
  // Producer: call LLM via Pi provider, write candidates to inbox
  // -----------------------------------------------------------------------
  pi.on("session_before_compact", async (event, ctx) => {
    const entries = event.preparation?.messagesToSummarize ?? [];
    if (entries.length === 0) return;

    const model = ctx.model;
    if (!model) {
      console.warn("[sk-lifecycle] No active model, skipping producer");
      return;
    }

    ctx.ui.notify("Extracting shared knowledge from session\u2026", "info");

    const auth = await ctx.modelRegistry.getApiKeyAndHeaders(model);
    if (!auth.ok) {
      console.warn("[sk-lifecycle] Auth failed for", model.id);
      return;
    }
    if (!auth.apiKey && (!auth.headers || Object.keys(auth.headers).length === 0)) {
      console.warn("[sk-lifecycle] No credentials for", model.id);
      return;
    }

    ctx.ui.setStatus("sk-producer", "Reviewing session\u2026");

    try {
      const systemPrompt = existsSync(PROMPT_FILE)
        ? readFileSync(PROMPT_FILE, "utf-8")
        : "Extract durable shared-knowledge candidates. Return JSON with a candidates array.";

      const text = serializeConversation(convertToLlm(entries));
      const userMsg = "Review this session and extract durable shared-knowledge candidates.\n\n"
        + text + "\n\nFollow these instructions:\n\n" + systemPrompt;

      const response = await complete(model, {
        messages: [{ role: "user" as const, content: [{ type: "text" as const, text: userMsg }] }],
      }, {
        apiKey: auth.apiKey,
        headers: auth.headers,
        maxTokens: 4096,
        signal: ctx.signal,
      });

      const raw = response.content
        .filter((c): c is { type: "text"; text: string } => c.type === "text")
        .map((c) => c.text).join("\n")
        .replace(/^```(?:json)?\n?/i, "")
        .replace(/\n?```\s*$/, "")
        .trim();

      let candidates: Record<string, unknown>[] = [];
      try { const p = JSON.parse(raw); candidates = Array.isArray(p.candidates) ? p.candidates : []; } catch {}
      if (candidates.length === 0) {
        ctx.ui.notify("Shared knowledge: no durable facts found", "info");
        return;
      }

      const inbox = join(ctx.cwd, "knowledge", "inbox");
      mkdirSync(inbox, { recursive: true });
      const today = new Date().toISOString().slice(0, 10);
      let written = 0;

      for (const c of candidates) {
        const errs = validateCandidate(c);
        if (errs.length > 0) { console.warn("[sk-lifecycle] skip:", errs.join("; ")); continue; }
        const cid = String(c.candidate_id ?? "").trim();
        if (candidateExists(inbox, cid)) continue;
        const dest = join(inbox, today + "-" + slugify(cid) + ".md");
        writeFileSync(dest, renderCandidate(c), "utf-8");
        written++;
      }

      ctx.ui.notify("Shared knowledge: " + written + " candidate(s) written to inbox", "info");
    } catch (err) {
      console.error("[sk-lifecycle] Producer failed:", err);
      ctx.ui.notify("Shared knowledge extraction failed", "error");
    } finally {
      ctx.ui.setStatus("sk-producer", undefined);
    }
  });

  // -----------------------------------------------------------------------
  // Absorber: spawn knowledge_absorb.py hook (detached)
  // -----------------------------------------------------------------------
  pi.on("session_compact", async (_event, ctx) => {
    try {
      const child = spawn("python3", [
        ABSORBER_SCRIPT, "--root", ctx.cwd, "hook", "--format", "json",
      ], { cwd: ctx.cwd, detached: true, stdio: "ignore", timeout: ABSORBER_TIMEOUT_MS });
      child.unref();
    } catch (err) {
      console.error("[sk-lifecycle] Absorber failed:", err);
    }
  });
};
"""
    # Replace path placeholders with actual resolved paths (no escaping issues)
    result = template.replace("__PROMPTS_DIR__", str(prompts_dir))
    result = result.replace("__SCRIPTS_DIR__", str(scripts_dir))
    return result


def _legacy_hook_dir(root: Path, scope: str) -> Path:
    if scope == "global":
        return Path.home() / ".pi" / "hooks"
    return root.resolve() / ".pi" / "hooks"


def _install_legacy_hook(root: Path, scope: str) -> dict[str, Any]:
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
        return {"status": "skipped", "message": f"Legacy hook already installed: {hook_path}", "path": str(hook_path)}
    try:
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook_path.write_text(content, encoding="utf-8")
        hook_path.chmod(hook_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return {"status": "ok", "message": f"Legacy hook installed: {hook_path}", "path": str(hook_path)}
    except (OSError, PermissionError) as exc:
        return {"status": "failed", "message": f"Failed to install legacy hook: {exc}", "path": str(hook_path) if hook_path.exists() else None}
