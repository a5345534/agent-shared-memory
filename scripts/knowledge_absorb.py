#!/usr/bin/env python3
"""Shared-memory inbox absorption workflow.

This script is intentionally stdlib-only so any agent platform, shell hook,
or CI/advisory job can call the same policy surface.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

VALID_MEMORY_TYPES = {"feedback", "project", "reference", "user", "architectural-invariant", "deprecated"}
ACTION_VALUES = {
    "retain_memory",
    "move_scope",
    "promote_to_module_doc",
    "promote_to_skill",
    "deprecate",
    "keep_inbox",
}
DEFAULT_INBOX_MAX_AGE_DAYS = 14
DEFAULT_INBOX_MAX_COUNT = 20
DEFAULT_WORKSPACE_MAX_COUNT = 20
PLAN_VERSION = "1"

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SLUG_RE = re.compile(r"[^a-z0-9]+")

# Default workspace index section headings. Override by setting
# SHARED_MEMORY_INDEX_HEADINGS as a JSON map in the environment.
DEFAULT_INDEX_HEADINGS: dict[str, str] = {
    "architecture": "Architecture Invariants / Conventions",
    "agent-workflow": "Agent Workflow / Pipeline",
    "submodule-deploy": "Submodule / Deployment",
    "pitfall": "Pitfalls / Operational Boundaries",
}
DEFAULT_INDEX_SECTION = "pitfall"
FOLLOWUP_VERSION = "1"
FOLLOWUP_KIND_TO_DIR: dict[str, str] = {
    "skill_followup": "skill",
    "module_doc_followup": "module-doc",
}
ACTION_TO_FOLLOWUP_KIND: dict[str, str] = {
    "promote_to_skill": "skill_followup",
    "promote_to_module_doc": "module_doc_followup",
}
ACTION_TO_HANDOFF: dict[str, str] = {
    "promote_to_skill": "skill-creator",
    "promote_to_module_doc": "doc-writer",
}
FOLLOWUP_STATUSES = {"open", "in_progress", "done", "rejected", "superseded"}
FOLLOWUP_PROMOTE_ACTIONS = {"promote_to_skill", "promote_to_module_doc"}


@dataclasses.dataclass
class Thresholds:
    inbox_max_age_days: int
    inbox_max_count: int
    workspace_max_count: int


@dataclasses.dataclass
class Pressure:
    triggered: bool
    reasons: list[str]
    thresholds: dict[str, int]
    metrics: dict[str, Any]


@dataclasses.dataclass
class PlanAction:
    candidatePath: str
    action: str
    reason: str
    evidence: list[str]
    destination: str | None
    confidence: float
    safeToApply: bool
    metadata: dict[str, Any]


@dataclasses.dataclass
class ApplyResult:
    changedPaths: list[str]
    skipped: list[str]
    followUps: list[str]


def find_workspace_root(start: Path) -> Path:
    """Locate the workspace root by finding AGENTS.md."""
    current = start.resolve()
    if current.is_file():
        current = current.parent
    for candidate in [current, *current.parents]:
        if (candidate / "AGENTS.md").exists():
            return candidate
    raise SystemExit(f"Could not locate workspace root (no AGENTS.md found) from {start}")


def rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def today() -> str:
    return dt.date.today().isoformat()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def timestamp_slug() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def index_headings() -> dict[str, str]:
    raw = os.environ.get("SHARED_MEMORY_INDEX_HEADINGS")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    return dict(DEFAULT_INDEX_HEADINGS)


def thresholds_from_args(args: argparse.Namespace) -> Thresholds:
    return Thresholds(
        inbox_max_age_days=args.inbox_max_age_days
        if args.inbox_max_age_days is not None
        else env_int("SHARED_MEMORY_INBOX_MAX_AGE_DAYS", DEFAULT_INBOX_MAX_AGE_DAYS),
        inbox_max_count=args.inbox_max_count
        if args.inbox_max_count is not None
        else env_int("SHARED_MEMORY_INBOX_MAX_COUNT", DEFAULT_INBOX_MAX_COUNT),
        workspace_max_count=args.workspace_max_count
        if args.workspace_max_count is not None
        else env_int("SHARED_MEMORY_WORKSPACE_MAX_COUNT", DEFAULT_WORKSPACE_MAX_COUNT),
    )


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text.strip()

    frontmatter: dict[str, Any] = {}
    current_key: str | None = None
    for raw_line in match.group(1).splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if line.startswith("  - ") and current_key:
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
    return frontmatter, text[match.end() :].strip()


def yaml_scalar(value: Any) -> str:
    text = str(value if value is not None else "").replace("\n", " ").strip()
    return json.dumps(text, ensure_ascii=False)


def slugify(value: str, fallback: str = "memory") -> str:
    normalized = value.lower().replace(".md", "")
    normalized = SLUG_RE.sub("-", normalized).strip("-")[:80].strip("-")
    return normalized or fallback


def clean_line(value: Any, max_len: int = 180) -> str:
    if value is None:
        return ""
    cleaned = re.sub(r"\s+", " ", str(value)).strip()
    return cleaned[:max_len].strip()


def evidence_list(frontmatter: dict[str, Any], body: str) -> list[str]:
    raw = frontmatter.get("evidence") or frontmatter.get("evidence_list") or []
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, list):
        values = [str(item) for item in raw]
    else:
        values = []

    if not values:
        match = re.search(r"(?im)^##\s+Evidence\b(?P<section>[\s\S]*?)(?:^##\s+|\Z)", body)
        if match:
            for line in match.group("section").splitlines():
                stripped = line.strip()
                if stripped.startswith("- "):
                    values.append(stripped[2:].strip())

    return [clean_line(value, 300) for value in values if clean_line(value, 300)]


def inbox_candidate_files(root: Path) -> list[Path]:
    inbox = root / "knowledge/inbox"
    if not inbox.exists():
        return []
    return sorted(path for path in inbox.glob("*.md") if path.name != "README.md")


def workspace_memory_files(root: Path) -> list[Path]:
    workspace = root / "knowledge/facts/workspace"
    if not workspace.exists():
        return []
    return sorted(path for path in workspace.glob("*.md") if path.name not in {"README.md", "MEMORY.md"})


def candidate_date(path: Path, frontmatter: dict[str, Any]) -> dt.date:
    raw = str(frontmatter.get("captured_at") or frontmatter.get("verified_at") or "")
    if ISO_DATE_RE.match(raw):
        return dt.date.fromisoformat(raw)
    return dt.datetime.fromtimestamp(path.stat().st_mtime).date()


def compute_pressure(root: Path, thresholds: Thresholds) -> Pressure:
    inbox_files = inbox_candidate_files(root)
    today_date = dt.date.today()
    oldest_age = 0
    oldest_path = None
    for path in inbox_files:
        frontmatter, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        age = max(0, (today_date - candidate_date(path, frontmatter)).days)
        if age >= oldest_age:
            oldest_age = age
            oldest_path = rel(root, path)

    workspace_count = 0
    deprecated_workspace_count = 0
    for path in workspace_memory_files(root):
        frontmatter, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        if frontmatter.get("type") == "deprecated":
            deprecated_workspace_count += 1
        else:
            workspace_count += 1

    reasons: list[str] = []
    if thresholds.inbox_max_count >= 0 and len(inbox_files) > thresholds.inbox_max_count:
        reasons.append(f"inbox_count {len(inbox_files)} > {thresholds.inbox_max_count}")
    if thresholds.inbox_max_age_days >= 0 and inbox_files and oldest_age > thresholds.inbox_max_age_days:
        reasons.append(f"oldest_inbox_age_days {oldest_age} > {thresholds.inbox_max_age_days}")
    if thresholds.workspace_max_count >= 0 and workspace_count > thresholds.workspace_max_count:
        reasons.append(f"workspace_memory_count {workspace_count} > {thresholds.workspace_max_count}")

    return Pressure(
        triggered=bool(reasons),
        reasons=reasons,
        thresholds={
            "inboxMaxAgeDays": thresholds.inbox_max_age_days,
            "inboxMaxCount": thresholds.inbox_max_count,
            "workspaceMaxCount": thresholds.workspace_max_count,
        },
        metrics={
            "inboxCount": len(inbox_files),
            "oldestInboxAgeDays": oldest_age if inbox_files else None,
            "oldestInboxPath": oldest_path,
            "workspaceMemoryCount": workspace_count,
            "deprecatedWorkspaceMemoryCount": deprecated_workspace_count,
        },
    )


def normalize_scope(scope: str) -> tuple[str, str] | None:
    scope = clean_line(scope, 120)
    if scope == "workspace":
        return scope, "knowledge/facts/workspace"
    if scope.startswith("module:"):
        name = slugify(scope.split(":", 1)[1], "module")
        return f"module:{name}", f"knowledge/facts/module/{name}"
    if scope.startswith("capability:"):
        name = slugify(scope.split(":", 1)[1], "capability")
        return f"capability:{name}", f"knowledge/facts/capability/{name}"
    return None


def destination_for_candidate(frontmatter: dict[str, Any]) -> tuple[str | None, str | None]:
    scope = str(frontmatter.get("suggested_scope") or frontmatter.get("scope") or "workspace")
    normalized = normalize_scope(scope)
    if not normalized:
        return None, None
    normalized_scope, directory = normalized
    suggested_file = clean_line(frontmatter.get("suggested_file") or frontmatter.get("file") or "", 120)
    base = slugify(suggested_file or frontmatter.get("name") or frontmatter.get("description") or "memory", "memory")
    return normalized_scope, f"{directory}/{base}.md"


def classify_candidate(root: Path, path: Path) -> PlanAction:
    text = path.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(text)
    location = rel(root, path)
    combined = "\n".join(
        str(frontmatter.get(key, "")) for key in ("name", "description", "reason", "suggested_action")
    ) + "\n" + body
    lowered = combined.lower()
    evidence = evidence_list(frontmatter, body)
    suggested = clean_line(frontmatter.get("suggested_action"), 80)
    action = suggested if suggested in ACTION_VALUES else ""

    if not action:
        if re.search(r"\b(module docs|runbook|docs/architecture|docs/operations|docs/runbooks|operation guide)\b", lowered):
            action = "promote_to_module_doc"
        elif re.search(r"\b(reusable skill|skill|procedure|script-backed|command workflow|template)\b", lowered):
            action = "promote_to_skill"
        else:
            action = "retain_memory"

    scope, destination = destination_for_candidate(frontmatter)
    name = clean_line(frontmatter.get("name"), 80)
    description = clean_line(frontmatter.get("description"), 180)
    memory_type = clean_line(frontmatter.get("type") or "feedback", 80)
    has_required_memory_fields = bool(name and description and scope and memory_type in VALID_MEMORY_TYPES)

    safe = action == "retain_memory" and has_required_memory_fields and destination is not None
    confidence = 0.78 if safe else 0.58
    reason = clean_line(frontmatter.get("reason"), 500)
    if not reason:
        reason = "Deterministic inbox classifier selected this action from candidate metadata and body text."
    if action != "retain_memory":
        safe = False
        destination = destination or suggested_destination_for_followup(action, frontmatter)
    if action == "retain_memory" and not safe:
        action = "keep_inbox"
        destination = None
        reason = "Candidate is missing required curated-memory fields or has an invalid destination scope."
        confidence = 0.35

    return PlanAction(
        candidatePath=location,
        action=action,
        reason=reason,
        evidence=evidence,
        destination=destination,
        confidence=confidence,
        safeToApply=safe,
        metadata={
            "source": frontmatter.get("capture_source") or frontmatter.get("source") or "",
            "suggestedScope": scope,
            "memoryType": memory_type,
            "name": name,
            "description": description,
        },
    )


def suggested_destination_for_followup(action: str, frontmatter: dict[str, Any]) -> str | None:
    if frontmatter.get("destination"):
        return str(frontmatter["destination"])
    if action == "promote_to_module_doc":
        return "<module>/docs/{architecture,operations,runbooks}/"
    if action == "promote_to_skill":
        return "agent-workspace/skills/<skill>/"
    return None


def get_candidate_id(frontmatter: dict[str, Any], source_path: Path) -> str:
    """Extract a stable candidate id from frontmatter or filename stem."""
    raw = str(frontmatter.get("candidate_id") or "")
    if raw.strip():
        return slugify(raw)
    return slugify(source_path.stem)


def followup_kind_for_action(action: str) -> str | None:
    return ACTION_TO_FOLLOWUP_KIND.get(action)


def followup_dir_for_kind(kind: str) -> str | None:
    return FOLLOWUP_KIND_TO_DIR.get(kind)


def followup_files(root: Path, kind: str) -> list[Path]:
    """List existing follow-up artifact files for a given kind."""
    kind_dir = followup_dir_for_kind(kind)
    if not kind_dir:
        return []
    followups_root = root / "knowledge/followups" / kind_dir
    if not followups_root.exists():
        return []
    return sorted(path for path in followups_root.glob("*.json"))


def find_existing_followup(root: Path, kind: str, candidate_id: str, source_candidate: str, source_action: str) -> tuple[Path | None, str | None]:
    """Check for an existing follow-up artifact with the same source details.

    Returns (existing_path, collision_type) where collision_type is:
      - "exact": same sourceCandidate + sourceAction + kind (idempotent — skip)
      - "id_collision": same candidate_id but different sourceCandidate (needs suffix)
      - None: no existing follow-up found

    An exact match takes priority over id collisions. The function scans all
    files and returns the first exact match; only if no exact match exists
    does it report an id collision.
    """
    existing_files = followup_files(root, kind)
    id_collision_path: Path | None = None
    for path in existing_files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        existing_source = data.get("sourceCandidate", "")
        existing_action = data.get("sourceAction", "")
        if existing_source == source_candidate and existing_action == source_action:
            return path, "exact"
        # Check for id collision: same candidate id, different source
        existing_id = path.stem.rsplit("-", 1)[0] if "-" in path.stem else path.stem
        if existing_id == candidate_id and existing_source != source_candidate:
            id_collision_path = path
    if id_collision_path:
        return id_collision_path, "id_collision"
    return None, None


def unique_followup_path(root: Path, kind: str, candidate_id: str) -> Path:
    """Find a unique path for a follow-up artifact, handling id collisions."""
    kind_dir = followup_dir_for_kind(kind)
    if not kind_dir:
        raise ValueError(f"Unknown followup kind: {kind}")
    followups_root = root / "knowledge/followups" / kind_dir
    followups_root.mkdir(parents=True, exist_ok=True)

    path = followups_root / f"{candidate_id}.json"
    if not path.exists():
        return path
    # Check if it's truly a collision (same id, different sourceCandidate)
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        existing = {}
    # If this is an exact match, return the original path (caller should skip)
    # Otherwise, find a numeric suffix
    for index in range(2, 100):
        candidate_path = followups_root / f"{candidate_id}-{index}.json"
        if not candidate_path.exists():
            return candidate_path
    raise RuntimeError(f"Could not find unique followup path for {candidate_id}")


def render_followup_artifact(
    source_candidate: str,
    source_action: str,
    frontmatter: dict[str, Any],
    body: str,
    suggested_destination: str,
    reason: str,
    evidence: list[str],
    confidence: float,
) -> dict[str, Any]:
    """Build the follow-up artifact JSON dict."""
    kind = followup_kind_for_action(source_action) or "skill_followup"
    handoff_to = ACTION_TO_HANDOFF.get(source_action, "skill-creator")
    name = clean_line(frontmatter.get("name"), 80) or "Untitled follow-up"
    description = clean_line(frontmatter.get("description"), 180) or "Follow-up from absorption pipeline."
    title = f"{name}: {description}"[:200].strip(": ").strip()

    recommended: list[str] = []
    raw_destination = frontmatter.get("destination") or ""
    if raw_destination:
        recommended.append(raw_destination)

    artifact: dict[str, Any] = {
        "version": FOLLOWUP_VERSION,
        "kind": kind,
        "status": "open",
        "createdAt": now_iso(),
        "sourceCandidate": source_candidate,
        "sourceAction": source_action,
        "suggestedDestination": suggested_destination,
        "title": title,
        "reason": reason,
        "evidence": evidence,
        "confidence": confidence,
        "safeToAutoApply": False,
        "handoffTo": handoff_to,
    }
    if recommended:
        artifact["recommendedOutputs"] = recommended
    return artifact


def classify_workspace_backlog(root: Path) -> list[PlanAction]:
    actions: list[PlanAction] = []
    for path in workspace_memory_files(root):
        text = path.read_text(encoding="utf-8")
        frontmatter, body = parse_frontmatter(text)
        if frontmatter.get("type") == "deprecated":
            continue
        evidence = evidence_list(frontmatter, body)
        location = rel(root, path)
        body_lower = body.lower()
        if "agent-workspace/skills" in body_lower or ("repeatable" in body_lower and "procedure" in body_lower):
            action = "promote_to_skill"
            destination = "agent-workspace/skills/<skill>/"
            reason = "Workspace memory looks procedural and may belong in a reusable skill."
            confidence = 0.45
        else:
            action = "retain_memory"
            destination = location
            reason = "Existing curated workspace entry retained; backlog triage found no safe automatic move."
            confidence = 0.3
        actions.append(
            PlanAction(
                candidatePath=location,
                action=action,
                reason=reason,
                evidence=evidence,
                destination=destination,
                confidence=confidence,
                safeToApply=False,
                metadata={
                    "source": frontmatter.get("source") or "",
                    "name": frontmatter.get("name") or path.stem,
                    "description": frontmatter.get("description") or "",
                    "backlog": True,
                },
            )
        )
    return actions


def build_plan(root: Path, thresholds: Thresholds, trigger: str, include_workspace_backlog: bool) -> dict[str, Any]:
    pressure = compute_pressure(root, thresholds)
    actions = [classify_candidate(root, path) for path in inbox_candidate_files(root)]
    if include_workspace_backlog:
        actions.extend(classify_workspace_backlog(root))
    return {
        "version": PLAN_VERSION,
        "generatedAt": now_iso(),
        "trigger": trigger,
        "pressure": dataclasses.asdict(pressure),
        "actions": [dataclasses.asdict(action) for action in actions],
    }


def render_markdown_plan(plan: dict[str, Any]) -> str:
    pressure = plan["pressure"]
    lines = [
        "# Shared-memory Absorption Plan",
        "",
        f"Generated: `{plan['generatedAt']}`",
        f"Trigger: `{plan['trigger']}`",
        f"Pressure: `{'triggered' if pressure['triggered'] else 'ok'}`",
        "",
    ]
    if pressure["reasons"]:
        lines.append("## Pressure reasons")
        lines.append("")
        for reason in pressure["reasons"]:
            lines.append(f"- {reason}")
        lines.append("")

    lines.extend(["## Actions", "", "| Candidate | Action | Destination | Safe | Reason |", "|---|---|---|---|---|"])
    for action in plan["actions"]:
        destination = action.get("destination") or ""
        lines.append(
            f"| `{action['candidatePath']}` | `{action['action']}` | `{destination}` | `{str(action['safeToApply']).lower()}` | {action['reason']} |"
        )
    if not plan["actions"]:
        lines.append("| _(none)_ | | | | |")
    lines.append("")
    return "\n".join(lines)


def load_plan(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    if args.plan_file:
        return json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
    return build_plan(root, thresholds_from_args(args), args.trigger, args.include_workspace_backlog)


def unique_destination(root: Path, destination: str) -> Path:
    path = root / destination
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix or ".md"
    for index in range(2, 100):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find unique destination for {destination}")


def render_curated_memory(frontmatter: dict[str, Any], body: str, normalized_scope: str) -> str:
    name = clean_line(frontmatter.get("name"), 80) or "Captured shared-memory fact"
    description = clean_line(frontmatter.get("description"), 180) or "Captured shared-memory fact."
    memory_type = clean_line(frontmatter.get("type") or "feedback", 80)
    if memory_type not in VALID_MEMORY_TYPES or memory_type == "deprecated":
        memory_type = "feedback"
    source = clean_line(frontmatter.get("capture_source") or frontmatter.get("source") or "agent:unknown", 120)
    reason = clean_line(frontmatter.get("reason"), 800)
    evidence = evidence_list(frontmatter, body)
    body_text = body.strip() or description

    lines = [
        "---",
        f"name: {yaml_scalar(name)}",
        f"description: {yaml_scalar(description)}",
        f"type: {memory_type}",
        f"scope: {normalized_scope}",
        f"verified_at: {today()}",
        f"source: {source}",
        "---",
        "",
        body_text,
    ]
    if reason and "## Why this is shared" not in body_text:
        lines.extend(["", "## Why this is shared", "", reason])
    if evidence and "## Evidence" not in body_text:
        lines.extend(["", "## Evidence", ""])
        lines.extend(f"- {item}" for item in evidence)
    lines.append("")
    return "\n".join(lines)


def index_line(name: str, description: str, file_name: str, agents: bool) -> str:
    target = f"knowledge/facts/workspace/{file_name}" if agents else file_name
    return f"- [{name}]({target}) — {description}"


def insert_under_heading(text: str, heading_prefix: str, heading: str, line: str, before_heading: str | None = None) -> str:
    if line in text:
        return text
    marker = re.search(rf"^{re.escape(heading_prefix + ' ' + heading)}\s*$", text, flags=re.MULTILINE)
    if not marker:
        if before_heading:
            before = re.search(rf"^{re.escape(before_heading)}\s*$", text, flags=re.MULTILINE)
            if before:
                return text[: before.start()].rstrip() + f"\n\n{heading_prefix} {heading}\n\n{line}\n\n" + text[before.start() :]
        return text.rstrip() + f"\n\n{heading_prefix} {heading}\n\n{line}\n"
    next_heading = re.search(rf"^{re.escape(heading_prefix)}\s+", text[marker.end() :], flags=re.MULTILINE)
    insert_at = marker.end() + (next_heading.start() if next_heading else len(text[marker.end() :]))
    return text[:insert_at].rstrip() + "\n" + line + "\n\n" + text[insert_at:].lstrip("\n")


def update_workspace_indexes(root: Path, destination: Path, frontmatter: dict[str, Any]) -> list[str]:
    changed: list[str] = []
    name = clean_line(frontmatter.get("name"), 80) or destination.stem.replace("-", " ").title()
    description = clean_line(frontmatter.get("description"), 180) or "Captured shared-memory fact."
    section_key = clean_line(frontmatter.get("index_section"), 80) or DEFAULT_INDEX_SECTION
    headings = index_headings()
    heading = headings.get(section_key, headings.get(DEFAULT_INDEX_SECTION, "Shared Memory"))

    memory_index = root / "knowledge/facts/workspace/MEMORY.md"
    if memory_index.exists():
        old = memory_index.read_text(encoding="utf-8")
        new = insert_under_heading(old, "##", heading, index_line(name, description, destination.name, False))
        if new != old:
            memory_index.write_text(new, encoding="utf-8")
            changed.append(rel(root, memory_index))

    agents = root / "AGENTS.md"
    if agents.exists():
        old = agents.read_text(encoding="utf-8")
        new = insert_under_heading(
            old,
            "####",
            heading,
            index_line(name, description, destination.name, True),
            before_heading="### Routing Decision Tree",
        )
        if new != old:
            agents.write_text(new, encoding="utf-8")
            changed.append(rel(root, agents))
    return changed


def apply_retain_memory(root: Path, action: dict[str, Any]) -> tuple[list[str], str | None]:
    source_path = root / action["candidatePath"]
    if not source_path.exists():
        return [], f"missing candidate: {action['candidatePath']}"
    text = source_path.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(text)
    normalized = normalize_scope(str(action.get("metadata", {}).get("suggestedScope") or frontmatter.get("suggested_scope") or "workspace"))
    if not normalized:
        return [], f"invalid destination scope for {action['candidatePath']}"
    normalized_scope, _ = normalized
    destination_raw = action.get("destination")
    if not destination_raw:
        return [], f"missing destination for {action['candidatePath']}"
    destination = unique_destination(root, destination_raw)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_curated_memory(frontmatter, body, normalized_scope), encoding="utf-8")
    source_path.unlink()

    changed = [rel(root, destination), rel(root, source_path)]
    if normalized_scope == "workspace":
        changed.extend(update_workspace_indexes(root, destination, frontmatter))
    return changed, None


def apply_plan(root: Path, plan: dict[str, Any], safe_only: bool) -> ApplyResult:
    changed: list[str] = []
    skipped: list[str] = []
    follow_ups: list[str] = []
    for action in plan.get("actions", []):
        action_name = action.get("action", "")
        candidate_path = action.get("candidatePath", "")

        # Follow-up artifact creation for promote actions (safe mechanical action)
        if action_name in FOLLOWUP_PROMOTE_ACTIONS:
            followup_result = apply_followup_artifact(root, action)
            if followup_result.get("existing"):
                follow_ups.append(
                    f"{candidate_path}: {action_name} (existing follow-up at {followup_result['path']})"
                )
            elif followup_result.get("path"):
                changed.append(followup_result["path"])
                follow_ups.append(
                    f"{candidate_path}: {action_name} → followup {followup_result['path']}"
                )
            elif followup_result.get("error"):
                skipped.append(followup_result["error"])
            continue

        if safe_only and not action.get("safeToApply"):
            if action_name in {"deprecate", "move_scope"}:
                follow_ups.append(f"{candidate_path}: {action_name} -> {action.get('destination') or '(needs destination)'}")
            continue

        if action_name == "retain_memory" and action.get("safeToApply"):
            action_changed, error = apply_retain_memory(root, action)
            if error:
                skipped.append(error)
            else:
                changed.extend(action_changed)
        elif action_name != "keep_inbox":
            follow_ups.append(f"{candidate_path}: {action_name} -> {action.get('destination') or '(needs destination)'}")
    return ApplyResult(sorted(set(changed)), skipped, follow_ups)


def apply_followup_artifact(root: Path, action: dict[str, Any]) -> dict[str, Any]:
    """Create a follow-up artifact for a promote action.

    Returns a dict with keys:
      - path: relative path of the created followup file
      - existing: true if an identical followup already exists
      - error: error message if creation failed
    """
    candidate_path_str = action.get("candidatePath", "")
    action_name = action.get("action", "")
    if not candidate_path_str or action_name not in FOLLOWUP_PROMOTE_ACTIONS:
        return {"error": f"Invalid action for followup: {action_name}"}

    source_path = root / candidate_path_str
    if not source_path.exists():
        return {"error": f"Source candidate not found: {candidate_path_str}"}

    text = source_path.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(text)

    kind = followup_kind_for_action(action_name)
    if not kind:
        return {"error": f"Unknown followup kind for action: {action_name}"}

    candidate_id = get_candidate_id(frontmatter, source_path)

    # Idempotency check: same sourceCandidate + sourceAction → skip
    existing_path, collision = find_existing_followup(
        root, kind, candidate_id, candidate_path_str, action_name
    )
    if collision == "exact":
        return {"path": rel(root, existing_path), "existing": True}

    # Determine suggested destination
    destination = action.get("destination") or ""
    if not destination:
        destination = suggested_destination_for_followup(action_name, frontmatter) or "(needs destination)"

    reason = action.get("reason", "")
    evidence = action.get("evidence", [])
    if not isinstance(evidence, list):
        evidence = [str(evidence)] if evidence else []
    confidence = float(action.get("confidence", 0.5))

    artifact = render_followup_artifact(
        source_candidate=candidate_path_str,
        source_action=action_name,
        frontmatter=frontmatter,
        body=body,
        suggested_destination=destination,
        reason=reason,
        evidence=evidence,
        confidence=confidence,
    )

    # Determine output path (handles numeric suffix for id collisions)
    followup_path = unique_followup_path(root, kind, candidate_id)

    followup_path.parent.mkdir(parents=True, exist_ok=True)
    followup_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    return {"path": rel(root, followup_path)}


def run(command: list[str], cwd: Path, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)


def run_hook(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    thresholds = thresholds_from_args(args)
    pressure = compute_pressure(root, thresholds)
    result: dict[str, Any] = {
        "pressure": dataclasses.asdict(pressure),
        "triggered": pressure.triggered,
        "autoApplyEnabled": os.environ.get("SHARED_MEMORY_ABSORB_AUTO_APPLY") != "0",
        "worktree": None,
        "commit": None,
        "apply": None,
    }
    if not pressure.triggered or not result["autoApplyEnabled"]:
        return result

    # Run plan + apply directly in-place (worktree integration is
    # workspace-specific and should be configured by the adopter).
    plan = build_plan(root, thresholds, "hook", args.include_workspace_backlog)
    apply_result = apply_plan(root, plan, True)
    result["apply"] = dataclasses.asdict(apply_result)

    changed_paths = apply_result.changedPaths
    if not changed_paths:
        return result

    add = run(["git", "add", "-A", *changed_paths], cwd=root, timeout=30)
    if add.returncode != 0:
        result["error"] = add.stderr or add.stdout or "git add failed"
        return result
    commit = run(["git", "commit", "-m", "memory: absorb shared-memory inbox candidates"], cwd=root, timeout=60)
    if commit.returncode != 0:
        result["error"] = commit.stderr or commit.stdout or "git commit failed"
        return result
    rev = run(["git", "rev-parse", "--short", "HEAD"], cwd=root, timeout=30)
    result["commit"] = {"ok": True, "sha": rev.stdout.strip()}
    return result


def emit(data: Any, fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        if isinstance(data, dict) and "version" in data and "actions" in data:
            print(render_markdown_plan(data))
        elif dataclasses.is_dataclass(data):
            print(json.dumps(dataclasses.asdict(data), ensure_ascii=False, indent=2))
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2))


def add_threshold_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--inbox-max-age-days", type=int, default=None)
    parser.add_argument("--inbox-max-count", type=int, default=None)
    parser.add_argument("--workspace-max-count", type=int, default=None)


def _rebuild_query_index(root: Path) -> None:
    """Invoke knowledge_query.py rebuild-index as a subprocess."""
    query_script = root / "scripts" / "knowledge_query.py"
    if not query_script.exists():
        print(f"[absorb] knowledge_query.py not found at {query_script}, skipping index rebuild", file=sys.stderr)
        return
    print(f"[absorb] Rebuilding query index via {query_script}...", file=sys.stderr)
    result = run([sys.executable, str(query_script), "--root", str(root), "rebuild-index"], cwd=root, timeout=60)
    if result.returncode != 0:
        print(f"[absorb] Query index rebuild failed: {result.stderr or result.stdout}", file=sys.stderr)
    else:
        print("[absorb] Query index rebuilt successfully.", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Absorb shared-memory inbox candidates")
    parser.add_argument("--root", default=".", help="Workspace root or path inside it")
    subparsers = parser.add_subparsers(dest="command", required=True)

    pressure = subparsers.add_parser("pressure", help="Report pressure thresholds and metrics")
    pressure.add_argument("--format", choices=("text", "json"), default="text")
    add_threshold_args(pressure)

    plan = subparsers.add_parser("plan", help="Build an absorption plan")
    plan.add_argument("--format", choices=("text", "json"), default="text")
    plan.add_argument("--trigger", default="manual")
    plan.add_argument("--include-workspace-backlog", action="store_true")
    add_threshold_args(plan)

    report = subparsers.add_parser("report", help="Alias for plan --format text")
    report.add_argument("--trigger", default="manual")
    report.add_argument("--include-workspace-backlog", action="store_true")
    add_threshold_args(report)

    apply_parser = subparsers.add_parser("apply", help="Apply safe actions from a plan")
    apply_parser.add_argument("--format", choices=("text", "json"), default="text")
    apply_parser.add_argument("--trigger", default="manual")
    apply_parser.add_argument("--plan-file", default="")
    apply_parser.add_argument("--safe-only", action="store_true")
    apply_parser.add_argument("--include-workspace-backlog", action="store_true")
    apply_parser.add_argument(
        "--rebuild-query-index",
        action="store_true",
        help="Rebuild the query index after applying actions",
    )
    add_threshold_args(apply_parser)

    hook = subparsers.add_parser("hook", help="Run local hook pressure check and safe auto-apply")
    hook.add_argument("--format", choices=("text", "json"), default="text")
    hook.add_argument(
        "--rebuild-query-index",
        action="store_true",
        help="Rebuild the query index after hook auto-apply",
    )
    add_threshold_args(hook)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    root = find_workspace_root(Path(args.root))

    if args.command == "pressure":
        emit(dataclasses.asdict(compute_pressure(root, thresholds_from_args(args))), args.format)
        return 0
    if args.command in {"plan", "report"}:
        plan = build_plan(
            root,
            thresholds_from_args(args),
            args.trigger,
            getattr(args, "include_workspace_backlog", False),
        )
        emit(plan, "text" if args.command == "report" else args.format)
        return 0
    if args.command == "apply":
        plan = load_plan(root, args)
        result = apply_plan(root, plan, args.safe_only)
        emit(dataclasses.asdict(result), args.format)
        ret = 1 if result.skipped else 0
        if getattr(args, "rebuild_query_index", False):
            _rebuild_query_index(root)
        return ret
    if args.command == "hook":
        emit(run_hook(root, args), args.format)
        if getattr(args, "rebuild_query_index", False):
            _rebuild_query_index(root)
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
