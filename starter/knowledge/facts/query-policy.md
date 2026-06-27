# Query Index Policy

This document defines the policy, determinism guarantees, and cache lifecycle
for the `knowledge_query.py` query index used by `agent-shared-memory`.

## Source of truth

Curated Markdown entries under `knowledge/facts/workspace/`,
`knowledge/facts/module/`, and `knowledge/facts/capability/`
are the **source of truth** for all shared memory content.

The SQLite FTS5 index at `knowledge/.index/memory.sqlite` and
its companion `manifest.json` are **rebuildable caches**. They can be
regenerated at any time from the Markdown sources with:

```bash
python3 scripts/knowledge_query.py rebuild-index
```

## Cache lifecycle

### When the index is created

The index is initially created when `rebuild-index` is run for the first
time. The `.index/` directory is created automatically if it does not exist.

### When the index becomes stale

The index becomes stale when any curated Markdown entry is added, modified,
or removed. The `manifest.json` contains a source-derived `hash` field
(computed from all entry data, excluding wall-clock timestamps) that can
be compared to detect changes.

Staleness can be detected via lint:

```bash
python3 scripts/knowledge_lint.py --check-query-index
```

This produces warnings when:
- `.index/memory.sqlite` or `.index/manifest.json` is missing
- The `manifest.json` entry count does not match the curated file count
- The `manifest.json` hash does not match recomputed content

### When to rebuild

Rebuild the index:
- After adding, modifying, or removing curated `.md` entries
- After running `knowledge_absorb.py apply` with `--rebuild-query-index`
- Before performing queries that need fresh results
- Whenever lint reports index staleness

`rebuild-index` is always safe to re-run. It uses an exclusive write lock
during rebuild and is idempotent — repeated rebuilds produce identical
results from unchanged sources.

### When the index is skipped

If the index is not present, all query subcommands (`search`, `resolve`,
`inject`, `explain`, `list`) report an error instructing the user to run
`rebuild-index` first.

## Determinism guarantees

### No source of non-determinism

The query index and all query operations are fully deterministic:

1. **No external services**: No network calls, APIs, databases, or services.
2. **No embedding models**: No vector embeddings, no ML models, no random
   initialization.
3. **No daemons or processes**: Single-shot CLI with no persistent state
   beyond the SQLite file.
4. **Fixed ranking formula**: All scoring weights are hardcoded constants
   (see below).

### Ranking formula

The composite score for each entry is computed as:

```
score = BM25_base + boost_rules - penalty_rules
```

Where `BM25_base` is the normalized SQLite FTS5 BM25 rank (mapped to [0,1]
where 1 = perfect match).

**Boost rules** (additive):

| Condition | Boost |
|---|---|
| Query text matches entry `name` | +0.30 |
| Query text matches entry `description` | +0.20 |
| Scope exactly matches filter (e.g., `--module workflow`) | +0.25 |
| Entry type is `architectural-invariant` | +0.15 |

**Penalty rules**:

| Condition | Penalty |
|---|---|
| Entry `verified_at` is older than 365 days | -0.10 |
| Entry type is `deprecated` | excluded entirely |

Final score is clamped to `[0, ∞)`. Penalties do not produce negative
scores.

### Idempotent rebuild

`rebuild-index` is idempotent. Running it multiple times on the same set
of curated Markdown entries produces:
- The same number of entries in `memory_entries`
- The same FTS5 index content
- The same `manifest.json` `hash` value

The `generatedAt` timestamp in `manifest.json` changes with each rebuild,
but the `hash` is source-derived and does not include `generatedAt`.
If the `hash` matches the existing manifest, the manifest is not rewritten
(avoiding unnecessary git diffs).

### Frontmatter parsing determinism

The frontmatter parser handles the supported YAML subset deterministically:
top-level scalar string fields and simple list fields using `  - item`
syntax. Unsupported nested YAML structures are silently ignored. The same
Markdown file always produces the same parsed frontmatter values.

## Git policy

### Tracked

- Curated `.md` entries under `workspace/`, `module/`, `capability/`
- Follow-up `.json` artifacts under `followups/skill/`, `followups/module-doc/`
- Schema files under `schemas/`
- Script files under `scripts/`

### Not tracked (git-ignored)

- `knowledge/.index/memory.sqlite` — binary SQLite cache
- `knowledge/.index/manifest.json` — cache metadata; regenerated
  on rebuild

Rationale: The index is rebuildable from Markdown sources. Committing
SQLite binaries would create large, non-diffable files and increase
merge conflict risk.

## Scope boundaries

### What the query index covers

- Curated Markdown entries under `workspace/`, `module/`, `capability/`
- Entry frontmatter fields: name, description, scope, type, tags, status,
  verified_at, source
- Entry body text (for full-text search)

### What the query index does NOT cover

- Inbox candidates (`inbox/`) — these are pending review, not curated
- Follow-up artifacts (`followups/`) — not yet indexed (may be added in
  future)
- `README.md` and `MEMORY.md` files — skipped during scanning
- Deprecated entries — excluded by default from list/search/resolve/inject;
  visible only with explicit `--type deprecated` filter
- Entries without valid frontmatter — skipped during scanning

## Injection budget policy

When using `inject` to produce prompt-ready context:

- Budget is specified in characters (`--budget-chars N`)
- Character count approximates token count as `chars / 4`
- Priority ordering:
  1. `architectural-invariant` entries first
  2. Exact module/capability scope matches
  3. Workspace-scoped entries
  4. Lower-score entries last
- Body text is truncated with a `[truncated]` marker when budget is exceeded
- Entry headers (name, path, type) are prioritized over body text
- `explain` subcommand shows which entries were included, excluded, or
  truncated, and why

## Related

- [README.md](../../../../../README.md) — project overview and quick start
- [Follow-up artifact convention](../followups/README.md) — follow-up
  handoff task lifecycle
- [Schema: query-result.schema.json](../../../../../schemas/query-result.schema.json)
- [Schema: injection-context.schema.json](../../../../../schemas/injection-context.schema.json)
