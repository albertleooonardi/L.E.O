# leo-orchestrator

L.E.O CLI, the multi-agent LLM Efficiency Orchestrator, in your terminal.

```bash
npx leo-orchestrator          # try it without installing
npm install -g leo-orchestrator
leo                           # then, in any project folder
leo update                    # upgrade to the latest version
```

L.E.O CLI is a Python program. This package is a small launcher:
1. It finds [uv](https://docs.astral.sh/uv/), and installs it for your user if it is missing.
2. On first use it installs L.E.O CLI with `uv tool install` (uv also provides Python if you have none).
3. It runs `leo` with your arguments.

`leo update` upgrades L.E.O CLI in place. `npm update -g leo-orchestrator` only updates this launcher.

Set `LEO_CLI_SOURCE` to install from somewhere else, for example a fork or a local checkout
(`LEO_CLI_SOURCE=./leo-cli`).

On Windows you also need [Git for Windows](https://git-scm.com/download/win): the agent runs its commands in Git Bash.
See the [L.E.O CLI guide](https://github.com/albertleooonardi/L.E.O/tree/main/leo-cli) for signing in and using it.
