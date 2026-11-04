# Agent mode: multi-agent coding in a sandbox

**Date:** 4 November 2026 · **Status:** approved for build (sandbox per session via `sandboxd`, L.E.O.-native agent loop)

## Goal

L.E.O. becomes a model-agnostic autonomous coding agent in the spirit of Claude Code and Antigravity: it works inside a
real workspace, runs long tasks, and delegates to sub-agents, while keeping L.E.O.'s strengths (routing every step to
the cheapest capable model, hard budgets, durable runs).

## Decisions

| Topic | Decision |
|---|---|
| Workspace | One container per workspace (= conversation), created by `sandboxd` |
| Network | Sandboxes sit on an internal Docker network; only an egress proxy that allows package registries is reachable |
| Autonomy | Free inside the sandbox; approval before `git push` and before going past the run budget; steer or stop any time |
| Models | Native tool calling only; connections carry `tools: true/false/null` (null = untried, learned on first use) |
| Topology | One main agent with a `delegate` tool; skills are the sub-agent types; independent delegations run in parallel |
| In / out | Start empty, from an uploaded zip, or from a git clone; leave as a zip download or a git push |
| Success | 3-task benchmark (`benchmarks/agent.py`): build from scratch, fix a repo, long multi-agent task with a worker restart |

## Components

- **`providers.py`** – a neutral transcript format (`assistant.tool_calls`, `tool` results) converted to Chat
  Completions, Responses, and Anthropic Messages; streamed tool-call deltas are assembled into complete calls.
- **`sandboxd.py`** – small FastAPI service, the only holder of the Docker socket. Creates one locked-down container
  per session (no capabilities, no-new-privileges, CPU/memory/pids limits, non-root user, workspace volume), runs
  commands with a hard timeout, reads/writes files through the archive API, imports/exports tar, and does git clone and
  push itself so tokens never enter the sandbox (push goes through a `git bundle`, never the sandbox's `.git/config`).
- **`egress.py`** – CONNECT/HTTP proxy with a host allowlist (npm, PyPI, crates, Go proxy by default).
- **`sandbox.py`** – the client the worker and API use to talk to `sandboxd`.
- **`agent.py`** – the loop: think → call tools → observe, with a todo list, context compaction, steering messages,
  approvals, and sub-agents. Every step is checkpointed in the run state, so a restarted worker resumes the transcript.
- **Store / API** – `workspaces` table (source, repo, encrypted git token); run status `waiting` for approvals;
  endpoints to create/upload/download/browse workspaces, approve, and steer.
- **UI** – Agent mode, workspace picker, file tree and viewer, agent timeline (steps, tools, todos, sub-agents),
  approval card, steer box, zip download.

## Limits and known trade-offs

- Agent runs have their own limits (steps, calls, tokens, cost, wall time); exceeding one asks to extend instead of failing.
- A tool call can run twice if the worker dies after it ran but before the checkpoint; sandbox commands should be idempotent where it matters.
- Disk quota per workspace is not enforced by Docker on most hosts; `sandboxd` reports workspace size instead.
- Sandbox isolation is Docker's (runc). For hostile multi-tenant use, run sandboxes under gVisor (`LEO_SANDBOX_RUNTIME=runsc`).
