# Workspace Scope

Workspace-scope shared memory entries apply to the entire workspace — architecture invariants, operational conventions, known pitfalls that span all modules.

These entries are **always-on** (B1 mechanism): loaded into every agent session that reads the workspace guide file.

## MEMORY.md Index

`MEMORY.md` in this directory is the always-on index. It SHALL NOT exceed 200 lines. New workspace entries must be added to the index under the appropriate section.

## Sections

Choose one of the sections defined in your `MEMORY.md`. Common sections include:

- Architecture Invariants / Conventions
- Agent Workflow / Pipeline
- Submodule / Deployment
- Pitfalls / Operational Boundaries

Configure the section-to-heading mapping via `SHARED_MEMORY_INDEX_HEADINGS` environment variable (JSON map).
