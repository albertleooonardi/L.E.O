# L.E.O CLI

The multi-agent LLM Efficiency Orchestrator in your terminal. `leo` works on the folder you start it in: it plans
with a todo list, reads and edits files, runs commands and tests, and hands focused subtasks to sub-agents. Every step
is routed to the cheapest capable model you have connected.

It signs in to your L.E.O. server and downloads your models and API keys. After that it runs on your own machine: it
calls API-key models directly, and your code, prompts, and transcripts stay on your computer. Only usage totals (calls,
tokens, cost per model) go back to the server, for the Usage page.

**Subscription models** (ChatGPT, Copilot, and others you signed in to on the website) live on your own 9Router, which
only the L.E.O. server can reach. leo calls them **through your L.E.O. server**, so prompts for those models pass
through it on the way (it stores nothing). `/models` marks them "via your L.E.O. server". The router's key never leaves
the server; leo signs those calls with its device token.

## Install

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). On Windows you also need
[Git for Windows](https://git-scm.com/download/win), because the agent runs its commands in Git Bash.

```bash
npm install -g leo-orchestrator    # or try it first: npx leo-orchestrator
# or with uv directly:
uv tool install "git+https://github.com/albertleooonardi/L.E.O#subdirectory=leo-cli"
uv tool install ./leo-cli          # from a checkout of this repository
```

The npm package is a small launcher: it installs [uv](https://docs.astral.sh/uv/) if you do not have it, installs
L.E.O CLI with uv on first use (uv brings Python too), and runs it. **`leo update`** upgrades L.E.O CLI in place,
however you installed it.

## Sign in

Run `leo`. The first time, it shows the L.E.O welcome screen:

1. **Choose a theme** with the arrow keys and Enter: Dark, Light, High contrast, Terminal colours (your terminal's own
   16 colours), or No colour. A preview shows how the agent's output looks. Change it any time with `/theme`.
2. **Choose Sign in, Use another L.E.O. server, or Quit.** Sign in uses `http://localhost:8000` unless you pick
   another server (or pass `--server`).
3. **Sign in on the L.E.O. website**, which opens in your browser. Sign in or create an account (with a password or
   with Google or GitHub, as on the website), then click **Allow** for this device.
4. The browser comes back to `leo` and the session starts. Your models download to this computer, and API keys go in
   the OS keychain (Windows Credential Manager, macOS Keychain, or Secret Service).

If no browser can open (for example over SSH), `leo` prints the link; open it on any device, and `leo` carries on once
you click Allow.

In a session, `/login [server]` signs in again (another account or server) and `/logout` signs out. From a script,
use `leo login [--server URL]`, `leo sync` (leo also syncs on its own when the models are more than an hour old),
`leo models`, and `leo logout`. You can sign a lost device out under
**Settings → Account → CLI devices**. Keys that were already downloaded still work until you rotate them, so rotate
them for a lost laptop.

## Use

```bash
cd my-project
leo                                  # interactive session on this folder
leo -p "fix the failing tests"       # one task; the answer goes to stdout, exit code 1 if it failed
leo -p "..." --autorun               # unattended: no questions, except for risky actions
leo --worktree                       # work in a new git worktree and branch, leaving your folder alone
leo resume                           # continue the last session on this folder (leo resume 2: the one before)
```

**While the agent works:**
- Each tool call is one line, followed by a short result.
- Typing a message steers the agent.
- Esc stops the current task.
- Ctrl-C twice quits. Unfinished work keeps its progress, and `leo resume` continues it.

**Approvals.** By default, leo asks before it edits a file or runs a command. Answer with one of these keys:

| Key | Meaning |
|---|---|
| `y` | Yes, once. |
| `s` | Yes for this session. For commands, it remembers a prefix such as `npm test`. |
| `p` | Yes, always in this project. Saved to `.leo-cli/settings.json`. |
| `n` | No. Esc also says no. |

Type the key (or its word, such as `yes`) and press Enter. A remembered prefix such as `npm test` never covers a chained
command such as `npm test && rm -rf src`: every part has to be allowed. A sentence typed while the question is shown is
sent to the agent as a message, not read as an answer.

Reading files and searching never ask. `--autorun` (or `/autorun on`) stops the questions, but these always ask, even
with autorun, and are never remembered:
- `sudo`;
- `rm -rf` of `/` or your home folder;
- piping `curl` or `wget` into a shell;
- `git push`;
- installing skills.

**Commands in a session.** Type `/` to see them as you type; Tab completes a command, your skills, and your command
files. The status bar at the bottom shows every session, what the current one is doing, and the keys that work now.

| Command | What it does |
|---|---|
| `/new [--worktree]` | Start another session. Several sessions can run at once. |
| `/sessions` | List the sessions in this window. |
| `/switch N` | Show session N. |
| `/resume [N]` | List earlier sessions on this folder, or reopen one. |
| `/clear` | Start over in this session. |
| `/autorun on\|off [--save]` | Turn the approval questions off or on. |
| `/models [add]` | List your models, or add one with an API key (saved on your L.E.O. account). |
| `/sync` | Fetch model changes made on the website. |
| `/cost` | Show tokens and cost for this session. |
| `/login [server]` | Sign in in the browser, to another account or server. |
| `/logout` | Sign out and remove the synced models and keys. |
| `/theme [name]` | Change the colours: `dark`, `light`, `contrast`, `terminal`, or `mono`. |
| `/help` | List all commands. |
| `/exit` | Quit. |

**Skills and your own commands:**
- **Skills.** A skill is a folder with a `SKILL.md` (front matter `name` and `description`). Put it in
  `~/.config/leo-cli/skills/` (`%APPDATA%\leo-cli\skills\` on Windows) or in the project's `.leo-cli/skills/`.
  The agent loads a skill when the work needs it, and `/name args` asks for it directly.
- **Command files.** `.leo-cli/commands/<name>.md` holds a prompt template for `/name`. `$ARGUMENTS` is replaced with
  what you type after the command.

## Where things are stored

| What | Where |
|---|---|
| Settings, sessions, and synced models | `~/.config/leo-cli/` (`%APPDATA%\leo-cli\` on Windows). Set `LEO_CLI_HOME` to change it. |
| Per-project rules, skills, commands, and worktrees | `.leo-cli/` in the project; add it to `.gitignore` if you like. |

**Safety.** There is no sandbox: the agent works as you, on your files. Paths stay inside the project folder, and links
that point out of it are refused. The approval questions are the safety layer, so keep autorun for projects you trust.

## Development

From the repository root, `uv run --frozen python -m pytest leo-cli/tests` runs the CLI's tests. The CLI reuses the
server's engine (`backend/`) in-process: routing, the agent loop, budgets, and checkpoints. `leo_cli/workspace.py`
replaces the Docker sandbox with your folder.

Windows notes:
- Commands run in Git Bash, found next to `git.exe`. Set `LEO_CLI_BASH` to use another `bash.exe`.
- Use Windows Terminal or PowerShell for the interactive prompt; the mintty window of Git Bash is not a full console.
