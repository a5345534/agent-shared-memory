# Module Scope

Module-scope shared memory entries apply to a single module. Name the subdirectory with the module's short name (e.g., `module/frontend/`, `module/workflow/`).

These entries are loaded via **B2 skill-body**: individual skills declare them under `## Pre-execution context`.

## Naming Convention

Use the module's short name (strip `-module` suffix if present). For example:
- `manufacturing-module` → `module/manufacturing/`
- `beyourself_frontend` → `module/frontend/`
