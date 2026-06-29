# Compact Review: Extract Durable Shared-Knowledge Candidates

You are reviewing an agent conversation session that is about to be compacted.
Your task is to identify any **durable, reusable facts** that should be preserved
as shared-knowledge entries for future sessions.

## What to look for

Extract ONLY these kinds of facts:

- **Architecture invariants**: decisions about project structure, module
  boundaries, dependency rules, conventions that span multiple sessions.
- **Operational workflows**: repeatable procedures, deployment steps, debugging
  recipes, CI/CD patterns.
- **Known pitfalls**: traps, gotchas, recurring issues, anti-patterns the team
  has learned to avoid.
- **Module/capability facts**: non-obvious behavior, contract requirements,
  integration notes about specific modules or capabilities.
- **Project conventions**: naming conventions, coding standards, testing
  patterns, documentation practices.

## What to IGNORE

- Task-local progress ("I fixed the bug", "the test passes now").
- Temporary debugging state ("tried X, got error Y, then fixed by Z" — Z may be
  worth capturing, but the debugging journey is not).
- Personal preferences that are not team conventions.
- Content already captured in existing shared-knowledge entries or documentation.
- Speculative design discussions with no decision.
- Chat, banter, greetings, status updates.

## Output format

Return a JSON object with a single key `candidates` containing an array of
candidate objects. If no durable facts are found, return `{"candidates": []}`.

Each candidate object MUST have these fields:

```json
{
  "candidate_id": "kebab-case-unique-identifier",
  "name": "Short display name (max 80 chars)",
  "description": "One-line retrieval description (max 180 chars)",
  "type": "architectural-invariant | reference | project | feedback",
  "suggested_scope": "workspace | module:<name> | capability:<name>",
  "body": "Markdown body with evidence and rationale. Be specific and cite sources from the conversation.",
  "reason": "Why this fact is durable and reusable across sessions. (max 500 chars)",
  "evidence": ["Source-specific evidence from the conversation"]
}
```

### Type guidance

| Type | When to use |
|------|-------------|
| `architectural-invariant` | A hard design constraint or decision that must not be violated |
| `reference` | Useful information that doesn't change often (API endpoints, conventions) |
| `project` | Project-level decision, roadmap item, or state |
| `feedback` | Learning or observation worth sharing |

### Scope guidance

| Scope | When to use |
|-------|-------------|
| `workspace` | Fact applies to the entire project/workspace |
| `module:<name>` | Fact applies to a specific module (e.g., `module:shared-knowledge`) |
| `capability:<name>` | Fact applies to a specific capability (e.g., `capability:inbox-absorption`) |

## Quality rules

- **Conservative**: If in doubt, leave it out. Missing a candidate is better than
  writing a noisy one.
- **Specific**: "The validation pipeline requires all hooks to be registered
  before the `on_commit` phase" is good. "There were some issues with the hooks"
  is not.
- **Durable**: Ask yourself: "Will this still be true and useful after 10 more
  sessions?" If not, skip it.
- **Correct scope**: Prefer narrower scope (`module:` or `capability:`) over
  `workspace` when the fact is module-specific. This keeps workspace-level
  entries focused and valuable.

## Response format reminder

Return ONLY valid JSON. No markdown wrapping, no explanation text outside the
JSON. Example:

```json
{
  "candidates": [
    {
      "candidate_id": "pipeline-registration-order",
      "name": "Pipeline hook registration order constraint",
      "description": "Validation hooks must register before on_commit phase to avoid race conditions",
      "type": "architectural-invariant",
      "suggested_scope": "module:workflow-engine",
      "body": "## Pipeline hook registration\n\nThe workflow engine requires all validation hooks to be registered\n**before** the `on_commit` phase begins. Hooks registered during or\nafter `on_commit` will not be executed and produce a silent failure.\n\n### Evidence\n- Developer spent 3 hours debugging a hook that was registered in\nthe `on_commit` callback instead of the `pre_commit` setup.\n- The `ValidatorRegistry` explicitly documents this in `validate()`\nbut the error message is misleading (returns 200 with empty result).",
      "reason": "This affects all future pipeline development. Multiple developers have hit this silently.",
      "evidence": [
        "Debug session showed ValidatorRegistry.validate() returns 200 with no errors even when no hooks are registered",
        "Fix required moving hook registration from on_commit to pre_commit setup"
      ]
    }
  ]
}
```
