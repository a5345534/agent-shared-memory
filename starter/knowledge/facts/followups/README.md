# Follow-up Artifacts

Handoff tasks produced by the absorption workflow when an inbox candidate is
classified as `promote_to_skill` or `promote_to_module_doc`. These JSON
artifacts are trackable tasks for downstream agents (skill-creator, doc-writer).

## Directory structure

```
followups/
├── README.md          # This file
├── skill/             # promote_to_skill artifacts
│   └── .gitkeep
└── module-doc/        # promote_to_module_doc artifacts
    └── .gitkeep
```

## Artifact lifecycle

Each follow-up artifact has a `status` field that transitions through:

| Status | Meaning |
|---|---|
| `open` | Created by absorption; awaiting downstream processing |
| `in_progress` | A downstream agent has claimed the task |
| `done` | The downstream agent completed the work; `outputs` is non-empty |
| `rejected` | The follow-up was reviewed and not actionable |
| `superseded` | A newer follow-up or alternative resolution replaced this |

## How artifacts are created

1. `knowledge_absorb.py plan` classifies inbox candidates
2. `knowledge_absorb.py apply` (including `--safe-only`) creates follow-up
   JSON artifacts for `promote_to_skill` and `promote_to_module_doc` actions
3. The inbox candidate is **not** deleted — it remains as an audit trail

## Idempotency

The same `sourceCandidate` + `sourceAction` + `kind` combination will not
create duplicate follow-up artifacts. Re-running absorption on the same
candidate with the same outcome safely returns the existing artifact path.

## Downstream agent contract

- **skill-creator**: Reads `skill/` artifacts with `kind: skill_followup`.
  Produces a reusable skill under `agent-workspace/skills/<name>/`. Updates
  the artifact `status` to `done` and populates `outputs`.
- **doc-writer**: Reads `module-doc/` artifacts with `kind: module_doc_followup`.
  Produces documentation under `<module>/docs/`. Updates the artifact `status`
  to `done` and populates `outputs`.

## Lint checks

`knowledge_lint.py` validates follow-up artifacts for:
- Valid JSON syntax and required fields
- Valid `kind`, `status`, and `handoffTo` enum values
- Non-empty `outputs` when `status` is `done`
- Aging alerts for `open` or `in_progress` artifacts older than the configured
  threshold (default 30 days, overridable via
  `SHARED_MEMORY_FOLLOWUP_MAX_AGE_DAYS`)

## Schema

Reference: `schemas/followup.schema.json`
