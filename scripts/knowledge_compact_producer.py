#!/usr/bin/env python3
"""Shared-memory inbox candidate generation from session context.

This script reviews session context (the messages being compacted) via an LLM
and writes durable shared-knowledge candidates to ``knowledge/inbox/*.md``.

Stdlib-only — no pip dependencies required.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_MEMORY_TYPES = {"architectural-invariant", "reference", "project", "feedback"}
VALID_SCOPE_RE = re.compile(r"^(workspace|module:[a-z0-9][a-z0-9-]*|capability:[a-z0-9][a-z0-9-]*)$")
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
SLUG_RE = re.compile(r"[^a-z0-9]+")

PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "compact-review.md"

PRODUCER_VERSION = "1"

# Env var defaults
ENV_API_KEY = "SHARED_KNOWLEDGE_LLM_API_KEY"
ENV_API_KEY_FALLBACK = "OPENAI_API_KEY"
ENV_BASE_URL = "SHARED_KNOWLEDGE_LLM_BASE_URL"
ENV_MODEL = "SHARED_KNOWLEDGE_LLM_MODEL"
ENV_ENABLED = "SHARED_KNOWLEDGE_PRODUCER_ENABLED"
ENV_HEADERS = "SHARED_KNOWLEDGE_LLM_HEADERS"
ENV_MAX_CONTEXT_TOKENS = "SHARED_KNOWLEDGE_MAX_CONTEXT_TOKENS"

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o"
DEFAULT_MAX_CONTEXT_TOKENS = 100_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_workspace_root(start: Path) -> Path:
    """Locate the workspace root by finding AGENTS.md or knowledge/facts/."""
    current = start.resolve()
    if current.is_file():
        current = current.parent
    for candidate in [current, *current.parents]:
        if (candidate / "AGENTS.md").exists():
            return candidate
        if (candidate / "knowledge" / "facts").exists():
            return candidate
    raise SystemExit(f"Could not locate workspace root from {start}. Expected AGENTS.md or knowledge/facts/.")


def rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def timestamp_slug() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _parse_headers_env(raw: str) -> dict[str, str] | None:
    """Parse SHARED_KNOWLEDGE_LLM_HEADERS JSON into a dict."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if v is not None}
    except json.JSONDecodeError:
        pass
    return None


def slugify(value: str, fallback: str = "candidate") -> str:
    normalized = value.lower().replace(".md", "")
    normalized = SLUG_RE.sub("-", normalized).strip("-")[:80].strip("-")
    return normalized or fallback


def clean_line(value: Any, max_len: int = 180) -> str:
    if value is None:
        return ""
    cleaned = re.sub(r"\s+", " ", str(value)).strip()
    return cleaned[:max_len].strip()


def format_frontmatter_value(value: Any) -> str:
    """Format a value for YAML frontmatter output."""
    text = str(value if value is not None else "").replace("\n", " ").strip()
    return json.dumps(text, ensure_ascii=False)


def env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config() -> dict[str, Any]:
    """Load configuration from environment variables."""
    api_key = env_str(ENV_API_KEY) or env_str(ENV_API_KEY_FALLBACK)
    return {
        "api_key": api_key,
        "base_url": env_str(ENV_BASE_URL, DEFAULT_BASE_URL),
        "model": env_str(ENV_MODEL, DEFAULT_MODEL),
        "enabled": env_str(ENV_ENABLED, "1") != "0",
        "max_context_tokens": env_int(ENV_MAX_CONTEXT_TOKENS, DEFAULT_MAX_CONTEXT_TOKENS),
        "headers": _parse_headers_env(env_str(ENV_HEADERS)),
    }


def check_config(config: dict[str, Any]) -> dict[str, Any]:
    """Check if the configuration is ready for production.

    Returns a dict with ``ready`` (bool) and ``message`` (str).
    """
    issues: list[str] = []
    if not config["enabled"]:
        issues.append(f"{ENV_ENABLED}=0, producer disabled")
    if not config["api_key"]:
        issues.append(f"Neither {ENV_API_KEY} nor {ENV_API_KEY_FALLBACK} is set")
    return {
        "ready": config["enabled"] and bool(config["api_key"]),
        "api_key_configured": bool(config["api_key"]),
        "enabled": config["enabled"],
        "base_url": config["base_url"],
        "model": config["model"],
        "issues": issues,
    }


# ---------------------------------------------------------------------------
# LLM integration (stdlib-only, OpenAI-compatible)
# ---------------------------------------------------------------------------

def build_review_prompt(context_json: str, prompt_text: str) -> str:
    """Build the user message for the LLM review."""
    token_est = len(context_json) // 4  # rough token estimate
    return (
        f"Review the following agent session context "
        f"(~{token_est} tokens estimated) and extract durable shared-knowledge "
        f"candidates.\n\n"
        f"---\n{context_json}\n---\n\n"
        f"Follow these instructions:\n\n{prompt_text}"
    )


def call_llm(config: dict[str, Any], prompt: str) -> dict[str, Any] | None:
    """Call the LLM via OpenAI-compatible chat completions endpoint.

    Returns the parsed JSON response object, or ``None`` on failure.
    """
    if not config["enabled"] or not config["api_key"]:
        return None

    url = f"{config['base_url'].rstrip('/')}/chat/completions"
    payload = {
        "model": config["model"],
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
        "temperature": 0.3,  # low temperature for conservative extraction
        "max_tokens": 4096,
    }

    # Build request headers
    # Start with Content-Type, then layer custom headers from auth.headers
    headers: dict[str, str] = {
        "Content-Type": "application/json",
    }
    custom_headers_raw = config.get("headers", {}) or {}
    if isinstance(custom_headers_raw, dict):
        for k, v in custom_headers_raw.items():
            if v is not None:
                headers[str(k)] = str(v)
    # Only add Bearer auth if no custom auth header was provided
    if "Authorization" not in headers and config.get("api_key"):
        headers["Authorization"] = f"Bearer {config['api_key']}"

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        print(f"[producer] LLM request failed: {exc}", file=sys.stderr)
        return None
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[producer] LLM response parse failed: {exc}", file=sys.stderr)
        return None

    # Extract text content from response
    try:
        choices = body.get("choices", [])
        if not choices:
            print(f"[producer] LLM returned no choices", file=sys.stderr)
            return None
        content = choices[0].get("message", {}).get("content", "")
        if not content:
            print(f"[producer] LLM returned empty content", file=sys.stderr)
            return None
        # Parse the content as JSON (it should be a JSON object)
        # Strip any markdown code fence wrapping
        content_clean = content.strip()
        if content_clean.startswith("```"):
            # Remove code fences (both ```json and ```)
            content_clean = re.sub(r"^```(?:json)?\s*\n?", "", content_clean)
            content_clean = re.sub(r"\n?```\s*$", "", content_clean)
        return json.loads(content_clean)
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        print(f"[producer] Failed to parse LLM response content: {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Candidate validation
# ---------------------------------------------------------------------------

def validate_candidate(candidate: dict[str, Any]) -> list[str]:
    """Validate a single candidate object. Returns a list of error messages.

    An empty list means the candidate is valid.
    """
    errors: list[str] = []

    name = clean_line(candidate.get("name"), 80)
    if not name:
        errors.append("missing or empty 'name'")

    description = clean_line(candidate.get("description"), 180)
    if not description:
        errors.append("missing or empty 'description'")

    memory_type = str(candidate.get("type", "")).strip()
    if memory_type not in VALID_MEMORY_TYPES:
        errors.append(f"invalid 'type': '{memory_type}' (must be one of {sorted(VALID_MEMORY_TYPES)})")

    scope = str(candidate.get("suggested_scope", "")).strip()
    if not VALID_SCOPE_RE.match(scope):
        errors.append(f"invalid 'suggested_scope': '{scope}'")

    body = str(candidate.get("body", "")).strip()
    if not body or len(body) < 20:
        errors.append("'body' missing or too short (< 20 chars)")

    reason = str(candidate.get("reason", "")).strip()
    if not reason:
        errors.append("missing or empty 'reason'")

    candidate_id = str(candidate.get("candidate_id", "")).strip()
    if not candidate_id:
        errors.append("missing or empty 'candidate_id'")

    return errors


def candidate_exists(inbox_dir: Path, candidate_id: str) -> bool:
    """Check if a candidate with the given ID already exists in the inbox."""
    if not inbox_dir.exists():
        return False
    for fpath in inbox_dir.iterdir():
        if fpath.suffix != ".md" or fpath.name == "README.md":
            continue
        text = fpath.read_text(encoding="utf-8")
        match = FRONTMATTER_RE.match(text)
        if not match:
            continue
        existing_id = ""
        for line in match.group(1).splitlines():
            line = line.rstrip()
            if line.startswith("candidate_id:"):
                existing_id = line.split(":", 1)[1].strip().strip('"')
                break
        if existing_id == candidate_id:
            return True
    return False


def render_candidate(candidate: dict[str, Any]) -> str:
    """Render a validated candidate as a Markdown inbox entry with frontmatter."""
    name = clean_line(candidate.get("name"), 80)
    description = clean_line(candidate.get("description"), 180)
    memory_type = str(candidate.get("type", "feedback")).strip()
    scope = str(candidate.get("suggested_scope", "workspace")).strip()
    body = str(candidate.get("body", "")).strip()
    reason = clean_line(candidate.get("reason"), 500)
    candidate_id = slugify(str(candidate.get("candidate_id", "")), "candidate")
    evidence_raw = candidate.get("evidence", [])
    if isinstance(evidence_raw, str):
        evidence_raw = [evidence_raw]
    evidence = [clean_line(e, 300) for e in evidence_raw if clean_line(e, 300)]

    lines = [
        "---",
        f"name: {format_frontmatter_value(name)}",
        f"description: {format_frontmatter_value(description)}",
        f"type: {memory_type}",
        f"suggested_action: retain_memory",
        f"suggested_scope: {scope}",
        f"candidate_id: {candidate_id}",
        f"captured_at: {dt.date.today().isoformat()}",
        f"source: agent:compact-producer",
        f"reason: {format_frontmatter_value(reason)}",
        "---",
        "",
        body,
    ]
    if evidence:
        lines.extend(["", "## Evidence", ""])
        lines.extend(f"- {item}" for item in evidence)
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core production logic
# ---------------------------------------------------------------------------

def produce(root: Path, context: list[Any], config: dict[str, Any]) -> dict[str, Any]:
    """Run the production pipeline: LLM review → validate → write candidates.

    Returns a summary dict with keys:
      - ``version``
      - ``generatedAt``
      - ``contextTokenCount`` (estimated)
      - ``candidatesWritten`` (int)
      - ``candidates`` (list of relative paths written)
      - ``skipped`` (list of skip reasons)
      - ``errors`` (list of error messages)
    """
    # Check if enabled
    if not config["enabled"]:
        return {
            "version": PRODUCER_VERSION,
            "generatedAt": now_iso(),
            "contextTokenCount": 0,
            "candidatesWritten": 0,
            "candidates": [],
            "skipped": [],
            "errors": ["producer disabled by environment"],
        }

    if not config["api_key"]:
        return {
            "version": PRODUCER_VERSION,
            "generatedAt": now_iso(),
            "contextTokenCount": 0,
            "candidatesWritten": 0,
            "candidates": [],
            "skipped": [],
            "errors": [f"no API key: set {ENV_API_KEY} or {ENV_API_KEY_FALLBACK}"],
        }

    inbox_dir = root / "knowledge" / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)

    # Load the review prompt
    prompt_text = ""
    if PROMPT_FILE.exists():
        prompt_text = PROMPT_FILE.read_text(encoding="utf-8")
    else:
        prompt_text = "Extract durable shared-knowledge candidates from this session context. Return JSON with a 'candidates' array."

    # Serialize context
    context_json = json.dumps(context, ensure_ascii=False)
    token_estimate = len(context_json) // 4

    # Build and send prompt
    user_prompt = build_review_prompt(context_json, prompt_text)

    response = call_llm(config, user_prompt)
    if response is None:
        return {
            "version": PRODUCER_VERSION,
            "generatedAt": now_iso(),
            "contextTokenCount": token_estimate,
            "candidatesWritten": 0,
            "candidates": [],
            "skipped": [],
            "errors": ["LLM call failed or returned no valid response"],
        }

    # Parse candidates from response
    raw_candidates = response.get("candidates", [])
    if not isinstance(raw_candidates, list):
        raw_candidates = []

    written: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []

    for idx, candidate in enumerate(raw_candidates):
        if not isinstance(candidate, dict):
            skipped.append(f"candidate[{idx}]: not a dict")
            continue

        # Validate
        validation_errors = validate_candidate(candidate)
        if validation_errors:
            cid = candidate.get("candidate_id", f"candidate[{idx}]")
            skipped.append(f"{cid}: validation failed: {'; '.join(validation_errors)}")
            continue

        # Dedup
        cid = str(candidate.get("candidate_id", "")).strip()
        if candidate_exists(inbox_dir, cid):
            skipped.append(f"{cid}: duplicate (already in inbox)")
            continue

        # Write
        content = render_candidate(candidate)
        filename = f"{dt.date.today().isoformat()}-{slugify(cid)}.md"
        dest = inbox_dir / filename
        try:
            # Ensure unique filename
            counter = 1
            while dest.exists():
                dest = inbox_dir / f"{dt.date.today().isoformat()}-{slugify(cid)}-{counter}.md"
                counter += 1
            dest.write_text(content, encoding="utf-8")
            written.append(rel(root, dest))
        except OSError as exc:
            errors.append(f"{cid}: write failed: {exc}")

    return {
        "version": PRODUCER_VERSION,
        "generatedAt": now_iso(),
        "contextTokenCount": token_estimate,
        "candidatesWritten": len(written),
        "candidates": written,
        "skipped": skipped,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate shared-knowledge inbox candidates from session context"
    )
    parser.add_argument("--root", default=".", help="Workspace root or path inside it")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # produce — read from file
    produce_parser = subparsers.add_parser("produce", help="Read session context from a JSON file and produce candidates")
    produce_parser.add_argument("--context-file", required=True, help="Path to JSON file with session context")
    produce_parser.add_argument("--format", choices=("text", "json"), default="json")

    # produce-stdin — read from stdin
    stdin_parser = subparsers.add_parser("produce-stdin", help="Read session context from stdin and produce candidates")
    stdin_parser.add_argument("--format", choices=("text", "json"), default="json")

    # check — validate configuration
    subparsers.add_parser("check", help="Check configuration readiness without calling LLM")

    return parser


def main() -> int:
    args = build_parser().parse_args()
    root = find_workspace_root(Path(args.root))

    config = load_config()

    if args.command == "check":
        result = check_config(config)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    # produce or produce-stdin
    if args.command == "produce":
        context_path = Path(args.context_file)
        if not context_path.exists():
            print(json.dumps({
                "version": PRODUCER_VERSION,
                "generatedAt": now_iso(),
                "candidatesWritten": 0,
                "candidates": [],
                "skipped": [],
                "errors": [f"context file not found: {args.context_file}"],
            }, ensure_ascii=False, indent=2))
            return 0
        try:
            context = json.loads(context_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(json.dumps({
                "version": PRODUCER_VERSION,
                "generatedAt": now_iso(),
                "candidatesWritten": 0,
                "candidates": [],
                "skipped": [],
                "errors": [f"failed to read context file: {exc}"],
            }, ensure_ascii=False, indent=2))
            return 0
    else:  # produce-stdin
        try:
            raw = sys.stdin.read()
            if not raw.strip():
                context = []
            else:
                context = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(json.dumps({
                "version": PRODUCER_VERSION,
                "generatedAt": now_iso(),
                "candidatesWritten": 0,
                "candidates": [],
                "skipped": [],
                "errors": [f"failed to parse stdin JSON: {exc}"],
            }, ensure_ascii=False, indent=2))
            return 0

    if not isinstance(context, list):
        context = [context]

    result = produce(root, context, config)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
