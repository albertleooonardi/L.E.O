"""What the agent may do on the user's machine without asking.

Reading is always free. In ask mode (the default) edits and commands need an answer: once, for this session, always in
this project (saved to .leo-cli/settings.json), or no. Autorun skips the questions, except for actions that are never
automatic: installing a skill, pushing, and commands that match the risk list.
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


def risky(command):
    return any(p.search(command) for p in RISKY)


class Policy:
    def __init__(self, root, autorun=None):
        self.root = Path(root)
        self.settings_path = self.root / ".leo-cli" / "settings.json"
        saved = self.load()
        self.autorun = saved.get("autorun", False) if autorun is None else autorun
        self.project_commands = list(saved.get("allow", {}).get("commands", []))
        self.project_edits = bool(saved.get("allow", {}).get("edits", False))
        self.session_commands, self.session_edits = [], False

    def load(self):
        try:
            value = json.loads(self.settings_path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self):
        settings = self.load()
        settings["autorun"] = self.autorun
        settings["allow"] = {"commands": self.project_commands, "edits": self.project_edits}
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps(settings, indent=2) + "\n")

    def set_autorun(self, on, remember=False):
        self.autorun = on
        if remember:
            self.save()

    def command_allowed(self, command):
        command = command.strip()
        return any(command == p or command.startswith(p + " ") for p in self.session_commands + self.project_commands)

    def check(self, agent, call):
        """None when the call may run; otherwise the question for the user: {kind, message, detail}."""
        name, args = call["name"], call.get("arguments") or {}
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
        if scope not in ("session", "project") or question["kind"] not in ("command", "edit") or question["detail"].get("risky"):
            return
        if question["kind"] == "edit":
            if scope == "session":
                self.session_edits = True
            else:
                self.project_edits = True
        else:
            rule = (prefix or question["detail"]["command"]).strip()
            (self.session_commands if scope == "session" else self.project_commands).append(rule)
        if scope == "project":
            self.save()
