# Shared-memory Inbox

`knowledge/inbox/` is the temporary capture layer for generated shared-memory candidates.

Inbox candidates are **not** curated shared-memory entries:

- They are not listed in `workspace/MEMORY.md`
- They are not embedded into the workspace guide file
- They are not B1 always-on memory
- They may be incomplete, over-broad, or better suited to module docs or a reusable skill

Use the absorption workflow to move candidates to the correct authority:

```bash
python3 scripts/knowledge_absorb.py plan
python3 scripts/knowledge_absorb.py apply --safe-only
```

Local hooks run the same workflow automatically when inbox pressure exceeds the configured thresholds.

## Candidate Frontmatter

Generated candidates use simple YAML frontmatter:

```yaml
---
candidate_id: postcompact-memory-20260601-120000-example
status: inbox
captured_at: 2026-06-01
capture_source: agent:platform-model
source: agent:platform-model
suggested_action: retain_memory
suggested_scope: workspace
suggested_file: example.md
type: feedback
name: Example fact
description: One-line summary
evidence:
  - transcript: compacted session summary
reason: Why the reviewer considered it durable
---
```

`source` is repeated intentionally so generated candidates still identify the agent/model that captured them even before they become curated memory.

## Absorption Outcomes

- `retain_memory` — converts the candidate to curated shared memory
- `promote_to_module_doc` — creates review-gated follow-up for module docs
- `promote_to_skill` — creates review-gated follow-up for reusable skill
- `keep_inbox` — candidate lacks enough evidence or destination clarity
- `deprecate` / `move_scope` — used for already-curated entries during backlog triage

After a candidate is safely retained as curated memory, the inbox file is removed by the absorption commit. Git history remains the audit trail.
