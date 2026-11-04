# Checkpoint: Agent mode (multi-agent coding in a sandbox) — handoff

**Date:** 4 November 2026 · **Repo:** `C:\Users\User\Documents\Project Leo\L.E.O\L.E.O` (Windows, Git Bash + PowerShell, Docker Desktop 29.9)
**Design spec:** [2026-11-04-agent-mode-design.md](2026-11-04-agent-mode-design.md) (read it first)

## 1. Goal (from the user)

Turn L.E.O. into a multi-agent orchestrator that runs long tasks the way Claude Code or Antigravity does. The user's
standing instruction is: **"Do the task and do the test too, build it using A and A"**:

- **Sandbox, option A:** one Docker container per workspace, created by a small `sandboxd` service (the only service with the Docker socket).
- **Agent loop, option A:** built natively in L.E.O. (no LangGraph or other framework), reusing the orchestrator's routing, budgets, checkpoints and events.

The user also approved these decisions:

| Topic | Decision |
|---|---|
| Workspace | A sandbox per session; one workspace per conversation, sharing the same id |
| Network | Package registries only, through an egress proxy with an allowlist |
| Autonomy | Free inside the sandbox; asks for approval before a git push or before going past the budget/step limit; the user can steer or stop at any time |
| Models | Native tool calling only (`tools: true/false/null` on each connection) |
| Topology | One lead agent with a `delegate` tool; skills are the sub-agent types; delegations run in parallel |
| In / out | Start empty, from an uploaded zip, or from a git clone; leave as a zip download or a git push |
| Success | A 3-task benchmark: build from scratch, fix a repo, long multi-agent task that survives a worker restart |

## 2. Rules for whoever continues

- **Do not commit or push** unless the user asks. The working tree also holds the **user's own earlier uncommitted changes**
  (api.py, auth.py, store.py, compose.yaml, frontend, and others; see `git status`). Do not revert or overwrite them.
- **Pre-existing test failures:** 9 tests in `tests/test_orchestrator.py` failed **before** this work started
  (`test_task_dependencies_and_synthesis`, `test_deep_recovery_resumes_remaining_tasks_only`, five `test_usage_report_*`,
  `test_repeated_rate_limits_back_off_exponentially`, `test_usage_separates_actual_and_reserved_tokens`). They are not
  caused by agent mode. Mention them to the user; fix them only if asked.
- **Shell tip:** long bash heredocs in this environment get truncated ("unexpected EOF"). Write a Python patch script to the
  scratchpad or use the Edit tool instead.
- Run tests with `uv run --frozen python -m pytest -q`. To check the frontend: `cd frontend && npm run check && npm run build`
  (`node_modules` is now installed; the `login.astro`/`register.astro` type errors are pre-existing).

## 3. What is done

### Backend (new files)
- `backend/agent.py`: `AgentRun`. It implements the lead-agent loop and these tools: `bash`, `read_file`, `write_file`,
  `edit_file`, `list_files`, `read_url`, `web_search` (only if SearXNG is configured), `todo_write`, `delegate`, and
  `git_push` (only if the workspace has a repo).
  - Parallel sub-agents use `asyncio.TaskGroup` limited by `agent_parallel`.
  - Approvals (`NeedsApproval`, ids `push:<call_id>`, `budget:<n>`, `steps:<limit>`) are stored in run state.
  - Steering reads `steer` events. Compaction summarizes older steps at 60% of the context allowance.
  - Every step is checkpointed in `state["agent"]["transcripts"]`, so a run resumes correctly after a worker crash or an approval.
- `backend/sandbox.py`:
  - `Sandbox` is the HTTP client for sandboxd.
  - Archive helpers: `workspace_path`, `TarBuilder`, `rebuild_tar` (sanitizes members, uid 1000, contained symlinks only),
    `zip_to_tar` (strips a wrapper folder, drops links and `..`), `tar_to_zip`, `tar_file`.
- `backend/sandboxd.py`: a FastAPI app talking to the Docker Engine API over the unix socket.
  - Containers: `CapDrop ALL`, `no-new-privileges`, memory/CPU/pids limits, user 1000, internal network `leo-sandbox`,
    proxy environment variables, volume `leo-ws-<sid>`.
  - Exec: demuxes Docker's stream, uses `timeout --kill-after`, and truncates output in the middle.
  - Imports go through a random `/tmp/leo-import-*` staging folder, then `cp -a` runs as user 1000 (protects against planted symlinks).
  - Git clone and push run in sandboxd with the token passed per command via `http.extraHeader`.
  - Push: the sandbox creates a `git bundle`; sandboxd fetches it into a fresh bare repo and pushes from there.
    Hooks are disabled and pushes are never forced.
  - An idle reaper stops containers after 30 minutes.
- `backend/egress.py`: an asyncio CONNECT/HTTP proxy with an allowlist (npm, PyPI, crates, Go, RubyGems, Maven).
  It also allows `LEO_EGRESS_ALLOW(_EXTRA)` hosts and blocks hosts that resolve to non-public IPs.

### Backend (modified)
- `providers.py`:
  - A neutral transcript format (`assistant.tool_calls`, `role: tool`), converted for Chat Completions, Responses
    (`function_call` / `function_call_output`) and Anthropic (`tool_use` / `tool_result`, with results grouped into one user turn).
  - `tools=` parameter; `Chunk.calls` decoding; a `ToolCalls` assembler.
- `context.py`: `token_bound` now counts tool calls (`message_bytes`).
- `orchestrator.py`:
  - New `sandbox` parameter and `self.agent` flag; `limits()` gives agent runs their own allowance, multiplied by approved extensions.
  - `check()` measures the time limit from `resumed_at`.
  - `candidates(..., tools=)` skips `tools is False` and tries known-good models first; roles `agent` (strongest) and `subagent` (ranked by skill).
  - `call(..., messages=, tools=)` returns `{text, tool_calls, model}` and learns `mark_tools(True/False)` (False on HTTP 400/404 errors that mention tools).
  - `execute()` routes agent runs to `AgentRun`. `SandboxError` messages are shown to the user.
- `store.py`:
  - New `workspaces` table (id = conversation id, encrypted token) and methods `create_workspace` / `get_workspace` / `update_workspace`.
  - `create_run` auto-creates an empty workspace when the mode is agent.
  - Run status `waiting` with `wait()` and `resolve_approval()` (optimistic lock on `updated_at`); `cancel` also stops waiting runs.
  - `steer()`, `events_of()`, `mark_tools()`; `preferences()` merges defaults for new fields.
  - Usage roles: `agent`, `subagent`, `compactor` (stage names `agent:<name>:<step>`).
- `schemas.py`:
  - New fields: `Connection.tools`, `Preferences.agent_*` (max_steps 80, max_calls 200, token_budget 4M, max_cost 1.0,
    max_run_seconds 7200, parallel 3, command_timeout 300); `RunInput.mode` now accepts `"agent"`.
  - New inputs: `WorkspaceInput` (https only, no credentials in the URL, branch validated), `ApprovalInput`, `SteerInput`, `GIT_REF`.
- `config.py`: `sandbox_url` and `sandbox_token` from `LEO_SANDBOX_URL` and `LEO_SANDBOX_TOKEN`.
- `worker.py`: passes the `Sandbox` through; enforces the agent time limit from `resumed_at`.
- `api.py`:
  - `create_app(config, store, sandbox=None)`.
  - Endpoints: `POST /api/workspaces`, `GET /api/workspaces/{id}`, `/files`, `/file?path=`, `PUT /upload` (raw zip body), `GET /download`.
  - Also `POST /api/runs/{id}/approve` and `/steer`.
  - Deleting a conversation deletes its sandbox; `/api/health` now includes `agent`.

### Infrastructure
- `Dockerfile` has stages `python`, `sandboxd` (adds git, runs as root) and `app` (default, last).
- `compose.yaml`:
  - api and worker use `target: app` and get `LEO_SANDBOX_URL` and `LEO_SANDBOX_TOKEN`.
  - New services: `sandboxd` (with the Docker socket, no host port), `egress` (on the default and sandbox networks),
    and `sandbox-image` (builds `leo-sandbox:latest`, then exits).
  - New network `sandbox` (name `leo-sandbox`, `internal: true`). Validated with `docker compose config`.
- `deploy/sandbox/Dockerfile` (python:3.12-slim with git, nodejs, npm, ripgrep, curl; user `agent` uid 1000 owns
  site-packages and /usr/local/bin; pytest preinstalled) and `deploy/sandbox/gitconfig`.
- `.env.example` now documents the `LEO_SANDBOX_*` and `LEO_EGRESS_ALLOW_EXTRA` variables.

### Frontend
- `index.astro`:
  - Agent option in `#composer-mode`; `#new-workspace` button and `#workspace-dialog` (empty or git); `#toggle-workspace` button.
  - `#workspace-panel` (file tree, viewer, upload, download).
  - `#provider-tools` select in the model form; agent limit fields under Limits.
- `chat.ts`:
  - Agent stages and timeline (`listenAgent`: `agent_step`, `agent_tool`, `todos`, `subagent`, `compacted`, `steer`, an approval card with Approve/Decline).
  - Typing while an agent runs sends a steering message instead of stopping it.
  - Workspace panel logic; `'tools'` added to `CONNECTION_FIELDS`, which matters because the toggle PUT would otherwise reset the flag; agent numeric settings.
- `global.css`: styles for the agent feed, todos, approval card and workspace panel. **Type-check is clean for chat.ts and the build succeeds.**
  The UI has **not been tried in a browser yet**.

### Benchmark
- `benchmarks/agent_tasks.py`: tasks `build`, `fix` (textstats with 3 bugs; tests must stay unchanged), and `long` (adds auth to a notes API;
  expects `research` and `coding` sub-agents and at least 50 steps).
- `benchmarks/agent.py`: the runner. It logs in, creates a workspace, uploads files, runs agent mode, auto-answers approvals
  (`--approve-budget`), optionally restarts the worker (`--restart-worker-after`), then writes the hidden tests into the sandbox
  and runs the check through sandboxd. Results go to `benchmarks/results/agent-*.json`.

### Tests (all passing)
- `tests/test_sandbox.py`, 17 tests: protocol conversion and tool-call decoding, archive safety, sandboxd against a fake Docker API, egress allowlist.
- `tests/test_agent.py`, 15 tests: a full agent run with parallel delegation, tool errors, edit uniqueness, push approval
  (approve and decline; token never sent to models), budget extension, steering, crash resume, learning which models lack
  tool support, compaction, no-sandbox error, and API workspace lifecycle, validation, isolation between users, and agent mode without sandboxd.
- `tests/test_agent_benchmark.py`, 3 tests: benchmark sanity checks.
- `tests/test_sandbox_docker.py`: **real Docker tests, not run yet** (skipped unless `LEO_TEST_SANDBOX_URL` is set).
  `tests/compose.sandbox-test.yaml` publishes sandboxd on `127.0.0.1:8100`.
- `README.md` has a new section "Agent mode: multi-agent work in a sandbox" and an updated security section.

## 4. What is left (in order)

1. **Finish the Docker build.** A background `docker compose build sandbox-image sandboxd egress` was still running
   (apt downloads are slow on this network; the log is in the session scratchpad). Re-run it if needed.
2. **Run the real sandbox tests:**
   ```
   docker compose -f compose.yaml -f tests/compose.sandbox-test.yaml up -d --build sandboxd
   LEO_TEST_SANDBOX_URL=http://127.0.0.1:8100 uv run --frozen python -m pytest tests/test_sandbox_docker.py -v
   ```
   Not yet verified against real Docker:
   - Exec start streaming over httpx without connection hijacking: Docker should return the multiplexed body; if it
     doesn't, add the `Connection: Upgrade` / `Upgrade: tcp` headers and read the raw stream.
   - PUT archive keeping uid 1000.
   - DNS resolution of `egress` on the internal network.
   - The proxy environment working for pip.
   - Whether `timeout` kills background jobs.
   - The push test assumes the container is named `leo-sandboxd-1`; check with `docker compose ps` and set `LEO_TEST_SANDBOXD_CONTAINER`.
3. **Finish the review of `backend/agent.py` from `execute()` onward** (the review stopped there). Things to watch:
   - `TypeError` from a handler bug is reported to the model as "invalid arguments".
   - The edge cases in `compact()`'s cut logic.
   - `self.o.status("sandbox")` runs while the workspace status may still be `creating`.
4. **Not built yet (these were in the spec):**
   - Discovery should set `tools` automatically. OpenRouter's `supported_parameters` contains `"tools"`, Anthropic and
     Gemini support it, and the rest stay `None`. This needs changes in `discovery.profile`/`discover`, the
     `DiscoveredModel` schema and `adopt_models`. Today the flag is only learned on the first agent call or set manually.
   - The usage page has no labels yet for the `agent`/`subagent`/`compactor` roles.
   - The MCP server (`backend/mcp_server.py`) does not expose agent mode.
   - Not handled: Anthropic thinking blocks and Responses reasoning items are not passed back between tool turns.
     It works, but it is less efficient for reasoning models.
5. **Full end-to-end check:** `docker compose up -d --build`, open http://localhost:8000, create an agent workspace, and run a small task with a real tool-calling model.
6. **Live benchmark** (it spends the user's model quota, so **ask the user first**):
   `uv run --frozen python -m benchmarks.agent --user <u> --password <p> --sandbox-url http://127.0.0.1:8100 --approve-budget`, and for the long task add `--restart-worker-after 120`.
7. Re-run the full suite and report honestly, including the 9 failures that were already there.
