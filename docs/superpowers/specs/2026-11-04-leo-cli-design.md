# L.E.O CLI: the multi-agent orchestrator in your terminal

**Date:** 4 November 2026  
**Status:** approved in conversation; implementation follows directly (the user asked to skip the separate plan step)  
**Part of:** L.E.O. as two products on one account ([accounts](2026-11-04-accounts-oauth-design.md),
[Chat](2026-11-04-leo-chat-design.md)). The CLI is the multi-agent LLM Efficiency Orchestrator: a terminal agent that
works on the local machine.

## 1. Decisions

| Question | Decision |
|---|---|
| How models are reached | Directly from the laptop. After login the CLI syncs the user's connections, keys included. |
| What goes back to the server | Usage totals only. Code, prompts, and transcripts stay local. |
| Permissions | Ask before edits and commands (default), with an autorun setting. |
| Language and engine | Python, reusing the server's agent engine in-process with a local SQLite store. |
| Where it lives | A `leo-cli/` folder: its own package (`leo-cli`, module `leo_cli`) in a uv workspace with the engine. |
| Extras | Agent skills (`SKILL.md`) that also become `/commands`, installable by the agent. |
| Multitasking | Several sessions at once, in the same folder by default, or `--worktree` for an isolated git worktree. |

**Out of version 1:**
- 9Router subscription models, which are reachable only on the server's network;
- a daemon that keeps sessions running after the terminal closes;
- budgets shared across devices.

## 2. Commands and experience

**Commands:**
- **Account and models:** `leo login [--server URL]`, `leo logout`, `leo sync`, `leo models`.
- **Running:** `leo` (interactive session on the current folder), `leo -p "task"` (one-shot; prints the answer; exits
  non-zero on failure), `leo --autorun`, `leo resume`.

**In a session:**
- **Built-in commands:**
  - `/new [--worktree]`, `/sessions`, `/switch N`, `/resume`, `/clear`;
  - `/autorun on|off`, `/models`, `/cost`, `/help`, `/exit`.
- **Your own commands:** every agent skill and every command file becomes a `/command`.
- **Display:** answers stream in. Each tool call is one line, and the todo list and sub-agent progress update in place.
- **Approvals** appear inline: once, this session, always in this project, or deny.
- **While it works:** typing steers the agent, Esc stops the current run, and pressing Ctrl-C twice quits.
- **Status bar:** shows sessions waiting for approval.

**Install:** `uv tool install ./leo-cli` (or from git with `--subdirectory leo-cli`). The engine package moves FastAPI,
uvicorn, psycopg, and bcrypt into a `server` extra, which the Dockerfile installs.

**Files:**
- `~/.config/leo-cli/` holds `config.json` (server URL, device name, last sync) and `leo.db` (sessions, synced
  connections, queued usage).
- The OS keychain (`keyring`) holds the device token and the Fernet key for synced API keys. Without a keychain these go
  in a mode-0600 file, with a warning.
- Per project, `.leo-cli/` holds `settings.json` (autorun and allow rules), `skills/`, `commands/`, and `worktrees/`.

## 3. Login, sync, and usage (server)

**Device login:**
1. `POST /api/cli/device` (public, rate-limited) returns `{device_code, user_code: "ABCD-EFGH", verification_url:
   <server>/cli?code=…, interval, expires_in: 600}`.
2. The `/cli` page asks for sign-in if needed, then shows "Allow L.E.O CLI on `<device>` to download your models and
   API keys?" with Allow and Deny.
3. `POST /api/cli/token {device_code}` answers `pending`, `denied`, or `expired`, or returns the token once (single use).
4. The token is 256 bits. Its SHA-256 digest is stored in `cli_devices` (id, user_id, name, token_hash, created_at,
   last_used_at, revoked_at).

**Scope:** the device token works only as `Authorization: Bearer` on `/api/cli/*`. It is never a web session.

**`GET /api/cli/sync`** returns:
- the enabled connections with decrypted API keys (`key_from` resolved), excluding the user's 9Router connections,
  which are listed separately as `chat_only`;
- the preferences (routing, limits, agent limits);
- the routing skill catalog;
- the device name.

**`POST /api/cli/usage`** takes batches of `{session, model, connection, input_tokens, output_tokens, cost, estimated,
calls, at}`.
- They are stored in `cli_usage`.
- The Usage page shows "L.E.O CLI" per model.
- A `401` means the device was revoked.

**Settings → Account → CLI devices:** name, added, last used, and Revoke. The page notes that revoking stops syncing,
but keys already on that laptop stay usable until `leo logout`, so rotate keys for a lost device.

## 4. Engine (shared code)

**Running in the CLI:**
- The CLI builds the normal `Store` on SQLite in `~/.config/leo-cli/`, with `LEO_ENCRYPTION_KEY` from the keychain.
  One local user owns everything.
- A session is a conversation, and each message is an agent run in it.
- Each session runs `worker.process_one(store, owner, transport, workspace)` as an asyncio task.
- On start, runs left `running` by a closed terminal go back to the queue.

**The workspace object carries everything new.** A missing attribute means today's server behaviour:
- `root`: the folder the agent is told about and works in (`/workspace` in the sandbox).
- `policy`: permission checks before edits, commands, push, and skill installs (`None` on the server).
- `skills`: agent skills offered through `use_skill` and `install_skill` (none on the server).

**`LocalWorkspace`** implements the six operations agent mode uses:
- `ensure`, `exec` (`bash -c` in the root, inheriting the environment `leo` started with, with a timeout killing the process group, output limits), `read_file`,
  `write_file`, `list_files` (confined to the root after resolving symlinks; skipping `.git`, `node_modules`, and
  virtual environments), and `push` (the user's own git and credentials).

**Permissions** (`policy.py`):
- **Always free:** reading, listing, web search, and reading pages.
- **Ask mode:** edits, writes, and commands pause the run (`NeedsApproval`, kind `edit` or `command`). The answers are
  once, this session (for commands: the exact command or a confirmed prefix), always in this project (saved to
  `.leo-cli/settings.json`), or deny.
- **Autorun:** everything runs except the always-ask list: `git_push`, `install_skill`, and risky commands (`sudo`,
  `rm -rf` on `/`, `~` or `$HOME`, piping `curl` or `wget` into a shell, writes outside the project).
- **Commands run as the user with no sandbox.** The prompts are the safety layer.

**Agent skills** (`skills.py`):
- `SKILL.md` folders with `name` and `description` front matter, in `~/.config/leo-cli/skills/` and `.leo-cli/skills/`
  (the project's copy wins).
- The lead agent's instructions list each skill's name and description.
- `use_skill(name)` returns `SKILL.md` and the skill's file list. Scripts run through the command permissions.
- `install_skill(source)` copies a local folder, or clones a git URL, after an approval that shows the name,
  description, and files.
- `/name args` sends "Use the `<name>` skill: args". Command files (`commands/<name>.md`, with `$ARGUMENTS`) expand into
  the message.
- These are separate from Chat's routing skills.

**Multitasking:**
- Sessions share the SQLite store (WAL mode).
- Each session records the files it writes, and a write to a file another open session wrote shows a warning in both.
- `/new --worktree` runs `git worktree add .leo-cli/worktrees/<name> -b leo/<name>` and uses that folder as the root.
  Closing such a session shows how to merge the branch.

## 5. Package layout

```
leo-cli/
  pyproject.toml          # name leo-cli; depends on leo-orchestrator (workspace), rich, prompt_toolkit, keyring
  leo_cli/
    app.py                # entry point `leo`: commands, one-shot mode
    account.py            # device login, sync into the local store, usage queue and upload, keychain
    sessions.py           # sessions as tasks over the local store, switching, worktrees, collisions
    workspace.py          # LocalWorkspace
    policy.py             # permissions
    skills.py             # skill and command discovery, use_skill and install_skill
    ui.py                 # terminal rendering and input
  tests/
```

## 6. Errors

- **Server unreachable:** the CLI keeps working with the last synced models, and usage waits in the local store.
- **Revoked device:** a `401` says "This device was signed out; run `leo login`".
- **Rejected key:** the usual fallback, plus a hint to run `leo sync`.
- **Paths outside the project, or a denied action:** a tool error returned to the agent.
- **`--worktree` failures:** a message, and the session falls back to the folder.

## 7. Testing

- **`LocalWorkspace`:** confinement (including symlink escapes), process-group timeout, output limits, and the
  real-path root in the instructions.
- **Policy:** ask, session and project rules, and the autorun always-ask list.
- **Server:**
  - device flow: allow, deny, expiry, single use;
  - token scope: only `/api/cli/*`;
  - sync: decrypted keys, 9Router excluded;
  - a revoked device gets `401`;
  - usage reaches the Usage page.
- **In-process sessions with a fake model:**
  - an edit after approval, and autorun;
  - a skill used by the agent and as a `/command`, and `install_skill` asking first;
  - two sessions with a collision warning;
  - a `--worktree` session in a temporary git repo.
- **Packaging:** build and install into a fresh environment; `leo --help` and `leo -p` against a mock server.
- **Server regression:** the existing agent tests pass unchanged.
