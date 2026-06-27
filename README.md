# Agent Shared Memory

A **workspace-level shared fact storage layer** for cross-agent, cross-platform,
cross-human-agent collaboration.

Shared memory provides a lightweight, git-tracked convention for capturing
durable workspace facts — architecture invariants, known pitfalls, operational
conventions, and project state — that every developer and agent in the workspace
should know.

## What It Solves

Large multi-repo workspaces accumulate undocumented conventions, unwritten
rules, and tribal knowledge. Shared memory makes these **discoverable and
machine-readable** so:

- Every agent session loads workspace facts automatically (B1 always-on)
- Skills declare which module/capability facts they need (B2 skill-body)
- Runtime task executors inject relevant facts from process variables (B3 adapter)
- Lint checks prevent staleness, orphaned entries, and inbox bloat

## Quick Start

### 1. Add the directory structure to your workspace

```bash
mkdir -p knowledge/facts/{workspace,module,capability,inbox}
```

Copy the starter READMEs from `starter/knowledge/facts/` into your
workspace.

### 2. Install the scripts

```bash
# Copy the scripts into your agent workspace
cp scripts/knowledge_absorb.py agent-workspace/skills/shared-memory/scripts/
cp scripts/knowledge_lint.py agent-workspace/skills/shared-memory/scripts/
cp scripts/knowledge_query.py agent-workspace/skills/shared-memory/scripts/
```

### 3. Wire into your workspace guide (AGENTS.md)

Add a `## Workspace Shared Memory` section to your `AGENTS.md` that references
`knowledge/facts/workspace/MEMORY.md` as the always-on index (B1
mechanism).

### 4. Run periodic lint

```bash
python3 agent-workspace/skills/shared-memory/scripts/knowledge_lint.py
```

## Directory Structure

```
knowledge/facts/
├── README.md                    # Convention docs + contributing guide
├── inbox/                       # Generated candidates; not always-on
│   └── README.md
├── workspace/                   # Workspace-wide, always-on loaded
│   ├── README.md
│   ├── MEMORY.md                # Index (SHALL NOT exceed 200 lines)
│   └── <name>.md                # Entry body
├── module/                      # Single-module scope
│   ├── README.md
│   └── <module>/<name>.md
├── capability/                  # Single-capability scope
│   ├── README.md
│   └── <capability>/<name>.md
├── followups/                   # Absorption → downstream handoff
│   ├── README.md
│   ├── skill/                   # promote_to_skill follow-up artifacts
│   └── module-doc/              # promote_to_module_doc follow-up artifacts
└── .index/                      # Local SQLite FTS5 cache (git-ignored)
    ├── memory.sqlite
    └── manifest.json
```

## Frontmatter Schema

Every entry requires 6 YAML frontmatter fields:

```yaml
---
name: Short display name (< 60 chars)
description: One-line retrieval description (< 150 chars)
type: feedback | project | reference | architectural-invariant | deprecated
scope: workspace | module:<name> | capability:<name>
verified_at: 2026-06-22   # ISO date, last verified
source: human:<name>       # or agent:<id>
---
```

| Field | Rule |
|---|---|
| `name` | Display name |
| `description` | One-line, used for retrieval/index |
| `type` | One of 6 values; `deprecated` for superseded entries |
| `scope` | **Must** match the file's parent directory |
| `verified_at` | Today's date on write; update when body changes |
| `source` | Writer identity for audit |

## Injection Mechanisms

| Mechanism | Who | Strength |
|---|---|---|
| **B1 always-on** | Any platform reading the workspace guide file | Index guaranteed; body instruction-driven |
| **B2 skill-body** | Individual skills declaring `## Pre-execution context` | Module/capability scope only |
| **B3 runtime adapter** | Task-aware runtimes injecting from process variables | Module/capability scope per task context |

## Tools

### `knowledge_absorb.py` — Inbox Absorption

Manages the lifecycle of inbox candidates: classifies them, applies safe
mechanical promotions, and triggers hook-based auto-absorption when pressure
thresholds are exceeded.

```bash
# Check pressure
python3 knowledge_absorb.py pressure

# Build plan
python3 knowledge_absorb.py plan --format json

# Apply safe mechanical actions
python3 knowledge_absorb.py apply --safe-only

# Run hook (pressure check + safe auto-apply)
python3 knowledge_absorb.py hook
```

### `knowledge_lint.py` — Knowledge Surface Lint

Validates shared memory entries, module maps, workspace guidance, knowledge
viewport for drift, staleness, and structural errors. Also validates follow-up
artifact contract compliance and aging, and optionally checks the query index.

```bash
# Full lint
python3 knowledge_lint.py

# JSON output with pressure summary
python3 knowledge_lint.py --format json --pressure-summary

# Dry-run mechanical fixes
python3 knowledge_lint.py --fix

# Apply safe mechanical fixes
python3 knowledge_lint.py --fix --apply

# Also check query index staleness
python3 knowledge_lint.py --check-query-index

# Custom follow-up aging threshold
SHARED_MEMORY_FOLLOWUP_MAX_AGE_DAYS=60 python3 knowledge_lint.py
```

### `knowledge_query.py` — Deterministic Query CLI

Builds a local SQLite FTS5 index from curated shared memory entries and provides
subcommands for search, scope-based resolve, prompt-ready injection, and
explainable scoring.

```bash
# Build the query index
python3 knowledge_query.py rebuild-index

# List entries with filters
python3 knowledge_query.py list --scope workspace
python3 knowledge_query.py list --type architectural-invariant

# Full-text search with BM25 + boost/penalty scoring
python3 knowledge_query.py search "validation hook"

# Resolve relevant entries by module/capability scope
python3 knowledge_query.py resolve --module workflow --capability agent-orchestration

# Produce prompt-ready Markdown injection
python3 knowledge_query.py inject --module workflow --budget-chars 4000 --format markdown

# Explain why entries were selected or excluded
python3 knowledge_query.py explain --query "validation hook"
```

### Follow-up Artifact Workflow

When absorption classifies an inbox candidate as `promote_to_skill` or
`promote_to_module_doc`, a structured JSON follow-up artifact is created under
`knowledge/followups/`. The artifact tracks status, evidence,
recommended outputs, and (when completed) actual outputs — without creating
skills or writing module docs.

```bash
# Apply safe actions (creates follow-up artifacts)
python3 knowledge_absorb.py apply --safe-only

# Apply + rebuild query index
python3 knowledge_absorb.py apply --safe-only --rebuild-query-index
```

Follow-up artifact status lifecycle:

| Status | Meaning |
|--------|---------|
| `open` | Created but not yet picked up |
| `in_progress` | An agent is working on it |
| `done` | Completed; `outputs` field must be non-empty |
| `rejected` | Reviewed and rejected |
| `superseded` | Replaced by another follow-up or artifact |

## Contributing

### Writing a new entry

1. Determine scope (`workspace`, `module:<name>`, or `capability:<name>`)
2. Create the file under the matching directory
3. Fill frontmatter + body
4. If `workspace` scope: update `workspace/MEMORY.md` index + your workspace guide file's embedded index
5. Commit and PR

### Deprecation

When an entry is superseded:
1. Change `type: deprecated`
2. Add `⚠ Superseded by <new entry path>` at top of body
3. Keep the file (git history remains searchable)
4. Remove from always-on index

### Routing Decision

```
New fact → useful to another dev?
├── Yes (workspace shared)
│   └── Spans multiple modules/capabilities?
│       ├── Yes → knowledge/facts/workspace/
│       ├── One module → knowledge/facts/module/<name>/
│       └── One capability → knowledge/facts/capability/<name>/
└── No (personal preference)
    └── Keep in user-local agent memory/config (don't track in git)
```

## Pressure Thresholds

| Metric | Default | Env Override |
|---|---|---|
| Inbox max age | 14 days | `SHARED_MEMORY_INBOX_MAX_AGE_DAYS` |
| Inbox max count | 20 | `SHARED_MEMORY_INBOX_MAX_COUNT` |
| Workspace max entries | 20 | `SHARED_MEMORY_WORKSPACE_MAX_COUNT` |
| Auto-apply disable | — | `SHARED_MEMORY_ABSORB_AUTO_APPLY=0` |
| Follow-up max age | 30 days | `SHARED_MEMORY_FOLLOWUP_MAX_AGE_DAYS` |
| Require query index | no | `SHARED_MEMORY_REQUIRE_QUERY_INDEX=1` |

## License

MIT
