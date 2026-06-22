#!/usr/bin/env python3
"""Lint workspace knowledge surfaces.

This script intentionally uses only the Python standard library so it can run
from a fresh checkout without bootstrapping project dependencies.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote


VALID_MEMORY_TYPES = {
    "feedback",
    "project",
    "reference",
    "user",
    "architectural-invariant",
    "deprecated",
}
VALID_MEMORY_SCOPE_RE = re.compile(r"^(workspace|module:[a-z0-9][a-z0-9-]*|capability:[a-z0-9][a-z0-9-]*)$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
PATH_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])"
    r"((?:AGENTS\.md|docs|knowledge|agent-workspace|projects|docker)"
    r"[A-Za-z0-9_./#-]*)"
)
DEFAULT_STALENESS_THRESHOLD = 25
DEFAULT_INBOX_MAX_AGE_DAYS = 14
DEFAULT_INBOX_MAX_COUNT = 20
DEFAULT_WORKSPACE_MAX_COUNT = 20
SKIP_DIR_NAMES = {
    ".git",
    ".worktrees",
    "target",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
}


@dataclasses.dataclass
class Finding:
    severity: str
    check_id: str
    surface: str
    location: str
    description: str
    fix_hint: str


def find_workspace_root(start: Path) -> Path:
    """Locate the workspace root by finding AGENTS.md."""
    current = start.resolve()
    while True:
        if (current / "AGENTS.md").exists():
            return current
        if current.parent == current:
            raise SystemExit("Could not find workspace root (no AGENTS.md found)")
        current = current.parent


def rel(root: Path, path: Path) -> str:
    root_abs = root.resolve()
    path_abs = path if path.is_absolute() else root / path
    try:
        return path_abs.absolute().relative_to(root_abs).as_posix()
    except ValueError:
        return path.as_posix()


def add_finding(findings: list[Finding], severity: str, check_id: str, surface: str, location: str, description: str, fix_hint: str) -> None:
    findings.append(Finding(severity, check_id, surface, location, description, fix_hint))


def iter_files(root: Path, suffixes: tuple[str, ...]) -> Iterable[Path]:
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIR_NAMES]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix in suffixes:
                yield path


def markdown_link_targets(text: str) -> Iterable[tuple[str, int]]:
    code_fence_lines: set[int] = set()
    in_fence = False
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            code_fence_lines.add(number)
            in_fence = not in_fence
            continue
        if in_fence:
            code_fence_lines.add(number)

    line_starts = [0]
    for match in re.finditer(r"\n", text):
        line_starts.append(match.end())

    def line_for_index(index: int) -> int:
        lo = 0
        hi = len(line_starts) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if line_starts[mid] <= index:
                lo = mid + 1
            else:
                hi = mid - 1
        return hi + 1

    for match in MARKDOWN_LINK_RE.finditer(text):
        line_number = line_for_index(match.start())
        if line_number in code_fence_lines:
            continue
        line_start = line_starts[line_number - 1]
        line_end = text.find("\n", match.start())
        if line_end == -1:
            line_end = len(text)
        line_prefix = text[line_start : match.start()]
        line_suffix = text[match.end() : line_end]
        if line_prefix.count("`") % 2 == 1 and line_suffix.count("`") % 2 == 1:
            continue
        yield match.group(1), line_number


def normalize_link_target(raw: str) -> str | None:
    target = unquote(raw.strip().strip("<>"))
    if not target or target.startswith("#"):
        return None
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):
        return None
    if " " in target and not Path(target).exists():
        target = target.split(" ", 1)[0].strip().strip("<>")
    target = target.split("#", 1)[0].strip()
    return target or None


def resolve_local_link(root: Path, source: Path, raw_target: str) -> Path | None:
    target = normalize_link_target(raw_target)
    if not target:
        return None
    if target.startswith("/"):
        return root / target.lstrip("/")
    relative = (source.parent / target).resolve()
    if relative.exists():
        return relative
    if not target.startswith(("./", "../")):
        root_relative = root / target
        if root_relative.exists():
            return root_relative.resolve()
    return relative

def memory_files(root: Path) -> list[Path]:
    shared = root / "knowledge/shared-memory"
    files = []
    for path in iter_files(shared, (".md",)):
        name = path.name
        rel_path = rel(root, path)
        if name in {"README.md", "MEMORY.md"}:
            continue
        if rel_path.startswith("knowledge/shared-memory/inbox/"):
            continue
        files.append(path)
    return sorted(files)


def inbox_candidate_files(root: Path) -> list[Path]:
    inbox = root / "knowledge/shared-memory/inbox"
    if not inbox.exists():
        return []
    return sorted(path for path in inbox.glob("*.md") if path.name != "README.md")


def workspace_memory_files(root: Path) -> list[Path]:
    workspace = root / "knowledge/shared-memory/workspace"
    if not workspace.exists():
        return []
    return sorted(path for path in workspace.glob("*.md") if path.name not in {"README.md", "MEMORY.md"})


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def expected_memory_scope(root: Path, path: Path) -> str | None:
    relative = path.relative_to(root / "knowledge/shared-memory")
    parts = relative.parts
    if not parts:
        return None
    if parts[0] == "workspace" and len(parts) == 2:
        return "workspace"
    if parts[0] == "module" and len(parts) >= 3:
        return f"module:{parts[1]}"
    if parts[0] == "capability" and len(parts) >= 3:
        return f"capability:{parts[1]}"
    return None


def inbox_candidate_date(path: Path, frontmatter: dict[str, str]) -> dt.date:
    captured_at = frontmatter.get("captured_at") or frontmatter.get("verified_at") or ""
    if ISO_DATE_RE.match(captured_at):
        return dt.date.fromisoformat(captured_at)
    return dt.datetime.fromtimestamp(path.stat().st_mtime).date()


def check_inbox_candidates(root: Path, findings: list[Finding]) -> None:
    inbox_files = inbox_candidate_files(root)
    inbox_max_count = env_int("SHARED_MEMORY_INBOX_MAX_COUNT", DEFAULT_INBOX_MAX_COUNT)
    inbox_max_age_days = env_int("SHARED_MEMORY_INBOX_MAX_AGE_DAYS", DEFAULT_INBOX_MAX_AGE_DAYS)
    today = dt.date.today()

    if len(inbox_files) > inbox_max_count:
        add_finding(
            findings,
            "warn",
            "memory-inbox-volume",
            "shared-memory",
            "knowledge/shared-memory/inbox",
            f"Shared-memory inbox contains {len(inbox_files)} candidates; threshold is {inbox_max_count}.",
            "Run the absorption workflow: python3 scripts/knowledge_absorb.py hook",
        )

    for path in inbox_files:
        text = path.read_text(encoding="utf-8")
        frontmatter, _ = parse_frontmatter(text)
        location = rel(root, path)
        for field in ("candidate_id", "captured_at", "capture_source", "source", "suggested_scope", "name", "description"):
            if not frontmatter.get(field):
                add_finding(
                    findings,
                    "warn",
                    "memory-frontmatter-invalid",
                    "shared-memory",
                    location,
                    f"Inbox candidate missing required field: {field}",
                    "Regenerate or repair the inbox candidate before absorption.",
                )
        age_days = (today - inbox_candidate_date(path, frontmatter)).days
        if age_days > inbox_max_age_days:
            add_finding(
                findings,
                "warn",
                "memory-inbox-aging",
                "shared-memory",
                location,
                f"Inbox candidate is {age_days} days old; threshold is {inbox_max_age_days}.",
                "Run the absorption workflow instead of leaving candidates in inbox.",
            )


def check_workspace_memory_pressure(root: Path, findings: list[Finding], workspace_count: int) -> None:
    workspace_max_count = env_int("SHARED_MEMORY_WORKSPACE_MAX_COUNT", DEFAULT_WORKSPACE_MAX_COUNT)
    if workspace_count > workspace_max_count:
        add_finding(
            findings,
            "warn",
            "memory-workspace-volume",
            "shared-memory",
            "knowledge/shared-memory/workspace",
            f"Curated workspace shared-memory has {workspace_count} active entries; recommended threshold is {workspace_max_count}.",
            "Run knowledge_absorb.py plan --include-workspace-backlog and move/promote/deprecate entries through the absorption workflow.",
        )


def build_pressure_summary(root: Path) -> dict[str, object]:
    inbox_files = inbox_candidate_files(root)
    today = dt.date.today()
    oldest_age = 0
    oldest_path = None
    for path in inbox_files:
        frontmatter, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        age = max(0, (today - inbox_candidate_date(path, frontmatter)).days)
        if age >= oldest_age:
            oldest_age = age
            oldest_path = rel(root, path)

    active_workspace_count = 0
    for path in workspace_memory_files(root):
        frontmatter, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        if frontmatter.get("type") != "deprecated":
            active_workspace_count += 1

    thresholds = {
        "inboxMaxAgeDays": env_int("SHARED_MEMORY_INBOX_MAX_AGE_DAYS", DEFAULT_INBOX_MAX_AGE_DAYS),
        "inboxMaxCount": env_int("SHARED_MEMORY_INBOX_MAX_COUNT", DEFAULT_INBOX_MAX_COUNT),
        "workspaceMaxCount": env_int("SHARED_MEMORY_WORKSPACE_MAX_COUNT", DEFAULT_WORKSPACE_MAX_COUNT),
    }
    reasons = []
    if len(inbox_files) > thresholds["inboxMaxCount"]:
        reasons.append(f"inbox_count {len(inbox_files)} > {thresholds['inboxMaxCount']}")
    if inbox_files and oldest_age > thresholds["inboxMaxAgeDays"]:
        reasons.append(f"oldest_inbox_age_days {oldest_age} > {thresholds['inboxMaxAgeDays']}")
    if active_workspace_count > thresholds["workspaceMaxCount"]:
        reasons.append(f"workspace_memory_count {active_workspace_count} > {thresholds['workspaceMaxCount']}")

    return {
        "triggered": bool(reasons),
        "reasons": reasons,
        "thresholds": thresholds,
        "metrics": {
            "inboxCount": len(inbox_files),
            "oldestInboxAgeDays": oldest_age if inbox_files else None,
            "oldestInboxPath": oldest_path,
            "workspaceMemoryCount": active_workspace_count,
        },
    }


def check_shared_memory(root: Path, findings: list[Finding], fixes: dict[Path, str], staleness_threshold: int) -> None:
    shared_root = root / "knowledge/shared-memory"
    workspace_index = shared_root / "workspace/MEMORY.md"
    workspace_index_text = workspace_index.read_text(encoding="utf-8") if workspace_index.exists() else ""

    check_inbox_candidates(root, findings)
    active_workspace_count = 0

    if workspace_index.exists():
        for raw_target, line in markdown_link_targets(workspace_index_text):
            resolved = resolve_local_link(root, workspace_index, raw_target)
            if resolved and not resolved.exists():
                add_finding(
                    findings,
                    "error",
                    "memory-index-orphan",
                    "shared-memory",
                    f"{rel(root, workspace_index)}:{line}",
                    f"Shared-memory index link target is missing: {raw_target}",
                    "Update or remove the stale index entry.",
                )

    missing_workspace_index_lines: list[tuple[Path, dict[str, str]]] = []

    for path in memory_files(root):
        text = path.read_text(encoding="utf-8")
        frontmatter, body = parse_frontmatter(text)
        location = rel(root, path)
        expected_scope = expected_memory_scope(root, path)

        for field in ("name", "description", "type", "scope", "verified_at", "source"):
            if not frontmatter.get(field):
                add_finding(
                    findings,
                    "warn",
                    "memory-frontmatter-invalid",
                    "shared-memory",
                    location,
                    f"Missing required frontmatter field: {field}",
                    "Add the required shared-memory frontmatter field.",
                )

        memory_type = frontmatter.get("type", "")
        memory_scope = frontmatter.get("scope", "")
        verified_at = frontmatter.get("verified_at", "")

        if memory_type and memory_type not in VALID_MEMORY_TYPES:
            add_finding(
                findings,
                "warn",
                "memory-frontmatter-invalid",
                "shared-memory",
                location,
                f"Invalid shared-memory type: {memory_type}",
                f"Use one of: {', '.join(sorted(VALID_MEMORY_TYPES))}.",
            )
        if memory_scope and not VALID_MEMORY_SCOPE_RE.match(memory_scope):
            add_finding(
                findings,
                "warn",
                "memory-frontmatter-invalid",
                "shared-memory",
                location,
                f"Invalid shared-memory scope: {memory_scope}",
                "Use workspace, module:<name>, or capability:<name>.",
            )
        if expected_scope and memory_scope and memory_scope != expected_scope:
            add_finding(
                findings,
                "warn",
                "memory-frontmatter-invalid",
                "shared-memory",
                location,
                f"Scope {memory_scope} does not match path-implied scope {expected_scope}",
                "Move the file or update frontmatter so scope and path agree.",
            )
        if verified_at and not ISO_DATE_RE.match(verified_at):
            add_finding(
                findings,
                "warn",
                "memory-frontmatter-invalid",
                "shared-memory",
                location,
                f"verified_at is not an ISO date: {verified_at}",
                "Use YYYY-MM-DD.",
            )

        source = frontmatter.get("source", "")
        if source and re.match(r"^agent:(codex|pi|claude|openai|gemini)-", source):
            evidence_section = re.search(r"(?im)^##\s+Evidence\b[\s\S]*?^- ", body)
            if not evidence_section:
                add_finding(
                    findings,
                    "warn",
                    "memory-postcompact-evidence",
                    "shared-memory",
                    location,
                    "Agent-generated memory entry lacks a concrete Evidence section.",
                    "Manually review the entry and add evidence, narrow the scope, or deprecate it.",
                )

        if expected_scope == "workspace" and memory_type != "deprecated":
            active_workspace_count += 1
            if path.name not in workspace_index_text and rel(root, path) not in workspace_index_text:
                add_finding(
                    findings,
                    "warn",
                    "memory-file-unindexed",
                    "shared-memory",
                    location,
                    "Workspace shared-memory entry is missing from MEMORY.md.",
                    "Add a concise index line under the appropriate MEMORY.md section.",
                )
                missing_workspace_index_lines.append((path, frontmatter))

        if memory_type != "deprecated" and re.search(r"(?i)(promoted to|superseded by|stronger authority|moved to module docs|moved to skill)", body):
            add_finding(
                findings,
                "info",
                "memory-promoted-not-retired",
                "shared-memory",
                location,
                "Shared-memory entry appears promoted or superseded but remains active.",
                "Convert it to a concise pointer or mark it deprecated.",
            )

        check_memory_staleness(root, path, frontmatter, text, findings, staleness_threshold)

    check_workspace_memory_pressure(root, findings, active_workspace_count)

    if missing_workspace_index_lines and workspace_index.exists():
        fixes[workspace_index] = render_workspace_memory_index_fix(workspace_index_text, missing_workspace_index_lines)


def render_workspace_memory_index_fix(index_text: str, missing: list[tuple[Path, dict[str, str]]]) -> str:
    lines = []
    for path, frontmatter in missing:
        name = frontmatter.get("name") or path.stem.replace("-", " ").title()
        description = frontmatter.get("description") or "Unindexed shared-memory entry."
        lines.append(f"- [{name}]({path.name}) — {description}")

    if not lines:
        return index_text

    heading = "## Pitfalls / Operational Boundaries"
    marker = re.search(rf"^{re.escape(heading)}\s*$", index_text, flags=re.MULTILINE)
    if not marker:
        return index_text.rstrip() + "\n\n## Unindexed shared-memory entries\n\n" + "\n".join(lines) + "\n"

    next_heading = re.search(r"^##\s+", index_text[marker.end() :], flags=re.MULTILINE)
    insert_at = marker.end() + (next_heading.start() if next_heading else len(index_text[marker.end() :]))
    before = index_text[:insert_at].rstrip()
    after = index_text[insert_at:].lstrip("\n")
    return before + "\n" + "\n".join(lines) + "\n\n" + after


def extract_referenced_paths(root: Path, source: Path, text: str) -> set[Path]:
    candidates: set[Path] = set()
    for raw_target, _line in markdown_link_targets(text):
        resolved = resolve_local_link(root, source, raw_target)
        if resolved and resolved.exists():
            candidates.add(resolved)

    for match in PATH_TOKEN_RE.finditer(text):
        raw = match.group(1).split("#", 1)[0]
        path = root / raw
        if path.exists():
            candidates.add(path.resolve())

    return {path for path in candidates if path != source.resolve()}


def check_memory_staleness(root: Path, path: Path, frontmatter: dict[str, str], text: str, findings: list[Finding], threshold: int) -> None:
    if threshold <= 0:
        return

    verified_at = frontmatter.get("verified_at", "")
    if ISO_DATE_RE.match(verified_at):
        since = verified_at
    else:
        since = dt.datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()

    referenced = sorted(extract_referenced_paths(root, path, text))
    if not referenced:
        return

    for referenced_path in referenced[:20]:
        git_path = rel(root, referenced_path)
        result = subprocess.run(
            ["git", "log", f"--since={since}", "--format=%H", "--", git_path],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        count = len([line for line in result.stdout.splitlines() if line.strip()])
        if count >= threshold:
            add_finding(
                findings,
                "info",
                "memory-staleness",
                "shared-memory",
                rel(root, path),
                f"Referenced path {git_path} changed {count} commits since {since}.",
                "Re-verify the memory before relying on it; update verified_at if still current.",
            )


def check_module_map(root: Path, findings: list[Finding]) -> None:
    module_map_root = root / "knowledge/module-map"
    if not module_map_root.exists():
        return

    existing_pages = {
        path.stem
        for path in module_map_root.glob("*.md")
        if path.name not in {"README.md", "index.md"}
    }

    active = set()
    backend_root = root / "projects/backend/module"
    if backend_root.exists():
        for child in backend_root.iterdir():
            if child.is_dir() and child.name.endswith("-module") and (child / "RESPONSIBILITY.md").exists():
                active.add(child.name.removesuffix("-module"))
    frontend = root / "projects/frontend"
    if frontend.exists():
        for child in frontend.iterdir():
            if child.is_dir() and (child / "RESPONSIBILITY.md").exists():
                active.add(child.name)

    for module in sorted(active - existing_pages):
        add_finding(
            findings,
            "error",
            "module-map-orphan",
            "module-map",
            f"knowledge/module-map/{module}.md",
            f"Active module {module} has no module-map page.",
            "Add a module-map page or remove the module from active topology if it is retired.",
        )

    for page in sorted(existing_pages - active):
        add_finding(
            findings,
            "warn",
            "module-map-orphan",
            "module-map",
            f"knowledge/module-map/{page}.md",
            f"Module-map page {page}.md has no matching active module directory.",
            "Move it under concepts/ if it is conceptual, or remove/archive it if obsolete.",
        )


def check_markdown_links(root: Path, files: Iterable[Path], check_id: str, surface: str, findings: list[Finding]) -> None:
    for path in sorted(set(files)):
        if not path.exists() or path.is_symlink():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for raw_target, line in markdown_link_targets(text):
            resolved = resolve_local_link(root, path, raw_target)
            if resolved and not resolved.exists():
                add_finding(
                    findings,
                    "error",
                    check_id,
                    surface,
                    f"{rel(root, path)}:{line}",
                    f"Local markdown link target is missing: {raw_target}",
                    "Update the link target or remove the stale reference.",
                )


def workspace_guidance_files(root: Path) -> list[Path]:
    files = []
    agents = root / "AGENTS.md"
    if agents.exists():
        files.append(agents)
    files.extend(iter_files(root / "docs", (".md",)))
    files.extend(iter_files(root / "agent-workspace", (".md", ".json")))
    return files


def check_knowledge_viewport(root: Path, findings: list[Finding]) -> None:
    readme = root / "knowledge/README.md"
    if not readme.exists():
        add_finding(
            findings,
            "info",
            "vault-link-missing",
            "knowledge-viewport",
            "knowledge/README.md",
            "Knowledge viewport README is missing.",
            "Add README guidance for shared-memory, module-map, module symlinks.",
        )


def render_fix_diff(path: Path, old: str, new: str, root: Path) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{rel(root, path)}",
            tofile=f"b/{rel(root, path)}",
        )
    )


def output_findings(findings: list[Finding], args: argparse.Namespace) -> None:
    if args.format == "json":
        print(json.dumps([dataclasses.asdict(finding) for finding in findings], ensure_ascii=False, indent=2))
        return

    counts = {severity: 0 for severity in ("error", "warn", "info")}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    print(f"Knowledge lint: {counts['error']} error / {counts['warn']} warn / {counts['info']} info")

    visible = findings if args.include_info else [finding for finding in findings if finding.severity != "info"]
    if not visible:
        if findings and not args.include_info:
            print("Info findings hidden; rerun with --include-info to inspect them.")
        return

    for finding in visible:
        print(f"[{finding.severity}] {finding.surface} {finding.check_id} {finding.location}")
        print(f"  {finding.description}")
        print(f"  fix: {finding.fix_hint}")
# ---------------------------------------------------------------------------
# Follow-up artifact validation — merged from goal-runner implementation
# ---------------------------------------------------------------------------
FOLLOWUP_VERSION = "1"
LINT_VERSION = "1"

FOLLOWUP_KINDS = {"skill_followup", "module_doc_followup"}
FOLLOWUP_STATUSES = {"open", "in_progress", "done", "rejected", "superseded"}
FOLLOWUP_SOURCE_ACTIONS = {"promote_to_skill", "promote_to_module_doc"}
FOLLOWUP_HANDOFFS = {"skill-creator", "doc-writer"}

FOLLOWUP_REQUIRED_FIELDS = [
    "version",
    "kind",
    "status",
    "createdAt",
    "sourceCandidate",
    "sourceAction",
    "suggestedDestination",
    "title",
    "reason",
    "evidence",
    "confidence",
    "safeToAutoApply",
    "handoffTo",
]

# These follow-up status values designate "active" follow-ups for aging checks.
ACTIVE_FOLLOWUP_STATUSES = {"open", "in_progress"}

DEFAULT_FOLLOWUP_MAX_AGE_DAYS = 30

# Query index constants (mirrored from knowledge_query.py for self-containment)
SCAN_DIRS = ("workspace", "module", "capability")
SKIP_DIRS = {"inbox", "followups", ".index"}
SKIP_FILES = {"README.md", "MEMORY.md"}
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today() -> dt.date:
    return dt.date.today()


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_flag(name: str) -> bool:
    """Return True if the environment variable is set to '1'."""
    return os.environ.get(name) == "1"


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter subset from Markdown text.

    Handles top-level scalar string fields and simple list fields using
    ``  - item`` syntax. Returns (frontmatter_dict, body_text).
    """
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text.strip()

    frontmatter: dict[str, Any] = {}
    current_key: str | None = None
    for raw_line in match.group(1).splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if line.startswith("  - ") and current_key is not None:
            value = line[4:].strip().strip('"')
            existing = frontmatter.setdefault(current_key, [])
            if isinstance(existing, list):
                existing.append(value)
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        current_key = key
        if value == "":
            frontmatter[key] = []
        else:
            frontmatter[key] = value.strip('"')
    return frontmatter, text[match.end():].strip()


def parse_scope(scope: str) -> tuple[str, str]:
    """Return (scope_type, scope_name) from a scope string."""
    scope = scope.strip()
    if scope == "workspace":
        return "workspace", ""
    if scope.startswith("module:"):
        return "module", scope[len("module:"):].strip()
    if scope.startswith("capability:"):
        return "capability", scope[len("capability:"):].strip()
    return "workspace", ""


def parse_iso_date(s: str) -> dt.date | None:
    """Parse an ISO 8601 date (YYYY-MM-DD) string."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def parse_iso_datetime_date(s: str) -> dt.date | None:
    """Extract the date portion from an ISO 8601 datetime string like '2026-06-22T14:46:52Z'."""
    # Try YYYY-MM-DD first
    date = parse_iso_date(s)
    if date:
        return date
    # Try ISO datetime: YYYY-MM-DDTHH:MM:SS...
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    if m:
        return parse_iso_date(m.group(1))
    return None


# ---------------------------------------------------------------------------
# Follow-up file discovery
# ---------------------------------------------------------------------------

def followup_dirs(root: Path) -> list[Path]:
    """Return existing follow-up kind directories under knowledge/shared-memory/followups/."""
    followups_root = root / "knowledge" / "shared-memory" / "followups"
    if not followups_root.exists():
        return []
    result: list[Path] = []
    for entry in sorted(followups_root.iterdir()):
        if entry.is_dir() and entry.name != "README.md":
            result.append(entry)
    return result


def collect_followup_files(root: Path) -> list[Path]:
    """Collect all .json follow-up artifact files sorted by path."""
    files: list[Path] = []
    for kind_dir in followup_dirs(root):
        for json_file in sorted(kind_dir.glob("*.json")):
            if json_file.name.endswith(".json"):
                files.append(json_file)
    return files


# ---------------------------------------------------------------------------
# Follow-up validation
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class LintIssue:
    level: str        # "error" or "warning"
    code: str         # e.g. "followup-json-valid"
    path: str         # relative path to the artifact
    detail: str       # human-readable description


def check_required_fields(data: dict[str, Any], art_path: str) -> list[LintIssue]:
    """Check that all required follow-up fields are present and non-null."""
    issues: list[LintIssue] = []
    for field in FOLLOWUP_REQUIRED_FIELDS:
        if field not in data or data[field] is None:
            issues.append(LintIssue(
                level="error",
                code="followup-missing-field",
                path=art_path,
                detail=f"Required field '{field}' is missing or null",
            ))
    return issues


def check_kind(data: dict[str, Any], art_path: str) -> list[LintIssue]:
    """Validate the 'kind' field against known follow-up kinds."""
    kind = data.get("kind")
    if kind is not None and kind not in FOLLOWUP_KINDS:
        return [LintIssue(
            level="error",
            code="followup-kind-valid",
            path=art_path,
            detail=f"Invalid kind '{kind}'. Must be one of: {', '.join(sorted(FOLLOWUP_KINDS))}",
        )]
    return []


def check_status(data: dict[str, Any], art_path: str) -> list[LintIssue]:
    """Validate the 'status' field against known lifecycle values."""
    status = data.get("status")
    if status is not None and status not in FOLLOWUP_STATUSES:
        return [LintIssue(
            level="error",
            code="followup-status-valid",
            path=art_path,
            detail=f"Invalid status '{status}'. Must be one of: {', '.join(sorted(FOLLOWUP_STATUSES))}",
        )]
    return []


def check_source_action(data: dict[str, Any], art_path: str) -> list[LintIssue]:
    """Validate the 'sourceAction' field."""
    if "sourceAction" not in data or data["sourceAction"] is None:
        return []  # handled by required fields check
    action = data["sourceAction"]
    if action not in FOLLOWUP_SOURCE_ACTIONS:
        return [LintIssue(
            level="error",
            code="followup-source-action-valid",
            path=art_path,
            detail=f"Invalid sourceAction '{action}'. Must be one of: {', '.join(sorted(FOLLOWUP_SOURCE_ACTIONS))}",
        )]
    return []


def check_handoff_to(data: dict[str, Any], art_path: str) -> list[LintIssue]:
    """Validate the 'handoffTo' field."""
    if "handoffTo" not in data or data["handoffTo"] is None:
        return []  # handled by required fields check
    handoff = data["handoffTo"]
    if handoff not in FOLLOWUP_HANDOFFS:
        return [LintIssue(
            level="error",
            code="followup-handoff-valid",
            path=art_path,
            detail=f"Invalid handoffTo '{handoff}'. Must be one of: {', '.join(sorted(FOLLOWUP_HANDOFFS))}",
        )]
    return []


def check_confidence(data: dict[str, Any], art_path: str) -> list[LintIssue]:
    """Validate the 'confidence' field is a number between 0 and 1."""
    if "confidence" not in data:
        return []  # handled by required fields check
    confidence = data["confidence"]
    if confidence is None:
        return []
    if not isinstance(confidence, (int, float)):
        return [LintIssue(
            level="error",
            code="followup-confidence-valid",
            path=art_path,
            detail=f"confidence must be a number, got {type(confidence).__name__}",
        )]
    if confidence < 0 or confidence > 1:
        return [LintIssue(
            level="warning",
            code="followup-confidence-range",
            path=art_path,
            detail=f"confidence {confidence} is outside expected range [0.0, 1.0]",
        )]
    return []


def check_source_candidate_exists(root: Path, data: dict[str, Any], art_path: str) -> list[LintIssue]:
    """Check that sourceCandidate path exists in the workspace."""
    source = data.get("sourceCandidate")
    if not source:
        return []  # handled by required fields check
    source_path = root / source
    if not source_path.exists():
        return [LintIssue(
            level="error",
            code="followup-source-candidate-missing",
            path=art_path,
            detail=f"sourceCandidate '{source}' does not exist in workspace",
        )]
    return []


def check_suggested_destination(data: dict[str, Any], art_path: str) -> list[LintIssue]:
    """Check that suggestedDestination is non-empty."""
    dest = data.get("suggestedDestination")
    if dest is not None and not isinstance(dest, str):
        return [LintIssue(
            level="error",
            code="followup-destination-invalid",
            path=art_path,
            detail="suggestedDestination must be a string",
        )]
    if isinstance(dest, str) and not dest.strip():
        return [LintIssue(
            level="error",
            code="followup-destination-invalid",
            path=art_path,
            detail="suggestedDestination is empty",
        )]
    return []


def check_done_outputs(data: dict[str, Any], art_path: str) -> list[LintIssue]:
    """Check that status=done follow-ups have non-empty outputs."""
    status = data.get("status")
    if status != "done":
        return []
    outputs = data.get("outputs")
    if not outputs or not isinstance(outputs, list) or len(outputs) == 0:
        return [LintIssue(
            level="error",
            code="followup-done-without-outputs",
            path=art_path,
            detail="status is 'done' but outputs array is missing or empty",
        )]
    # Validate each output entry has required fields
    issues: list[LintIssue] = []
    for i, entry in enumerate(outputs):
        if not isinstance(entry, dict):
            issues.append(LintIssue(
                level="error",
                code="followup-outputs-invalid",
                path=art_path,
                detail=f"outputs[{i}] is not a JSON object",
            ))
        else:
            if "path" not in entry or not entry.get("path"):
                issues.append(LintIssue(
                    level="error",
                    code="followup-outputs-invalid",
                    path=art_path,
                    detail=f"outputs[{i}] is missing required 'path' field",
                ))
            if "description" not in entry or not entry.get("description"):
                issues.append(LintIssue(
                    level="error",
                    code="followup-outputs-invalid",
                    path=art_path,
                    detail=f"outputs[{i}] is missing required 'description' field",
                ))
    return issues


def check_evidence(data: dict[str, Any], art_path: str) -> list[LintIssue]:
    """Check that evidence is a non-empty array."""
    evidence = data.get("evidence")
    if evidence is not None:
        if not isinstance(evidence, list):
            return [LintIssue(
                level="error",
                code="followup-evidence-invalid",
                path=art_path,
                detail="evidence must be an array",
            )]
        if len(evidence) == 0:
            return [LintIssue(
                level="warning",
                code="followup-evidence-empty",
                path=art_path,
                detail="evidence array is empty",
            )]
    return []


def check_version(data: dict[str, Any], art_path: str) -> list[LintIssue]:
    """Check that version field is present and is a string."""
    version = data.get("version")
    if version is not None and not isinstance(version, str):
        return [LintIssue(
            level="error",
            code="followup-version-invalid",
            path=art_path,
            detail="version must be a string",
        )]
    return []


def check_created_at(data: dict[str, Any], art_path: str) -> list[LintIssue]:
    """Check that createdAt is a valid ISO datetime string."""
    created = data.get("createdAt")
    if created is not None:
        if not isinstance(created, str):
            return [LintIssue(
                level="error",
                code="followup-created-at-invalid",
                path=art_path,
                detail="createdAt must be a string",
            )]
        if parse_iso_datetime_date(created) is None:
            return [LintIssue(
                level="warning",
                code="followup-created-at-parse",
                path=art_path,
                detail=f"createdAt '{created}' could not be parsed as ISO datetime",
            )]
    return []


def check_safe_to_auto_apply(data: dict[str, Any], art_path: str) -> list[LintIssue]:
    """Check that safeToAutoApply is a boolean."""
    if "safeToAutoApply" in data and data["safeToAutoApply"] is not None:
        if not isinstance(data["safeToAutoApply"], bool):
            # Also accept int 0/1 for leniency
            return [LintIssue(
                level="error",
                code="followup-safe-to-auto-apply-invalid",
                path=art_path,
                detail="safeToAutoApply must be a boolean",
            )]
    return []


def validate_followup(root: Path, art_path: Path) -> list[LintIssue]:
    """Validate a single follow-up artifact JSON file.

    Returns a list of LintIssue objects (errors and warnings).
    """
    art_rel = rel(root, art_path)
    issues: list[LintIssue] = []

    # 1. JSON parse check
    try:
        data = json.loads(art_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        issues.append(LintIssue(
            level="error",
            code="followup-json-valid",
            path=art_rel,
            detail=f"Failed to parse JSON: {exc}",
        ))
        return issues  # Cannot continue without valid JSON
    except OSError as exc:
        issues.append(LintIssue(
            level="error",
            code="followup-json-valid",
            path=art_rel,
            detail=f"Failed to read file: {exc}",
        ))
        return issues

    if not isinstance(data, dict):
        issues.append(LintIssue(
            level="error",
            code="followup-json-valid",
            path=art_rel,
            detail="Root JSON value must be an object (dict)",
        ))
        return issues

    # 2. Required fields
    issues.extend(check_required_fields(data, art_rel))

    # 3. version check
    issues.extend(check_version(data, art_rel))

    # 4. kind validation
    issues.extend(check_kind(data, art_rel))

    # 5. status validation
    issues.extend(check_status(data, art_rel))

    # 6. sourceAction validation
    issues.extend(check_source_action(data, art_rel))

    # 7. handoffTo validation
    issues.extend(check_handoff_to(data, art_rel))

    # 8. confidence validation
    issues.extend(check_confidence(data, art_rel))

    # 9. createdAt parse check
    issues.extend(check_created_at(data, art_rel))

    # 10. evidence check
    issues.extend(check_evidence(data, art_rel))

    # 11. safeToAutoApply type check
    issues.extend(check_safe_to_auto_apply(data, art_rel))

    # 12. sourceCandidate path existence
    issues.extend(check_source_candidate_exists(root, data, art_rel))

    # 13. suggestedDestination non-empty
    issues.extend(check_suggested_destination(data, art_rel))

    # 14. done-without-outputs
    issues.extend(check_done_outputs(data, art_rel))

    return issues


def check_followup_aging(root: Path, art_path: Path, max_age_days: int) -> list[LintIssue]:
    """Check if a follow-up artifact has been open/in_progress beyond max_age_days.

    Returns warnings only (never errors).
    """
    art_rel = rel(root, art_path)
    try:
        data = json.loads(art_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []  # JSON validity errors handled by validate_followup

    if not isinstance(data, dict):
        return []

    status = data.get("status")
    if status not in ACTIVE_FOLLOWUP_STATUSES:
        return []

    created = data.get("createdAt")
    if not created:
        return []

    created_date = parse_iso_datetime_date(created)
    if created_date is None:
        return []

    age_days = (today() - created_date).days
    if age_days > max_age_days:
        return [LintIssue(
            level="warning",
            code="followup-aging",
            path=art_rel,
            detail=f"Follow-up is {age_days} days old (status={status}, max={max_age_days}d). Consider updating or closing.",
        )]
    return []


# ---------------------------------------------------------------------------
# Query index checks
# ---------------------------------------------------------------------------

def collect_curated_entries_for_lint(root: Path) -> list[dict[str, Any]]:
    """Scan curated shared memory entries and return normalized source data.

    Mirrors knowledge_query.collect_curated_entries but returns dicts
    instead of MemoryEntry objects to stay self-contained.
    """
    entries: list[dict[str, Any]] = []
    shared_memory = root / "knowledge" / "shared-memory"
    if not shared_memory.exists():
        return entries

    for scan_dir in SCAN_DIRS:
        scan_path = shared_memory / scan_dir
        if not scan_path.exists():
            continue
        for md_file in sorted(scan_path.rglob("*.md")):
            # Skip excluded directories
            if any(skip in md_file.parts for skip in SKIP_DIRS):
                continue
            if md_file.name in SKIP_FILES:
                continue

            text = md_file.read_text(encoding="utf-8")
            frontmatter, body = parse_frontmatter(text)
            if not frontmatter:
                continue

            scope = frontmatter.get("scope", "workspace")
            scope_type, scope_name = parse_scope(scope)
            memory_type = frontmatter.get("type", "feedback")
            name = frontmatter.get("name", md_file.stem.replace("-", " ").title())
            description = frontmatter.get("description", "")
            verified_at = frontmatter.get("verified_at", "")
            source = frontmatter.get("source", "")
            status = frontmatter.get("status", "active")
            tags_raw = frontmatter.get("tags", [])
            if isinstance(tags_raw, str):
                tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
            elif isinstance(tags_raw, list):
                tags = [str(t).strip() for t in tags_raw if str(t).strip()]
            else:
                tags = []
            if not tags:
                if memory_type:
                    tags.append(memory_type)
                if scope_type:
                    tags.append(scope_type)
                if scope_name:
                    tags.append(scope_name)

            body_text = body.strip()
            body_hash = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
            ts = md_file.stat().st_mtime
            updated_at = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).strftime("%Y-%m-%d")

            entries.append({
                "path": rel(root, md_file),
                "scope": scope,
                "scope_type": scope_type,
                "scope_name": scope_name or "",
                "type": memory_type,
                "name": name,
                "description": description,
                "verified_at": verified_at,
                "source": source,
                "status": status,
                "tags": tags,
                "body_hash": body_hash,
                "body": body_text,
                "updated_at": updated_at,
            })

    return entries


def compute_content_hash_from_entries(entries: list[dict[str, Any]]) -> str:
    """Compute a deterministic hash from entry data (source-derived).

    Mirrors knowledge_query.compute_content_hash.
    """
    rows: list[str] = []
    for entry in sorted(entries, key=lambda e: (e["path"], e["scope"])):
        row_parts = [
            entry["path"],
            entry["scope"],
            entry["scope_type"],
            entry["scope_name"] or "",
            entry["type"],
            entry["name"],
            entry["description"],
            entry["verified_at"],
            entry["source"],
            entry["status"],
            json.dumps(sorted(entry["tags"]), ensure_ascii=False),
            entry["body_hash"],
            entry["body"],
        ]
        rows.append("\t".join(row_parts))
    combined = "\n".join(rows)
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:12]


def check_query_index(root: Path) -> list[LintIssue]:
    """Check query index for missing, stale, or entry-count-mismatch.

    Returns a list of LintIssue objects.
    """
    issues: list[LintIssue] = []
    index_dir = root / "knowledge" / "shared-memory" / ".index"
    sqlite_path = index_dir / "memory.sqlite"
    manifest_path = index_dir / "manifest.json"

    # 1. Check index exists
    if not sqlite_path.exists():
        issues.append(LintIssue(
            level="warning",
            code="query-index-missing",
            path=rel(root, sqlite_path),
            detail="Query index database does not exist. Run 'knowledge_query.py rebuild-index'.",
        ))
        return issues  # Can't check staleness without index

    # 2. Check manifest exists
    if not manifest_path.exists():
        issues.append(LintIssue(
            level="warning",
            code="query-index-stale",
            path=rel(root, manifest_path),
            detail="Query index manifest.json does not exist. Run 'knowledge_query.py rebuild-index'.",
        ))
        return issues

    # 3. Read manifest
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        issues.append(LintIssue(
            level="warning",
            code="query-index-stale",
            path=rel(root, manifest_path),
            detail=f"Failed to parse manifest.json: {exc}. Run 'knowledge_query.py rebuild-index'.",
        ))
        return issues

    # 4. Check entry count in manifest vs curated files
    entries = collect_curated_entries_for_lint(root)
    curated_count = len(entries)
    manifest_count = manifest.get("entryCount")

    if manifest_count is None:
        issues.append(LintIssue(
            level="warning",
            code="query-index-stale",
            path=rel(root, manifest_path),
            detail="manifest.json is missing 'entryCount' field. Run 'knowledge_query.py rebuild-index'.",
        ))
    elif manifest_count != curated_count:
        issues.append(LintIssue(
            level="warning",
            code="query-index-entry-count-mismatch",
            path=rel(root, manifest_path),
            detail=f"Entry count mismatch: manifest has {manifest_count}, curated files count is {curated_count}. Run 'knowledge_query.py rebuild-index'.",
        ))

    # 5. Check content hash
    if entries:
        recomputed_hash = compute_content_hash_from_entries(entries)
        manifest_hash = manifest.get("hash")
        if manifest_hash is not None and manifest_hash != recomputed_hash:
            issues.append(LintIssue(
                level="warning",
                code="query-index-stale",
                path=rel(root, manifest_path),
                detail=f"Content hash mismatch: manifest hash={manifest_hash}, recomputed hash={recomputed_hash}. Run 'knowledge_query.py rebuild-index'.",
            ))

    # 6. Verify sqlite file is actually a valid database
    if sqlite_path.exists():
        try:
            import sqlite3
            db = sqlite3.connect(str(sqlite_path))
            cur = db.execute("SELECT COUNT(*) FROM memory_entries")
            actual_count = cur.fetchone()[0]
            cur.close()
            db.close()

            if manifest_count is not None and actual_count != manifest_count:
                issues.append(LintIssue(
                    level="warning",
                    code="query-index-entry-count-mismatch",
                    path=rel(root, sqlite_path),
                    detail=f"SQLite has {actual_count} rows but manifest says {manifest_count}. Run 'knowledge_query.py rebuild-index'.",
                ))
        except Exception as exc:
            issues.append(LintIssue(
                level="warning",
                code="query-index-stale",
                path=rel(root, sqlite_path),
                detail=f"Failed to read query index database: {exc}. Run 'knowledge_query.py rebuild-index'.",
            ))

    return issues


# ---------------------------------------------------------------------------
# Main lint runner
# ---------------------------------------------------------------------------

def check_followup_artifacts(root, findings, max_age_days):
    """Validate follow-up JSON artifacts for contract compliance and aging."""
    for art_path in collect_followup_files(root):
        for issue in validate_followup(root, art_path):
            findings.append(Finding(
                severity="error" if issue.level == "error" else "warn",
                check_id=issue.code,
                surface="followup-artifact",
                location=issue.path,
                description=issue.detail,
                fix_hint="Fix the follow-up artifact contract violations.",
            ))
        for issue in check_followup_aging(root, art_path, max_age_days):
            findings.append(Finding(
                severity="error" if issue.level == "error" else "warn",
                check_id=issue.code,
                surface="followup-artifact",
                location=issue.path,
                description=issue.detail,
                fix_hint="Resolve or close the aged follow-up artifact.",
            ))


def check_query_index_lint(root, findings):
    """Optional query index staleness and mismatch checks."""
    for issue in check_query_index(root):
        findings.append(Finding(
            severity="error" if issue.level == "error" else "warn",
            check_id=issue.code,
            surface="query-index",
            location=issue.path,
            description=issue.detail,
            fix_hint="Run 'python3 scripts/knowledge_query.py rebuild-index' to rebuild.",
        ))


def main() -> int:
    parser = argparse.ArgumentParser(description="Lint workspace knowledge surfaces")
    parser.add_argument("--root", default=".", help="Workspace root or any path inside it")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--include-info", action="store_true", help="Show info findings in text output")
    parser.add_argument("--staleness-threshold", type=int, default=DEFAULT_STALENESS_THRESHOLD)
    parser.add_argument(
        "--fail-on",
        choices=("error", "warn", "never"),
        default="error",
        help="Exit non-zero on error findings, warn-or-error findings, or never. Default: error.",
    )
    parser.add_argument("--fix", action="store_true", help="Print safe mechanical fix diff")
    parser.add_argument("--apply", action="store_true", help="Apply safe mechanical fixes; requires --fix")
    parser.add_argument("--pressure-summary", action="store_true", help="With --format json, include machine-readable shared-memory pressure summary")
    parser.add_argument(
        "--check-query-index",
        action="store_true",
        help="Also check the query index (SQLite FTS5 cache) for staleness or mismatch",
    )
    parser.add_argument(
        "--followup-max-age-days",
        type=int,
        default=int(os.environ.get("SHARED_MEMORY_FOLLOWUP_MAX_AGE_DAYS", "30")),
        help="Maximum age in days for open/in_progress follow-ups before warning. Default: 30.",
    )
    args = parser.parse_args()

    if args.apply and not args.fix:
        parser.error("--apply requires --fix")

    root = find_workspace_root(Path(args.root))
    findings: list[Finding] = []
    fixes: dict[Path, str] = {}

    check_shared_memory(root, findings, fixes, args.staleness_threshold)
    check_module_map(root, findings)
    check_markdown_links(root, workspace_guidance_files(root), "guidance-path-broken", "workspace-guidance", findings)
    check_knowledge_viewport(root, findings)
    check_followup_artifacts(root, findings, args.followup_max_age_days)
    if args.check_query_index:
        check_query_index_lint(root, findings)

    if args.fix:
        emitted_diff = False
        for path, new_text in sorted(fixes.items(), key=lambda item: rel(root, item[0])):
            old_text = path.read_text(encoding="utf-8")
            if old_text == new_text:
                continue
            print(render_fix_diff(path, old_text, new_text, root))
            emitted_diff = True
            if args.apply:
                path.write_text(new_text, encoding="utf-8")
        if not emitted_diff:
            print("No safe mechanical fixes available.")
        elif args.apply:
            print("Applied safe mechanical fixes.")

    if args.format == "json" and args.pressure_summary:
        print(
            json.dumps(
                {
                    "findings": [dataclasses.asdict(finding) for finding in findings],
                    "pressure": build_pressure_summary(root),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        output_findings(findings, args)
    if args.fail_on == "never":
        return 0
    if args.fail_on == "warn":
        return 1 if any(finding.severity in {"error", "warn"} for finding in findings) else 0
    return 1 if any(finding.severity == "error" for finding in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
