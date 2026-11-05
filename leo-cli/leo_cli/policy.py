"""What the agent may do on the user's machine without asking.

Reading is always free. In ask mode (the default) edits, commands, and MCP tools that change things need an answer: once,
for this session, always in this project (saved to .leo-cli/settings.json), or no. Autorun skips the questions, except
for actions that are never automatic: installing a skill, pushing, and commands that match the risk list.
"""
import json
import re
from pathlib import Path

GUARDED = {"write_file", "edit_file", "bash", "install_skill", "git_push"}
RISKY = [re.compile(p) for p in (
    r"\bsudo\b",
    r"\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*\s+(-\S+\s+)*(/|~|\$HOME|/\*)(\s|$)",
    r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z|da)?sh\b",
    r"\bgit\s+push\b",
    r"\bmkfs\b|\bdd\s+if=",
    r":\(\)\s*\{",
)]


# Where one command ends and the next begins (2>&1 and &> are redirections, not a background &), and substitutions that
# run a command inside another one.
CHAIN = re.compile(r"&&|\|\|?|;|\n|(?<!>)&(?!>)")
SUBSTITUTION = re.compile(r"`|\$\(|[<>]\(")


def risky(command):
    return any(p.search(command) for p in RISKY)


def compound(command):
    """Whether a command line runs more than one command."""
    return bool(CHAIN.search(command) or SUBSTITUTION.search(command))


class Policy:
    def __init__(self, root, autorun=None):
        self.root = Path(root)
        self.settings_path = self.root / ".leo-cli" / "settings.json"
        saved = self.load()
        self.autorun = saved.get("autorun", False) if autorun is None else autorun
        self.project_commands = list(saved.get("allow", {}).get("commands", []))
        self.project_edits = bool(saved.get("allow", {}).get("edits", False))
        self.project_tools = list(saved.get("allow", {}).get("mcp", []))
        self.session_commands, self.session_edits, self.session_tools = [], False, []
        self.external = None  # name -> {server, tool, read_only, allowed} for MCP tools, set by the session's MCP servers.

    def load(self):
        try:
            value = json.loads(self.settings_path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self):
        settings = self.load()
        settings["autorun"] = self.autorun
        settings["allow"] = {"commands": self.project_commands, "edits": self.project_edits, "mcp": self.project_tools}
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps(settings, indent=2) + "\n")

    def set_autorun(self, on, remember=False):
        self.autorun = on
        if remember:
            self.save()

    def command_allowed(self, command):
        """A remembered rule covers a command it starts, and a chained command only when it covers every part of it:
        allowing `npm test` must not allow `npm test && rm -rf src`."""
        command, rules = command.strip(), self.session_commands + self.project_commands
        if command in rules:
            return True  # Exactly what was approved.
        if SUBSTITUTION.search(command):
            return False
        parts = [part.strip() for part in CHAIN.split(command)]
        return all(part and any(part == rule or part.startswith(rule + " ") for rule in rules) for part in parts)

    def check(self, agent, call):
        """None when the call may run; otherwise the question for the user: {kind, message, detail}."""
        name, args = call["name"], call.get("arguments") or {}
        tool = self.external(name) if self.external and name not in GUARDED else None
        if tool:
            # An MCP tool: one that only reads, or that L.E.O Chat's settings allow, runs; the others ask first.
            if tool["read_only"] or tool["allowed"] or self.autorun or name in self.session_tools + self.project_tools:
                return None
            shown = json.dumps({k: v for k, v in args.items() if k != "_invalid_json"}, indent=2, ensure_ascii=False)
            return {"kind": "mcp", "message": f"Use {tool['tool']} from the {tool['server']} MCP server?",
                    "detail": {"name": name, "server": tool["server"], "tool": tool["tool"], "arguments": shown[:4000]}}
        if name not in GUARDED:
            return None
        if name == "install_skill":
            return {"kind": "install_skill", "message": f"Install the skill from {args.get('source', '')}?", "detail": {"source": args.get("source", "")}}
        if name == "git_push":
            return {"kind": "git_push", "message": f"Push branch {args.get('branch', '')}?", "detail": args}
        if name == "bash":
            command = str(args.get("command", ""))
            if risky(command):
                return {"kind": "command", "message": "Run this command? It matches L.E.O.'s list of risky commands.", "detail": {"command": command, "risky": True}}
            if self.autorun or self.command_allowed(command):
                return None
            return {"kind": "command", "message": "Run this command?", "detail": {"command": command, "risky": False}}
        if self.autorun or self.session_edits or self.project_edits:
            return None
        path = str(args.get("path", ""))
        if name == "edit_file":
            return {"kind": "edit", "message": f"Edit {path}?", "detail": {"path": path, "old": str(args.get("old_string", ""))[:2000],
                                                                         "new": str(args.get("new_string", ""))[:2000]}}
        content = str(args.get("content", ""))
        return {"kind": "edit", "message": f"Write {path}?", "detail": {"path": path, "new": content[:2000], "lines": content.count("\n") + 1}}

    def allow(self, question, scope, prefix=None):
        """Remember a yes beyond this one call: scope is "session" or "project"; commands remember `prefix` (default: the command)."""
        if scope not in ("session", "project") or question["kind"] not in ("command", "edit", "mcp") or question["detail"].get("risky"):
            return
        if question["kind"] == "mcp":
            (self.session_tools if scope == "session" else self.project_tools).append(question["detail"]["name"])
        elif question["kind"] == "edit":
            if scope == "session":
                self.session_edits = True
            else:
                self.project_edits = True
        else:
            rule = (prefix or question["detail"]["command"]).strip()
            (self.session_commands if scope == "session" else self.project_commands).append(rule)
        if scope == "project":
            self.save()
