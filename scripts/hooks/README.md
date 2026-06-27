# Hook Adapters

Hook adapters enable `knowledge init` to automatically register a
post-session callback that triggers `knowledge_absorb.py hook` whenever
the agent/editor finishes a session.

## Architecture

Each adapter lives in `scripts/hooks/<name>.py` and exports a single
function:

```python
def install(root: Path) -> dict[str, Any]:
    ...
```

**Parameters:**

- `root` (`Path`) — workspace root directory.

**Returns** a dict with:

| Key       | Type   | Description                                    |
|-----------|--------|------------------------------------------------|
| `status`  | str    | `"ok"`, `"skipped"`, or `"failed"`            |
| `message` | str    | Human-readable summary                         |
| `path`    | str\|None  | Absolute path to the installed hook file (if any) |

## Detection Priority

When `knowledge init` runs (without `--skip-hook`), it calls
`detect_harness()` which checks for well-known markers in this priority
order:

1. **Pi** — `~/.pi/` directory exists
2. **OpenCode** — `.opencode.json` in workspace root
3. **GitHub Actions** — `$GITHUB_ACTIONS` environment variable is `"true"`
4. **None** — fallback; prints manual instructions

## Available Adapters

### Pi (`pi.py`)

Detects [Pi agent harness](https://github.com/earendil-works/pi-coding-agent)
via `~/.pi/`. By default, `knowledge init` installs a workspace-local
post-compact hook at
`<workspace>/.pi/hooks/post-compact/shared-knowledge-absorb.sh`.

Global Pi hooks are opt-in:

```bash
python3 shared-knowledge/scripts/knowledge_query.py --root . init --hook-scope global
```

That writes to `~/.pi/hooks/post-compact/shared-knowledge-absorb.sh`.

### OpenCode (`opencode.py`)

Detects OpenCode via `.opencode.json` in the workspace root. Installs a
post-session hook at `.opencode/hooks/post-session/shared-knowledge-absorb.sh`.

### GitHub Actions (`github_actions.py`)

Generates (or updates) `.github/workflows/shared-knowledge.yml` with a
scheduled workflow that runs `knowledge_absorb.py hook` + `knowledge_lint.py`
daily, plus manual trigger via `workflow_dispatch`.

### None / Fallback (`none.py`)

When no known harness is detected, prints manual instructions for running
hook and lint commands. This is always safe and informative.

## Adding a New Adapter

1. Create `scripts/hooks/<name>.py` with an `install(root: Path) -> dict` function.
2. Add the detection logic to `detect_harness()` in `scripts/knowledge_query.py`.
3. Verify with: `python3 -m pytest tests/ -v`
