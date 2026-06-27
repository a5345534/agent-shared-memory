---
name: <short display name>
description: <one-line retrieval description>
type: feedback | project | reference | architectural-invariant
scope: workspace | module:<name> | capability:<name>
verified_at: <YYYY-MM-DD>
source: human:<name> | agent:<id>
---

# <Fact Title>

<State the reusable fact, convention, pitfall, or durable project state. Keep it
short and useful to another developer or agent.>

## Evidence

- <Source path, change, PR, or command output that justifies the fact>

## Not This Destination When

- The item is long module-owned documentation; promote it to module docs.
- The item is a repeatable procedure with commands/templates; promote it to a
  reusable skill.

## Index Step

If `scope: workspace`, update:

- `knowledge/facts/workspace/MEMORY.md`
- Your workspace guide file's embedded shared-memory index
