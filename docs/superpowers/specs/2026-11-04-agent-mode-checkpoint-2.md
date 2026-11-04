# Checkpoint 2: Agent mode real-Docker validation and follow-up work

**Date:** 4 November 2026  
**Repo:** `C:\Users\User\Documents\Project Leo\L.E.O\L.E.O`  
**Previous checkpoint:** [2026-11-04-agent-mode-checkpoint.md](2026-11-04-agent-mode-checkpoint.md)  
**Design:** [2026-11-04-agent-mode-design.md](2026-11-04-agent-mode-design.md)

## 1. Handoff summary

Work continued from the first checkpoint. The Docker images now build, real sandbox integration tests run, and six of
seven real-Docker tests pass. The only remaining real-Docker failure is cleanup of a background process after command
timeout.

Do not commit, push, revert, or overwrite unrelated changes. This remains a dirty working tree containing both the
user's earlier work and agent-mode work.

## 2. Changes made in this continuation

### Model discovery and adoption

- `backend/discovery.py` now discovers native tool support:
  - OpenRouter: reads `supported_parameters` and sets `tools` based on whether it contains `"tools"`.
  - Gemini: marks discovered chat models as `tools: true`.
  - Anthropic: marks supported Claude models as `tools: true`.
  - Other/router catalogs: reads boolean `capabilities.tools` or `capabilities.toolCalling`; otherwise leaves it `None`.
- `backend/schemas.py`: `DiscoveredModel.tools: bool | None` added.
- `backend/api.py`: adopted model connections preserve `m.tools`.
- `tests/test_discovery.py`: assertions added for OpenRouter, Gemini, Anthropic, and router capability discovery.

### Usage UI and MCP

- `frontend/src/scripts/usage.ts` labels internal roles as `Lead agent`, `Sub-agent`, and `Context compactor`.
- `backend/mcp_server.py` now accepts `agent` mode, allows a 7200-second wait, and returns immediately with an
  approval-required message when an agent run enters `waiting`.

### Agent loop review

- `backend/agent.py`: tool argument binding now uses `inspect.signature(...).bind(...)` before invoking a handler.
  Invalid model-supplied arguments are returned as tool errors, while a genuine `TypeError` raised inside a handler is
  no longer incorrectly reported as “invalid arguments.”

### Docker build reliability

- `deploy/sandbox/Dockerfile` was redesigned because Debian's `npm` package pulled 413 packages and exhausted Docker
  memory.
  - Uses `python:3.12-slim` as the final base.
  - Copies Node 24/npm/corepack from the official `node:24-slim` image.
  - Installs OS tools in three smaller apt layers.
  - Uses persistent BuildKit apt caches plus retry and timeout settings.
- The final `leo-sandbox:latest` image builds successfully and contains Python 3.12, Node 24/npm, git, ripgrep, curl,
  pytest, procps, file, less, and unzip.
- Root `Dockerfile`: sandboxd's apt install now also uses persistent apt caches and retries.

### Real sandbox fixes

- `backend/sandbox.py`: request headers are merged correctly. Previously archive uploads supplied `headers` twice to
  `httpx`, raising `TypeError`.
- `backend/sandboxd.py`:
  - Archive import explicitly rejects any existing symbolic-link destination or parent before copying from staging.
  - Commands run through `setsid --wait timeout ...` and attempt a process-group cleanup after a timeout.
  - The timeout reporting problem is fixed, but one background `sleep` still remains; see the blocker below.

## 3. Verification completed

### Focused non-Docker tests

Before the latest real-Docker-only changes:

```text
tests/test_discovery.py + tests/test_agent.py + tests/test_sandbox.py + tests/test_agent_benchmark.py
43 passed, 1 warning
```

After the discovery and agent handler review:

```text
tests/test_discovery.py + tests/test_agent.py
23 passed, 1 warning
```

The warning is the existing Starlette `httpx` deprecation warning.

### Frontend

- Production build succeeded inside Docker.
- `astro check` still reports the 16 pre-existing `login.astro` / `register.astro` nullability and element-type errors.
- The agent-related TypeScript changes did not add new errors.

### Docker images and services

These images built successfully:

```text
leo-sandbox:latest
leo-sandboxd
leo-egress
```

At checkpoint time the integration-test services are still running:

```text
leo-egress-1     Up
leo-sandboxd-1   Up, published on 127.0.0.1:8100
```

Stop them later with:

```powershell
docker compose -f compose.yaml -f tests/compose.sandbox-test.yaml down
```

### Real Docker tests

Command:

```powershell
$env:LEO_TEST_SANDBOX_URL='http://127.0.0.1:8100'
$env:LEO_TEST_SANDBOXD_CONTAINER='leo-sandboxd-1'
uv run --frozen python -m pytest tests/test_sandbox_docker.py -v
```

Latest result:

```text
6 passed, 1 failed, 1 warning
```

Passing real-Docker coverage:

- Sandbox runs as uid 1000 without capabilities.
- File write/read/list round-trip works.
- Uploaded zip is safely unpacked with correct ownership.
- A planted symlink cannot redirect an imported write.
- Internal sandbox networking reaches registries but not sandboxd directly.
- Git push goes through a bundle, ignores hostile hooks, and keeps the token out of the sandbox.

## 4. Remaining real-Docker failure

Failing test:

```text
tests/test_sandbox_docker.py::test_command_timeout_kills_the_process_group
```

Current behavior:

- `sleep 300 & sleep 300` returns after the three-second timeout.
- `result["timed_out"]` is now `True`.
- A follow-up `ps -eo comm | grep -c sleep || true` returns `1`, expected `0`.

Relevant code is `Docker.exec()` in `backend/sandboxd.py`. It currently:

1. Executes `setsid --wait timeout --kill-after=5 <seconds> bash -lc <command>`.
2. Reads the exec PID from Docker's exec inspect response.
3. Starts a second Docker exec with `kill -KILL -<pid>` when timed out.

The remaining process may be outside the assumed process group, or it may briefly be a zombie awaiting init reaping.
Inspect PID/PPID/PGID/SID/state immediately after timeout before changing the test:

```sh
ps -eo pid,ppid,pgid,sid,state,comm,args | grep -E 'sleep|timeout|bash'
```

Likely fixes to evaluate:

- Capture the actual session/process-group ID while the exec is running, then kill that exact group.
- Use a wrapper that records its PID/PGID in `/tmp` and removes it after cleanup.
- If the remaining entry is confirmed to be a short-lived zombie owned by Docker's init, make cleanup wait for reaping
  with a short bounded poll before returning. Do not simply weaken the test without confirming the process state.

## 5. Recommended next steps

1. Diagnose and fix the one timeout cleanup failure above.
2. Rebuild/restart sandboxd and rerun `tests/test_sandbox_docker.py` until all seven pass.
3. Run the focused tests again because `backend/sandbox.py` and `backend/sandboxd.py` changed after the last 43-pass run:

   ```powershell
   uv run --frozen python -m pytest tests/test_sandbox.py tests/test_agent.py tests/test_discovery.py tests/test_agent_benchmark.py -q
   ```

4. Run the full suite and report the known pre-existing orchestrator failures from checkpoint 1 separately.
5. Improve the root Dockerfile stage order so backend source changes do not force sandboxd's git apt layer to reinstall.
6. Do the browser UI smoke test and full compose end-to-end test.
7. Ask the user before running the live benchmark because it spends model quota.

## 6. Important constraints retained from checkpoint 1

- Do not commit or push unless the user asks.
- Preserve all unrelated dirty-worktree changes.
- Do not run the live benchmark without explicit permission.
- The previously recorded nine `tests/test_orchestrator.py` failures predate agent mode; do not attribute them to this work.

