# Workspace Shared Memory

Cross-agent, cross-platform, cross-human-agent **workspace-level shared fact storage**.

## What Goes Here

**Workspace-level shared facts**: facts that span modules, capabilities, and agent platforms. Everyone who clones the workspace should benefit from them. Examples:

- Architecture invariants
- Known technical risks
- Workspace operational conventions
- Module status snapshots
- Pitfall records and root-cause SOPs

## What Does NOT Go Here

- ❌ **Personal preferences** → user-local agent memory/config (not in workspace git)
- ❌ **Single-task working memory** → LLM context window
- ❌ **Single-session short-term memory** → platform-native session mechanism
- ❌ **Module-level long-form docs** → `<module>/docs/{architecture,operations,runbooks}/`

Decision rule: **"Would another dev benefit from knowing this? → share it. Only you need it? → keep it personal."**

## Directory Structure

```
knowledge/facts/
├── README.md                    # This file (convention docs + contributing guide)
├── inbox/                       # Generated candidates; not always-on
│   └── README.md
├── workspace/                   # Workspace-wide, always-on loaded
│   ├── README.md
│   ├── MEMORY.md                # Index (SHALL NOT exceed 200 lines)
│   └── <name>.md                # Entry body
├── module/                      # Single-module scope
│   ├── README.md
│   └── <module>/<name>.md
└── capability/                  # Single-capability scope
    ├── README.md
    └── <capability>/<name>.md
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
| `source` | Writer identity for audit/conflict resolution |

## Contributing: Writing a New Entry

### 1. Determine scope

| Applies to | scope |
|---|---|
| Entire workspace (architecture invariants / conventions) | `workspace` |
| Single module only | `module:<name>` |
| Single capability only | `capability:<name>` |
| Personal preference | **Don't write here** — keep in user-local agent memory/config |

### 2. Create the file

Create the entry under the matching directory with proper frontmatter and body.

### 3. Update indexes (workspace scope only)

If `scope: workspace`, update:
- `knowledge/facts/workspace/MEMORY.md` — add an index line under the appropriate section
- Your workspace guide file's embedded shared-memory index

### 4. Commit and review

Commit the new entry. Reviewers check the markdown diff directly.

## Deprecation: Handling Superseded Entries

When a new fact supersedes an existing entry:

1. Change frontmatter `type: deprecated`
2. Add `⚠ Superseded by <new entry path>` at the top of the body
3. **Keep** the file in its original scope directory (maintains git-grep / history searchability)
4. **Remove** from the always-on index (avoid injecting stale content)

## Injection Mechanisms (How Agents/Humans Access This)

| Mechanism | Who | Strength |
|---|---|---|
| **B1 always-on** (workspace scope) | Any platform reading the workspace guide file | Index guaranteed; body instruction-driven |
| **B2 skill-body** (module/capability scope) | Individual skills declaring `## Pre-execution context` | Section content guaranteed; body instruction-driven |
| **B3 runtime adapter** (future) | Task-aware runtimes injecting from process variables | Module/capability scope per task context |

### B3 Runtime Adapter Interface Contract (for future task-execution integration)

When a task-aware runtime/worker adapter needs to inject shared-memory before execution:

**Contract**:
- Input: `processVariables: Record<string, unknown>` (from task claim)
- Actions:
  1. Parse module/capability identifiers from process variables
  2. Load matching scope entries:
     - `knowledge/facts/workspace/` — all (always-on)
     - `knowledge/facts/module/<name>/` — if module identifier present
     - `knowledge/facts/capability/<name>/` — if capability identifier present
  3. Inject loaded content into prompt prefix (recommended heading: `## Workspace Shared Memory (relevant scopes)`)

**Contract does not assume**: runtime implementation form (daemon / worker / custom), LLM platform, or whether retrieval is used.

Implement using existing markdown reader (don't reimplement frontmatter parser).

## Relationship to Other Knowledge Layers

| Layer | Shared Memory vs That Layer |
|---|---|
| Module docs | Module docs = long-form (chapter-organized); shared-memory = short entries (single fact) |
| User-local agent memory/config | Personal preferences stay outside workspace; workspace shared facts move here |
| Workspace guide file | Guide file embeds an index referencing this directory (B1 mechanism) |

## Maintenance Notes

- Workspace scope entries: aim for < 20; above that, split to module/capability or promote to module docs/skills
- Inbox default thresholds: candidate > 20, oldest > 14 days, workspace curated > 20
- `verified_at` is not auto-verified — entries > 50 with > 50% older than 90 days warrant a staleness detection pass
- Conflict resolution: when two entries contradict, PR review decides; merge the newer, deprecate the older with a superseded link
