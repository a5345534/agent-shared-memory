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

### Pi (`pi_lifecycle.py`) — Preferred

Detects [Pi agent harness](https://github.com/earendil-works/pi-coding-agent)
via `~/.pi/`. Installs a unified TypeScript extension at
`<workspace>/.pi/extensions/shared-knowledge-lifecycle.ts` that handles both:

- **Producer** (via `session_before_compact`): Runs
  `knowledge_compact_producer.py` to generate session-derived inbox
  candidates before compaction.
- **Absorber** (via `session_compact`): Runs `knowledge_absorb.py hook` to
  absorb inbox candidates after compaction.

Both stages run via `child_process.spawn()` with `detached: true` +
`.unref()` to avoid blocking Pi's session.

Global Pi scope is opt-in:

```bash
python3 shared-knowledge/scripts/knowledge_query.py --root . init --hook-scope global
```

The legacy post-compact shell hook can be installed alongside via:

```bash
python3 shared-knowledge/scripts/knowledge_query.py --root . init --legacy-hook
```

### Pi (`pi.py`) — DEPRECATED

Legacy adapter that installs a post-compact shell hook at
`<workspace>/.pi/hooks/post-compact/shared-knowledge-absorb.sh`.

Replaced by `pi_lifecycle.py`. Kept for backward compatibility via
`init --legacy-hook` and will be removed in a future release.

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
