"""The terminal: what the agent is doing, approval questions, and the prompt.

Events come from the local store, the same event log the web app streams, so this module only reads and renders them.
The prompt stays usable while sessions work: typing steers the current one, Esc stops it, a status bar lists every
session, and an approval question takes over the prompt as soon as the current session asks one.

The visual grammar, used everywhere:

    ● tool(argument)              a step of the lead agent
      └ result                    its result (✗ when it failed)
      │ #1a2b ● tool(argument)    a step of sub-agent 1a2b, on its rail
      ▲ warning   ◇ note   ✔ ok   ✗ error
    ── ✔ done · 7 calls · 12.9K tokens · $0.0123 · 42s ──   the end of a turn

Text that came from outside (paths, commands, model output) is always printed as Text, never as Rich markup, so a
"[y]" or "[add]" in it shows as typed.
"""
import asyncio
import difflib
import io
import os
import shutil
import sys
import time
from types import SimpleNamespace

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application, get_app
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import FormattedTextControl, Layout, Window
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts.choice_input import ChoiceInput
from prompt_toolkit.styles import DynamicStyle
from prompt_toolkit.utils import get_cwidth
from rich import box
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from . import NAME, theme, treeview
from .account import AccountError
from .mentions import attach, complete
from .policy import compound

TERMINAL = {"completed", "failed", "cancelled"}
INTERRUPTED = "\0interrupted"  # The prompt was closed to show an approval question.
CHOICES = {"y": "once", "s": "session", "p": "project", "n": "deny"}
KINDS = {"mcp": "MCP tool", "git_push": "push", "install_skill": "skill install"}  # What an approval is for, in words.
ANSWERS = {"once": "yes", "session": "yes, this session", "project": "yes, always in this project", "deny": "no"}
HELP = [
    ("Sessions", [("/new [--worktree]", "start another session (in its own git worktree with --worktree)"),
                  ("/sessions", "list sessions in this window"), ("/switch N", "show session N"),
                  ("/resume [N]", "list earlier sessions on this folder, or reopen one"),
                  ("/cd PATH", "work on another folder in this window"), ("/clear", "start over in this session"),
                  ("/tree", "draw the agents of the latest task as a tree")]),
    ("Models and account", [("/models [add]", "list your models, or add some (account sign-in or API key)"),
                            ("/sync", "fetch model changes made on the website"), ("/cost", "tokens and cost of this session"),
                            ("/usage", "calls, tokens, and cost of every model on this device"),
                            ("/context", "how full the agent's context window is, and with what"),
                            ("/mcp", "MCP servers: L.E.O Chat's connectors and the CLI's own"),
                            ("/login [server]", "sign in in the browser (another account or server)"),
                            ("/logout", "sign out and remove the synced models")]),
    ("Settings", [("/autorun on|off [--save]", "run edits and commands without asking (risky ones still ask)"),
                  ("/theme [name]", "change the colours (dark, light, contrast, terminal, mono)")]),
    ("L.E.O", [("/help", "this list"), ("/exit", "quit (Ctrl-C twice also quits)")]),
]
KEYS = [("Enter", "send a task; while the agent works, your message steers it"), ("Esc", "stop the current task"),
        ("@path", "attach a file's text or a folder's listing to your message, e.g. @src/app.py"),
        ("Tab", "complete a /command or an @path"), ("Up", "earlier messages"), ("Ctrl-C x2", "quit; unfinished sessions keep their progress")]
LETTERS = {"L": ["██╗     ", "██║     ", "██║     ", "██║     ", "███████╗", "╚══════╝"],
           "E": ["███████╗", "██╔════╝", "█████╗  ", "██╔══╝  ", "███████╗", "╚══════╝"],
           "O": [" ██████╗ ", "██╔═══██╗", "██║   ██║", "██║   ██║", "╚██████╔╝", " ╚═════╝ "],
           "C": [" ██████╗", "██╔════╝", "██║     ", "██║     ", "╚██████╗", " ╚═════╝"],
           "D": ["██████╗ ", "██╔══██╗", "██║  ██║", "██║  ██║", "██████╔╝", "╚═════╝ "],
           ".": ["   ", "   ", "   ", "   ", "██╗", "╚═╝"]}
BANNER = ["".join(LETTERS[letter][row] for letter in NAME) for row in range(6)]
LOGO_WIDTH = 80  # A narrower terminal gets the name as text.

UNICODE = dict(tee="├─", elbow="└─", pipe="│  ", wave="▁▂▃▄▅▆▇█▇▆▅▄▃▂", dot="●", hook="└", rail="│", agent="◆", ok="✔", fail="✗", warn="▲", info="◇", idle="○", now="▸",
               prompt="❯", sep="·", bar="━", track="─", rule="─", spinner="⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏", box=box.ROUNDED, ellipsis="…")
ASCII = dict(tee="|-", elbow="`-", pipe="|  ", wave="_.-~'~-.", dot="*", hook="`-", rail="|", agent="#", ok="+", fail="x", warn="!", info="-", idle="o", now=">",
             prompt=">", sep="-", bar="#", track="-", rule="-", spinner="|/-\\", box=box.ASCII, ellipsis="...")


def glyphs(console=None, unicode=None):
    """The symbols to draw with: Unicode on a UTF-8 terminal, plain ASCII elsewhere."""
    if unicode is None:
        unicode = "utf" in ((console.encoding if console else None) or "utf-8").lower()
    return SimpleNamespace(**(UNICODE if unicode else ASCII))


def banner():
    """The name in block letters, shaded top to bottom in the theme's colours."""
    return Text("\n").join(Text(line, style=f"leo.banner{i}") for i, line in enumerate(BANNER))


def clip(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def clip_left(text, limit):
    """Text that keeps its end when it is too long, for paths: the folder name matters more than where it lives."""
    text = str(text)
    return text if len(text) <= limit else "…" + text[len(text) - limit + 1:]


def plural(count, word, many=None):
    return f"{count} {word if count == 1 else many or word + 's'}"


def tokens(n):
    return f"{n / 1000:.1f}K" if n >= 1000 else str(n)


def money(cost):
    cost = cost or 0
    return "$0" if not cost else f"${cost:.4f}" if cost < 1 else f"${cost:.2f}"


def duration(seconds):
    return f"{int(seconds // 60)}m {int(seconds % 60):02d}s" if seconds >= 60 else f"{seconds:.0f}s"


def usage_line(usage, seconds=None, sep="·"):
    total = (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
    parts = [f"{usage.get('calls', 0)} calls", f"{'~' if usage.get('estimated') else ''}{tokens(total)} tokens",
             money(usage.get("estimated_cost"))]
    if seconds is not None:
        parts.append(duration(seconds))
    return f" {sep} ".join(parts)


def home_path(path):
    """A path with the home folder shortened to ~."""
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path and (path == home or path.startswith(home + os.sep)) else path


def suggested_prefix(command):
    """What to remember for "allow commands like this": the program and its subcommand, e.g. `npm test` or `pytest`."""
    words = command.strip().split()
    if not words or compound(command):
        return command.strip()  # Chained commands are remembered exactly as approved.
    keep = words[:2] if len(words) > 1 and not words[1].startswith(("-", "/", ".", "'", '"')) and "=" not in words[1] else words[:1]
    return " ".join(keep)


def split_blocks(text):
    """Streamed Markdown split where it is safe to print: (finished blocks, the block still being written).

    A block ends at a blank line or a closing code fence; a code block is never cut in two."""
    cut = position = 0
    fence = False
    for line in text.splitlines(keepends=True):
        position += len(line)
        if not line.endswith("\n"):
            break  # The line is still being written.
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            fence = not fence
            if not fence:
                cut = position
        elif not stripped and not fence:
            cut = position
    return text[:cut], text[cut:]


def key_hint(pairs):
    """Keys as chips, e.g. ` y  yes   n  no`, for a line printed by rich."""
    text = Text()
    for key, label in pairs:
        text.append(f" {key} ", style="leo.key")
        text.append(f" {label}   ")
    return text


class Renderer:
    """Turns a session's events into lines on the terminal."""

    def __init__(self, console, root=None, unicode=None):
        self.console, self.root = theme.apply(console), root
        self.g = glyphs(console, unicode)
        self.model = None
        self.started = None  # When the current turn's first event arrived, for the elapsed time at the end.
        self.activity = None  # The latest tool call, for the status bar.
        self.parallel = {}  # agent -> how many tool calls its latest step made: their results then say which call they belong to.
        self.pending = {}  # stage -> streamed text not printed yet (the block still being written)
        self.streamed = {}  # stage -> all the text streamed for it
        self.answer = None  # The lead agent's latest streamed text: the answer, when the run ends after it.
        self.phase = None  # thinking, writing, or running, for the status bar
        self.printed = set()  # Stages that have printed a block of their streamed text.
        self.tree = {}  # The agents of the current run, from its tree events.
        self.doing = {}  # agent -> its latest tool call, for the tree.

    def event(self, kind, data):
        for item in self.render(kind, data):
            self.console.print(item)

    def render(self, kind, data):
        """The renderables for one event (empty for events that show nothing)."""
        show = getattr(self, f"on_{kind}", None)
        if not show:
            return []
        if self.started is None and kind != "done":
            self.started = time.monotonic()
        return show(self.short(data)) or []

    def reset(self):
        self.model = self.started = self.activity = self.answer = self.phase = None
        self.parallel, self.pending, self.streamed, self.printed = {}, {}, {}, set()
        self.tree, self.doing = {}, {}

    @property
    def preview(self):
        """The last line of the block being written, for the status bar."""
        for text in reversed(list(self.pending.values())):
            lines = [line for line in text.splitlines() if line.strip()]
            if lines:
                return lines[-1].strip()
        return ""

    def short(self, value):
        """Paths inside the project as relative paths: the agent needs them absolute, a reader does not."""
        if not self.root:
            return value
        if isinstance(value, str):
            return value.replace(self.root + os.sep, "").replace(self.root, ".")
        if isinstance(value, dict):
            return {k: self.short(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.short(v) for v in value]
        return value

    # -- the gutter -------------------------------------------------------------------

    @staticmethod
    def tag(agent):
        return f"#{agent[4:8]}"

    def step_prefix(self, agent):
        if agent == "main":
            return Text()
        return Text.assemble((f"  {self.g.rail} ", "leo.accent"), (f"{self.tag(agent)} ", "leo.accent"))

    def result_prefix(self, agent):
        if agent == "main":
            return Text("  ")
        return Text.assemble((f"  {self.g.rail} ", "leo.accent"), " " * (len(self.tag(agent)) + 3))

    def line(self, glyph, text, style="leo.muted", indent="  "):
        return Text.assemble((f"{indent}{glyph} ", style), (text, style))

    # -- events -----------------------------------------------------------------------

    def block(self, stage, text):
        """One finished Markdown block of streamed text; blocks of the same answer keep a blank line between them."""
        gap = [Text("")]  # Paragraphs printed one by one still read as paragraphs.
        self.printed.add(stage)
        return gap + [Markdown(text.strip("\n"), code_theme=theme.code())]

    def on_agent_text(self, data):
        """The lead agent's text as it is written: whole blocks are printed, the unfinished one waits in the status bar."""
        stage = data.get("stage") or "main"
        if data.get("reset"):
            self.pending.pop(stage, None)
            self.streamed.pop(stage, None)
            if stage in self.printed:
                self.printed.discard(stage)
                return [self.line(self.g.info, "that model stopped; another one starts the answer again")]
            return []
        self.phase = "writing"
        text = data.get("text", "")
        self.streamed[stage] = self.streamed.get(stage, "") + text
        ready, self.pending[stage] = split_blocks(self.pending.get(stage, "") + text)
        return self.block(stage, ready) if ready.strip() else []

    def flush(self, stage):
        """What is left of a stage's streamed text, printed when its step ends."""
        rest = self.pending.pop(stage, "")
        return self.block(stage, rest) if rest.strip() else []

    def on_tree(self, data):
        self.tree[data["id"]] = data  # Drawn above the prompt, not printed.

    def on_model(self, data):
        self.phase = "thinking"
        if str(data.get("stage", "")).startswith("agent:main") and data.get("model") != self.model:
            self.model = data.get("model")
            return [Text.assemble((f"  {self.g.info} model ", "leo.muted"), (str(self.model), "leo.accent"))]

    def on_agent_step(self, data):
        out, agent = [], data.get("agent") or "main"
        stage = f"agent:{agent}:{data.get('step')}"
        self.phase = "running" if data.get("tools") else self.phase
        if stage in self.streamed:  # Its text was printed as it was written: print the rest, never all of it again.
            out += self.flush(stage)
            self.answer = self.streamed.pop(stage)
            self.printed.discard(stage)
        elif agent == "main" and data.get("text") and data.get("tools"):
            # Thinking out loud between tool calls, a step back from the answer; the final answer comes with done.
            out.append(Padding(Markdown(data["text"], code_theme=theme.code(), style="leo.muted"), (0, 0, 0, 2)))
        calls = data.get("tools") or []
        self.parallel[agent] = sum(1 for call in calls if call["name"] != "todo_write")
        for call in calls:
            summary = f"({clip(call['summary'], 100)})" if call.get("summary") else ""
            self.activity = self.doing[agent] = call["name"] + summary
            if call["name"] == "todo_write":
                continue  # The plan itself is shown by its todos event.
            line = self.step_prefix(agent)
            line.append(f"{self.g.dot} ", style="leo.lead" if agent == "main" else "leo.accent")
            line.append(call["name"], style="bold")
            line.append(summary)
            out.append(line)
        return out

    def on_agent_tool(self, data):
        output = str(data.get("output") or "")
        lines, name, agent = output.splitlines(), data.get("name"), data.get("agent") or "main"
        if name == "todo_write" and data.get("ok"):
            return []  # The plan was just shown.
        prefix = self.result_prefix(agent)
        hook = f"{self.g.hook} "
        if self.parallel.get(agent, 0) > 1:  # Several calls ran at once: say which one this result is for.
            label = data.get("summary") if len(data.get("summary") or "") > 1 else name  # "list_files" says more than ".".
            hook += f"{clip(label, 40)} {self.g.sep} "
        if not data.get("ok"):
            return [prefix + Text(f"{hook}{self.g.fail} {clip(output, 160)}", style="leo.error")]
        if name == "bash" and lines:
            body = [l for l in lines[1:] if l.strip() and l.strip() != "(no output)"]
            failed = not lines[0].startswith("exit code 0")
            note = lines[0] + (f" {self.g.sep} {plural(len(body), 'line')} of output" if body else "")
            out = [prefix + (Text(f"{hook}{self.g.fail} {note}", style="leo.warn") if failed else Text(f"{hook}{note}", style="leo.muted"))]
            shown = body[-6:] if failed else body[-3:]  # The end of the output says how it went.
            for line in shown:
                out.append(prefix + Text(f"    {clip(line, 160)}", style="leo.muted"))
            return out
        if name == "read_file":
            read = [l for l in lines if l[:1].isdigit()]
            return [prefix + Text(f"{hook}{plural(len(read), 'line') if read else clip(output, 140)}", style="leo.muted")]
        if lines and lines[0].startswith("[MCP result from"):  # The note for the model, not for you.
            lines = lines[1:]
        if name == "list_files" and len(lines) > 1:
            entries = [l for l in lines if not l.startswith("…")]
            return [prefix + Text(f"{hook}{plural(len(entries), 'entry', 'entries')}", style="leo.muted")]
        return [prefix + Text(f"{hook}{clip(lines[0] if lines else 'done', 140)}", style="leo.muted")]

    def on_todos(self, data):
        items = data.get("items") or []
        if not items:
            return []
        done = sum(1 for item in items if item["status"] == "completed")
        filled = round(20 * done / len(items))
        out = [Text.assemble(("  Plan ", "bold"), (self.g.bar * filled, "leo.ok"), (self.g.track * (20 - filled), "leo.border"),
                             (f" {done}/{len(items)}", "leo.muted"))]
        for item in items:
            if item["status"] == "completed":
                out.append(Text(f"  {self.g.ok} {item['content']}", style="leo.muted"))
            elif item["status"] == "in_progress":
                out.append(Text.assemble((f"  {self.g.now} ", "leo.accent"), (item["content"], "bold")))
            else:
                out.append(Text(f"  {self.g.idle} {item['content']}"))
        return out

    def on_subagent(self, data):
        tag = self.tag(data["agent"])
        if data.get("status") == "started":
            return [Text.assemble((f"  {self.g.agent} ", "leo.accent"), (f"{data.get('skill') or 'general'} sub-agent ", "bold"),
                                  (tag, "leo.accent"), "  ", clip(data.get("task", ""), 120))]
        return [Text.assemble((f"  {self.g.agent} ", "leo.accent"), (f"{tag} finished", "leo.accent"), "  ",
                              (clip(data.get("report", ""), 140), "leo.muted"))]

    def on_compacted(self, data):
        return [self.line(self.g.info, "older steps were summarized to save context")]

    def on_warning(self, data):
        return [self.line(self.g.warn, str(data.get("message", "")), "leo.warn")]

    def on_steer(self, data):
        return [Text.assemble((f"  {self.g.prompt} you: ", "leo.accent"), (clip(data.get("message", ""), 160), "leo.muted"))]

    def on_approval_resolved(self, data):
        kind = data.get("kind")
        what = f"{KINDS.get(kind, kind)} " if kind else ""
        if data.get("approved"):
            return [self.line(self.g.ok, f"{what}approved", "leo.ok")]
        return [self.line(self.g.fail, f"{what}declined", "leo.warn")]

    def on_done(self, data):
        status, out = data.get("status"), []
        for stage in list(self.pending):
            out += self.flush(stage)
        if status == "completed" and data.get("output"):
            streamed, output = (self.answer or "").strip(), data["output"].strip()
            if streamed and output.startswith(streamed):  # It streamed in already; only what the engine added is new.
                rest = output[len(streamed):].strip()
                out += [Text(""), Markdown(rest, code_theme=theme.code()), Text("")] if rest else [Text("")]
            else:
                out += [Text(""), Markdown(data["output"], code_theme=theme.code()), Text("")]
        elif status == "failed":
            out.append(Text(f"{self.g.fail} {data.get('error') or 'The run failed'}", style="leo.error"))
        elif status == "cancelled":
            out.append(Text(f"{self.g.warn} Stopped.", style="leo.warn"))
        out.append(self.footer(data))
        self.started = self.activity = self.answer = self.phase = None
        self.pending, self.streamed, self.printed = {}, {}, set()
        self.tree, self.doing = {}, {}
        return out

    def footer(self, data):
        """The rule that ends a turn: how it ended, and what it cost."""
        status = data.get("status")
        glyph, word, style = {"completed": (self.g.ok, "done", "leo.ok"), "failed": (self.g.fail, "failed", "leo.error"),
                              "cancelled": (self.g.warn, "stopped", "leo.warn")}.get(status, (self.g.info, str(status), "leo.muted"))
        title = Text.assemble((f" {glyph} {word}", style))
        seconds = time.monotonic() - self.started if self.started is not None else None
        seconds = seconds if seconds is not None and seconds >= 1 else None  # An instant turn needs no clock.
        if (data.get("usage") or {}).get("calls"):
            title.append(f" {self.g.sep} {usage_line(data['usage'], seconds, self.g.sep)}", style="leo.muted")
        elif seconds is not None:
            title.append(f" {self.g.sep} {duration(seconds)}", style="leo.muted")
        title.append(" ")
        return Rule(title, align="left", style="leo.border", characters=self.g.rule)


def approval_view(approval, unicode=True):
    """The question, with what exactly would happen: the command, the change, or the push."""
    g = glyphs(unicode=unicode)
    detail = approval.get("detail") or {}
    kind = approval.get("kind")
    risky = bool(detail.get("risky"))
    parts = [Text(approval.get("message", ""), style="bold"), Text("")]
    if kind == "command":
        parts.append(Syntax(detail.get("command", ""), "bash", theme=theme.code(), word_wrap=True, background_color="default"))
        if risky:
            parts += [Text(""), Text(f"{g.warn} This matches L.E.O.'s list of risky commands, so it is always asked and never remembered.",
                                     style="leo.error")]
    elif kind == "edit" and detail.get("old") is not None:
        path = detail.get("path", "")
        diff = list(difflib.unified_diff(detail["old"].splitlines(), detail.get("new", "").splitlines(), path, path, lineterm=""))[2:]
        added = sum(1 for l in diff if l.startswith("+"))
        removed = sum(1 for l in diff if l.startswith("-"))
        parts[-1] = Text.assemble((path, "leo.accent"), "  ", (f"+{added}", "leo.ok"), " ", (f"-{removed}", "leo.error"))
        more = len(diff) - 60
        body = "\n".join(diff[:60]) + (f"\n… {more} more lines" if more > 0 else "")
        parts += [Text(""), Syntax(body, "diff", theme=theme.code(), word_wrap=True, background_color="default")]
    elif kind == "edit":
        path = detail.get("path", "")
        preview = detail.get("new", "").splitlines()
        count = detail.get("lines") or len(preview)
        parts[-1] = Text.assemble((path, "leo.accent"), (f"  new file · {count} line{'s' if count != 1 else ''}", "leo.muted"))
        more = count - 20
        code = "\n".join(preview[:20])
        lexer = Syntax.guess_lexer(path, code) if path else "text"
        parts += [Text(""), Syntax(code, lexer, theme=theme.code(), word_wrap=True, background_color="default")]
        if more > 0:
            parts.append(Text(f"… {more} more lines", style="leo.muted"))
    elif kind == "mcp":
        parts += [Syntax(detail.get("arguments") or "{}", "json", theme=theme.code(), word_wrap=True, background_color="default")]
    elif detail:
        table = Table.grid(padding=(0, 2))
        table.add_column(style="leo.muted")
        table.add_column()
        for key, value in detail.items():
            if value not in (None, ""):
                table.add_row(Text(str(key)), Text(str(value)))
        parts.append(table)
    what = KINDS.get(kind, kind or "action")
    title = Text(f"{g.warn} Risky command {g.sep} always asked" if risky else f"{g.warn} Approval needed {g.sep} {what}",
                 style="leo.error" if risky else "leo.warn")
    title.stylize("bold")
    return Panel(Group(*parts), box=g.box, border_style="leo.error" if risky else "leo.warn", title=title, title_align="left",
                 padding=(1, 2))


def remembers(approval):
    """Whether "this session" and "always in this project" are offered (never for risky commands, pushes, or installs)."""
    return approval.get("kind") in ("command", "edit", "mcp") and not (approval.get("detail") or {}).get("risky")


def answers(approval):
    """The keys this approval accepts, and what each one means."""
    return CHOICES if remembers(approval) else {"y": "once", "n": "deny"}


def parse_answer(text, options):
    """The key an approval answer means: the key itself or its word ("y", "yes", "p", "project"), nothing else.

    Only the whole answer counts, never its first letter: "please also run the tests", typed to steer the agent just
    as the question appeared, must not answer "p" (yes, always in this project)."""
    answer = text.strip().lower()
    if answer.isdigit() and 1 <= int(answer) <= len(options):
        return list(options)[int(answer) - 1]  # The numbers shown beside the answers.
    words = {"yes": "y", "no": "n", "session": "s", "project": "p"}
    answer = words.get(answer, answer)
    return answer if answer in options else None


def menu_labels(approval, prefix=None):
    """What each answer does, said in full for the menu."""
    kind, detail = approval.get("kind"), approval.get("detail") or {}
    if kind == "mcp":
        what = f"{detail.get('tool')} from {detail.get('server')}"
    elif kind == "edit":
        what = "file edits"
    else:
        what = f"commands starting with `{prefix}`" if prefix and not compound(prefix) else "this exact command"
    labels = {"once": "Yes", "session": f"Yes, and allow {what} for the rest of this session",
              "project": f"Yes, and always allow {what} in this project", "deny": "No, tell the agent to do it differently  (esc)"}
    return [(decision, labels[decision]) for decision in answers(approval).values()]


async def choose_answer(approval, prefix=None, g=None, guard=0.4):
    """The answer to an approval, from an arrow-key menu: Up/Down or a number moves, Enter chooses, a number key
    chooses at once, Esc or Ctrl-C says no.

    For `guard` seconds after the menu appears Enter and number keys do nothing: keys typed to steer the agent just as
    the question appeared must not answer it."""
    g = g or glyphs()
    options = menu_labels(approval, prefix)
    shown = time.monotonic()
    early = Condition(lambda: time.monotonic() - shown < guard)
    keys = KeyBindings()

    @keys.add("enter", filter=early, eager=True)
    def _too_soon(event):
        pass

    @keys.add("escape", eager=True)
    def _deny(event):
        event.app.exit(result="deny")

    for number, (decision, _) in enumerate(options, 1):
        @keys.add(str(number), filter=~early, eager=True)
        def _choose(event, decision=decision):
            event.app.exit(result=decision)

    try:
        answer = await ChoiceInput(message=[("class:prompt", "Allow this?")], options=options, default="once", style=theme.prompt_style(),
                                   symbol=g.prompt, key_bindings=keys).prompt_async()
    except (EOFError, KeyboardInterrupt):
        answer = None
    return answer or "deny"


def choices_text(approval):
    return "  ".join(f"[{key}] {ANSWERS[decision]}" for key, decision in answers(approval).items())


async def pick(message, options, default=None):
    """An arrow-key menu (Enter chooses, or type the option's number); None if cancelled with Ctrl-C."""
    try:
        return await ChoiceInput(message=message, options=options, default=default, style=theme.prompt_style()).prompt_async()
    except (EOFError, KeyboardInterrupt):
        return None


async def pick_many(message, options, keep=None, height=12):
    """A checklist: type to filter, Up/Down move, Space ticks, Enter takes the ticked rows (or, with none ticked, the
    highlighted one). None if cancelled with Esc or Ctrl-C. The `keep` row stays visible whatever the filter."""
    g, query, index, ticked = glyphs(), "", 0, set()

    def rows():
        needle = query.lower()
        return [o for o in options if o[0] == keep or needle in f"{o[0]} {o[1]}".lower()]

    def text():
        shown, start = rows(), max(0, index - height + 1)
        out = [("class:prompt", message), ("class:muted", f"  {len(ticked)} ticked" if ticked else ""), ("", "\n")]
        out += [("class:muted", "  filter: "), ("", query + "\n")] if query else []
        for number, (value, label) in enumerate(shown[start:start + height], start):
            here = number == index
            mark = f"{g.ok} " if value in ticked else f"{g.idle} "
            out += [("class:selected-option" if here else "", f"  {g.now if here else ' '} {mark}{label}\n")]
        if not shown:
            out.append(("class:muted", "    nothing matches\n"))
        more = len(shown) - start - height
        out.append(("class:muted", (f"    {g.ellipsis} {more} more\n" if more > 0 else "") + "  type to filter · space ticks · enter adds · esc cancels"))
        return out

    keys = KeyBindings()

    def move(step):
        nonlocal index
        index = max(0, min(len(rows()) - 1, index + step))

    keys.add("up")(lambda event: move(-1))
    keys.add("down")(lambda event: move(1))
    keys.add("pageup")(lambda event: move(-height))
    keys.add("pagedown")(lambda event: move(height))

    @keys.add(" ")
    def _tick(event):
        shown = rows()
        if shown:
            ticked.symmetric_difference_update({shown[index][0]})

    @keys.add("enter")
    def _done(event):
        shown = rows()
        if ticked or shown:
            event.app.exit(result=[value for value, _ in options if value in ticked] or [shown[index][0]])

    @keys.add("escape", eager=True)
    @keys.add("c-c")
    def _cancel(event):
        event.app.exit(result=None)

    @keys.add("backspace")
    def _erase(event):
        nonlocal query, index
        query, index = query[:-1], 0

    @keys.add(Keys.Any)
    def _filter(event):
        nonlocal query, index
        if event.data.isprintable():
            query, index = query + event.data, 0

    app = Application(layout=Layout(Window(FormattedTextControl(text, focusable=True, show_cursor=False), dont_extend_height=True, wrap_lines=False)),
                      key_bindings=keys, style=theme.prompt_style(), full_screen=False, erase_when_done=True)
    return await app.run_async()


def theme_preview(unicode=True):
    """A few lines exactly as the agent prints them (the same renderer), in the active theme."""
    renderer = Renderer(Console(file=io.StringIO()), unicode=unicode)
    diff = "@@ -1 +1 @@\n-print('hello')\n+print('hello from leo')"
    items = [*renderer.render("todos", {"items": [{"content": "Say hello", "status": "completed"},
                                                  {"content": "Run it", "status": "in_progress"}]}),
             *renderer.render("agent_step", {"agent": "main", "tools": [{"name": "edit_file", "summary": "greet.py"}]}),
             Padding(Syntax(diff, "diff", theme=theme.code(), background_color="default"), (0, 0, 0, 4)),
             *renderer.render("agent_step", {"agent": "main", "tools": [{"name": "bash", "summary": "python greet.py"}]}),
             *renderer.render("agent_tool", {"agent": "main", "name": "bash", "ok": True, "output": "exit code 0\nhello from leo"}),
             *renderer.render("subagent", {"agent": "sub-1a2b3c4d", "skill": "coding", "status": "finished", "report": "tests pass"}),
             *renderer.render("warning", {"message": "Rate limited; switching to the next model"}),
             Text(f"{renderer.g.fail} Example error", style="leo.error"),
             renderer.footer({"status": "completed", "usage": {"calls": 3, "input_tokens": 2400, "output_tokens": 180,
                                                               "estimated_cost": 0.0021}})]
    label = theme.THEMES[theme.current()]["label"]
    return Panel(Group(*items), box=renderer.g.box, title=f"Theme: {label}", title_align="left", border_style="leo.brand",
                 padding=(1, 2))


async def choose_theme(account, console, current=None):
    """Ask for a theme, save it, and show what it looks like; None if cancelled."""
    options = [(key, f"{spec['label']} · {spec['description']}") for key, spec in theme.THEMES.items()]
    picked = await pick("Choose the look that suits your terminal (you can change it later with /theme):", options,
                        default=current or theme.current())
    if picked is None:
        return None
    console.push_theme(theme.use(picked))
    account.save(theme=picked)
    console.print(theme_preview(glyphs(console).dot == UNICODE["dot"]))
    return picked


def liquid_wave(g, frame, width=8):
    """A wave rolling through the status bar while the agent works, coloured along the theme's gradient."""
    colors = theme.liquid()
    out = []
    for i in range(width):
        char = g.wave[(frame + i) % len(g.wave)]
        if colors:
            gradient = colors[0]
            out.append((f"fg:{gradient[(i + frame // 3) % len(gradient)]}", char))
        else:
            out.append(("class:tb.spin", char))
    return out


def shimmer(text, frame):
    """Text with a band of light sliding across it, like light on moving liquid."""
    colors = theme.liquid()
    span = len(text) + 12
    peak = frame % span - 6
    out = []
    for i, char in enumerate(text):
        light = max(0.0, 1 - abs(i - peak) / 5)
        if colors:
            out.append((f"fg:{theme.mix(colors[1], colors[2], light)}" + (" bold" if light > 0.6 else ""), char))
        else:
            out.append(("class:tb.spin bold" if light > 0.5 else "class:tb", char))
    return out


def fit(fragments, width, style="class:tb"):
    """prompt_toolkit fragments cut to `width` cells, ending in … when something was cut."""
    out, used = [], 0
    for fragment_style, text in fragments:
        size = get_cwidth(text)
        if used + size <= width:
            out.append((fragment_style, text))
            used += size
            continue
        room = width - used - 1
        while text and get_cwidth(text) > room:
            text = text[:-1]
        if text:
            out.append((fragment_style, text))
        out.append((style, "…"))
        break
    return out


SUBCOMMANDS = {"/models": [("add", "sign in with an account, or add models with an API key")],
               "/model": [("add", "sign in with an account, or add models with an API key")],
               "/autorun": [("on", "run edits and commands without asking"), ("off", "ask before edits and commands")]}


class SlashCompleter(Completer):
    """Completes /commands, your skills, and your command files, with what each one does; and @paths to attach."""

    def __init__(self, app):
        self.app = app

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        word = text.split()[-1] if text and not text[-1].isspace() else ""
        if word.startswith("@"):
            for replacement, label in complete(word, self.app.root()):
                yield Completion(replacement, start_position=-len(word), display=label)
            return
        command, space, typed = text.partition(" ")
        if space and " " not in typed:
            for name, description in SUBCOMMANDS.get(command, []):
                if name.startswith(typed):
                    yield Completion(name, start_position=-len(typed), display_meta=description)
            return
        if not text.startswith("/") or space:
            return
        for usage, description in self.app.command_list():
            name = usage.split()[0]
            if name.startswith(text):
                yield Completion(name, start_position=-len(text), display=usage, display_meta=description)


class App:
    """The interactive session: prompt, background event printing, approvals, and slash commands."""

    def __init__(self, sessions, account, console=None):
        self.sessions, self.account = sessions, account
        self.console = theme.apply(console or Console())
        self.renderer = Renderer(self.console, sessions.project)
        self.cursors, self.notified, self.saved_text, self.last_interrupt = {}, set(), "", 0.0
        self.history = []  # Earlier sessions listed by /resume, so /resume N refers to that list.
        self.prompt = None
        self._commands = (0.0, [])
        self._node_usage = (0.0, None, {})

    @property
    def g(self):
        return self.renderer.g

    def root(self):
        """The folder the current session works in (its worktree, if it has one)."""
        current = self.sessions.current
        return current.root if current else self.sessions.project

    # -- printing -----------------------------------------------------------------

    def say(self, text, style="leo.muted"):
        """One line; ok, warning, and error lines get their symbol."""
        glyph = {"leo.ok": self.g.ok, "leo.warn": self.g.warn, "leo.error": self.g.fail}.get(style)
        self.console.print(Text(f"{glyph} {text}" if glyph else str(text), style=style))

    def notify(self, session, message):
        prefix = f"[{session.number}] " if session is not None and len(self.sessions.items) > 1 else ""
        self.say(prefix + message, "leo.warn")

    def answer(self, markdown):
        self.console.print(Markdown(markdown, code_theme=theme.code()))

    def follow(self, session):
        """Print new events: the current session in full, others only when they need you or finish."""
        if not session.run_id:
            return
        cursor = self.cursors.get(session.run_id, 0)
        while True:
            batch = self.sessions.events(session, cursor)
            for event in batch:
                cursor = event["id"]
                if session is self.sessions.current:
                    self.renderer.event(event["kind"], event["data"])
                    if event["kind"] == "done":
                        tree = tree_panel(session.run, self.sessions.store, self.g, self.console.width)
                        if tree:
                            self.console.print(tree)
                elif event["kind"] == "approval":
                    self.say(f"Session {session.number} is waiting for your approval: /switch {session.number}", "leo.warn")
                elif event["kind"] == "done":
                    self.say(f"Session {session.number} {event['data'].get('status')}: /switch {session.number} to see it", "leo.warn")
            self.cursors[session.run_id] = cursor
            if len(batch) < 200:
                return

    def skip_history(self, session):
        """Show only what happens from now on (used when reopening or switching to a session)."""
        if session.run_id:
            events = self.sessions.store.get_events(session.run_id, self.cursors.get(session.run_id, 0))
            while events:
                self.cursors[session.run_id] = events[-1]["id"]
                events = self.sessions.store.get_events(session.run_id, self.cursors[session.run_id])

    async def watch(self):
        while True:
            try:
                for session in list(self.sessions.items):
                    self.follow(session)
                current = self.sessions.current
                approval = current.approval if current else None
                if approval and approval["id"] not in self.notified and self.prompt and self.prompt.app.is_running:
                    # Close the prompt (keeping what was typed) so the loop can ask the question.
                    self.saved_text = self.prompt.default_buffer.text
                    self.interrupt(self.prompt.app)
            except Exception as exc:  # Printing must never take the session down.
                self.say(f"(display error: {exc})", "leo.error")
            await asyncio.sleep(0.15)

    # -- prompt -------------------------------------------------------------------

    @staticmethod
    def interrupt(app):
        """Close the prompt to show something else, leaving no half-typed prompt line behind on the screen."""
        app.erase_when_done = True
        app.exit(result=INTERRUPTED)

    def busy(self):
        current = self.sessions.current
        return bool(current and current.busy)

    def completes_while_typing(self):
        """Not while the agent works (Tab still completes): the room that keeps free for the menu is the tree's."""
        return not self.busy()

    def size(self):
        """The terminal's columns and rows."""
        try:
            found = get_app().output.get_size()
            return found.columns, found.rows
        except Exception:
            found = shutil.get_terminal_size()
            return found.columns, getattr(found, "lines", 24)

    def node_usage(self):
        """What each agent of the current run has used, looked up at most once a second: this runs on every redraw."""
        current = self.sessions.current
        run_id = current.run_id if current else None
        stamp, known, usage = self._node_usage
        if known != run_id or time.monotonic() - stamp > 1:
            usage = treeview.agent_usage(self.sessions.store.call_rows(run_id)) if run_id else {}
            self._node_usage = (time.monotonic(), run_id, usage)
        return usage

    def message(self):
        if self.busy():
            width, height = self.size()
            tree = treeview.live(self.renderer.tree, self.node_usage(), width, height, self.g, int(time.monotonic() * 8), self.renderer.doing)
            return tree + [("class:prompt.steer", f"steer {self.g.prompt} ")]
        return [("class:prompt", f"{self.g.prompt} ")]

    def placeholder(self):
        if self.busy():
            return [("class:placeholder", "type to steer the agent · Esc stops it")]
        return [("class:placeholder", "describe a task · @ attaches a file · / for commands")]

    def live(self, width):
        """While the current session works: a rolling wave, what it is doing (shimmering), how long, and the line the
        agent is writing right now."""
        frame, r = int(time.monotonic() * 12), self.renderer
        seconds = time.monotonic() - r.started if r.started else 0
        if r.phase == "writing" and r.preview:
            label = "Writing"
        elif r.phase == "running" and r.activity:
            label = f"Running {clip(r.activity, 48)}"
        else:
            label = "Thinking"
        line = [("", " ")] + liquid_wave(self.g, frame) + [("", "  ")] + shimmer(label + "…", frame)
        line.append(("class:tb", f"  {duration(seconds)}" if seconds >= 1 else ""))
        if r.phase == "writing" and r.preview:
            line.append(("class:tb", f"  {self.g.sep}  "))
            line.append(("class:placeholder", r.preview))
        return fit(line, width - 1)

    def toolbar(self):
        """Every session with its state on the left, the mode and the keys that matter now on the right; above them,
        while the agent works, a live line with what it is doing."""
        g, left = self.g, [("class:tb", " ")]
        spin = g.spinner[int(time.monotonic() * 8) % len(g.spinner)]
        for session in self.sessions.items:
            current = session is self.sessions.current
            if session.approval:
                left += [("class:tb.wait", f" {g.warn} {session.number} needs approval "), ("class:tb", "  ")]
                continue
            icon, style = ((spin, "class:tb.spin") if session.busy else
                           (g.ok, "class:tb.ok") if session.status == "completed" else
                           (g.fail, "class:tb.error") if session.status in ("failed", "cancelled") else (g.idle, "class:tb"))
            left += [(style, f"{icon} "), ("class:tb.current" if current else "class:tb", f"{session.number} {clip(session.title or 'new', 24)}")]
            if current:
                detail = clip(self.renderer.activity, 40) if session.busy and self.renderer.activity else session.status
                left.append(("class:tb", f" · {detail}" + (f" · {session.branch}" if session.branch else "")))
            left.append(("class:tb", "  "))
        if not self.sessions.items:
            left.append(("class:tb", "no session yet"))
        try:
            width = get_app().output.get_size().columns
        except Exception:
            width = shutil.get_terminal_size().columns
        keys = ([("class:tb.key", "esc"), ("class:tb", " stop  "), ("class:tb.key", "enter"), ("class:tb", " steer ")] if self.busy() else
                [("class:tb.key", "/help"), ("class:tb", "  "), ("class:tb.key", "tab"), ("class:tb", " complete ")])
        mode = [("class:tb.autorun", "autorun on"), ("class:tb", "  ")] if self.sessions.policy.autorun else []
        if not mode and width >= 80:
            mode = [("class:tb", "asks before edits"), ("class:tb", "  ")]
        right = mode + keys
        # The sessions give way first on a narrow terminal: the keys that work now must stay visible.
        left = fit(left, max(width - get_cwidth("".join(t for _, t in right)) - 4, 8))
        gap = width - sum(get_cwidth(t) for _, t in left + right) - 1
        bar = left + [("class:tb", " " * max(gap, 1))] + right
        return self.live(width) + [("", "\n")] + bar if self.busy() else bar

    def command_list(self):
        """Built-in commands, then your skills and command files (looked up again every few seconds)."""
        stamp, cached = self._commands
        if time.monotonic() - stamp > 5:
            skills, commands = self.sessions.skills.discover(), self.sessions.skills.commands()
            cached = [(f"/{name}", (skills.get(name) or {}).get("description") or f"command file {commands[name].name}")
                      for name in sorted(set(skills) | set(commands))]
            self._commands = (time.monotonic(), cached)
        return [row for _, rows in HELP for row in rows] + cached

    def keys(self):
        bindings = KeyBindings()

        @bindings.add("escape", eager=True, filter=Condition(lambda: bool(self.sessions.current and
                                                                          (self.sessions.current.busy or self.sessions.current.approval))))
        def _stop(event):
            self.sessions.stop(self.sessions.current)
            self.saved_text = event.app.current_buffer.text
            self.interrupt(event.app)
            self.say("Stopping…", "leo.warn")

        @bindings.add("c-c")
        def _interrupt(event):
            buffer = event.app.current_buffer
            if buffer.text:
                buffer.reset()
            elif time.monotonic() - self.last_interrupt < 2:
                event.app.exit(exception=EOFError())
            else:
                self.last_interrupt = time.monotonic()
                self.say("Press Ctrl-C again to quit (sessions keep their progress: leocode resume).", "leo.warn")

        return bindings

    async def run(self):
        color, width = self.console.color_system, shutil.get_terminal_size().columns
        with patch_stdout(raw=True):
            # Print through prompt_toolkit's stdout, so output appears above the prompt instead of through it.
            self.console = theme.apply(Console(file=sys.stdout, force_terminal=True, color_system=color or "standard", width=width))
            self.renderer = Renderer(self.console, self.sessions.project)
            self.sessions.notify = self.notify
            self.prompt = PromptSession(history=FileHistory(str(self.account.home / "history")), key_bindings=self.keys(),
                                        bottom_toolbar=self.toolbar, refresh_interval=0.08, style=DynamicStyle(theme.prompt_style),
                                        completer=SlashCompleter(self), complete_while_typing=Condition(self.completes_while_typing),
                                        placeholder=self.placeholder)
            watcher = asyncio.create_task(self.watch())
            try:
                await self.loop()
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
                await self.sessions.close()
        if any(s.status in ("running", "queued", "waiting") for s in self.sessions.items):
            self.say("Unfinished sessions keep their progress; continue with: leocode resume")

    async def loop(self):
        while True:
            current = self.sessions.current
            approval = current.approval if current else None
            if approval and approval["id"] not in self.notified:
                await self.ask(current, approval)
                continue
            text, self.saved_text = self.saved_text, ""
            self.prompt.app.erase_when_done = False  # A sent message stays on the screen, above what it started.
            try:
                text = await self.prompt.prompt_async(self.message, default=text)
            except EOFError:
                return
            except KeyboardInterrupt:
                continue
            if text == INTERRUPTED:
                continue
            text = text.strip()
            if not text:
                continue
            try:
                if text.startswith("/"):
                    if await self.command(text) == "exit":
                        return
                    continue
                await self.submit(text)
            except Exception as exc:  # One failed command must not close the window and every session in it.
                self.say(f"{text.split()[0] if text.startswith('/') else 'That message'} failed: {exc}", "leo.error")

    async def ask(self, session, approval):
        """Show an approval question as a menu (arrows or a number, then Enter; Esc says no) and send the answer."""
        self.notified.add(approval["id"])
        self.console.print(approval_view(approval, unicode=self.g.dot == UNICODE["dot"]))
        command = (approval.get("detail") or {}).get("command") if approval.get("kind") == "command" else None
        prefix = suggested_prefix(command) if command else None
        decision = await choose_answer(approval, prefix, self.g)
        if session.approval and session.approval["id"] == approval["id"]:
            self.sessions.answer(session, decision, prefix if decision in ("session", "project") else None)

    async def submit(self, text):
        session = self.sessions.current or await self.sessions.open()
        text, notes = attach(text, session.root)
        for note in notes:
            self.console.print(self.renderer.line(self.g.info, f"attached {note}"))
        if session.busy or session.approval:
            # A message sent while the agent works, or just as it asks a question, steers it: it reads the message
            # before its next step. (A new run cannot start while this one waits for an answer.)
            self.sessions.steer(session, text)
            return
        await self.sessions.send(session, text)

    # -- slash commands -------------------------------------------------------------

    async def command(self, text):
        name, _, arguments = text[1:].partition(" ")
        arguments = arguments.strip()
        handler = getattr(self, f"cmd_{name}", None) if name.isidentifier() else None  # Skill names with dashes are never built-ins.
        if handler:
            return await handler(arguments)
        expanded = self.sessions.skills.expand(name, arguments)
        if expanded is None:
            self.say(f"Unknown command /{name}. /help lists commands; your skills and command files are listed there too.", "leo.error")
            return None
        await self.submit(expanded)

    async def cmd_help(self, arguments):
        def section(title, rows, key_style="leo.accent", chips=False):
            table = Table.grid(padding=(0, 2))
            table.add_column(min_width=26)
            table.add_column(style="leo.muted")
            for usage, description in rows:
                table.add_row(Text(f" {usage} ", style="leo.key") if chips else Text(usage, style=key_style), Text(description))
            return [Text(title, style="leo.title"), Padding(table, (0, 0, 0, 2)), Text("")]

        parts = []
        for title, rows in HELP:
            parts += section(title, rows)
        mine = self.command_list()[sum(len(rows) for _, rows in HELP):]
        if mine:
            parts += section("Your skills and commands", mine)
        parts += section("Keys", KEYS, chips=True)
        self.console.print(Panel(Group(*parts[:-1]), box=self.g.box, border_style="leo.border", title="L.E.O.C.O.D.E · help",
                                 title_align="left", padding=(1, 2)))

    async def cmd_exit(self, arguments):
        return "exit"

    cmd_quit = cmd_exit

    async def cmd_new(self, arguments):
        session = await self.sessions.open(worktree="--worktree" in arguments)
        self.renderer.reset()
        where = f" on branch {session.branch} in {session.root}" if session.branch else ""
        self.say(f"Session {session.number}{where}. Earlier sessions keep running; /sessions lists them.", "leo.ok")

    def status_cell(self, session):
        if session.approval:
            return Text(f"{self.g.warn} needs approval", style="leo.warn")
        glyph, style = {"completed": (self.g.ok, "leo.ok"), "failed": (self.g.fail, "leo.error"), "cancelled": (self.g.warn, "leo.warn"),
                        "idle": (self.g.idle, "leo.muted")}.get(session.status, (self.g.dot, "leo.accent"))
        return Text(f"{glyph} {session.status}", style=style)

    async def cmd_sessions(self, arguments):
        if not self.sessions.items:
            self.say("No sessions yet: type a task to start one.")
            return
        table = Table(box=box.SIMPLE_HEAD, header_style="leo.muted", border_style="leo.border", pad_edge=False, show_edge=False)
        for column in ("", "#", "Session", "Status", "Branch"):
            table.add_column(column)
        for session in self.sessions.items:
            current = session is self.sessions.current
            table.add_row(Text(self.g.prompt if current else " ", style="leo.accent"), str(session.number),
                          Text(session.title or "new session", style="bold" if current else ""), self.status_cell(session),
                          Text(session.branch or "", style="leo.muted"))
        self.console.print(table)
        self.say("Show one with /switch N.")

    async def cmd_switch(self, arguments):
        if not arguments:
            return await self.cmd_sessions(arguments)
        session = next((s for s in self.sessions.items if str(s.number) == arguments), None)
        if not session:
            self.say("Usage: /switch N (see /sessions)", "leo.error")
            return
        self.sessions.current = session
        self.renderer.reset()
        self.renderer.tree = dict(treeview.nodes_of(session.run))
        self.say(session.label(), "leo.ok")
        run = session.run
        if run and run["status"] in TERMINAL and run.get("output"):
            self.answer(run["output"])  # Its events went by while another session was shown.

    async def cmd_resume(self, arguments):
        if not arguments:
            self.history = self.sessions.history()
            if not self.history:
                self.say("No earlier sessions on this folder.")
                return
            table = Table(box=box.SIMPLE_HEAD, header_style="leo.muted", border_style="leo.border", pad_edge=False, show_edge=False)
            for column in ("#", "Session", "Last active"):
                table.add_column(column)
            for number, item in enumerate(self.history[:20], 1):
                table.add_row(Text(str(number), style="leo.accent"), Text(item["title"] or "untitled"),
                              Text(time.strftime("%d %b %H:%M", time.localtime(item["updated_at"])), style="leo.muted"))
            self.console.print(table)
            self.say("Reopen one with /resume N.")
            return
        if not self.history:
            self.history = self.sessions.history()
        try:
            item = self.history[int(arguments) - 1]
        except (ValueError, IndexError):
            self.say("Usage: /resume N (see /resume)", "leo.error")
            return
        await self.reopen(item["id"])

    async def reopen(self, conversation_id):
        session = next((s for s in self.sessions.items if s.id == conversation_id), None)
        if session:
            self.sessions.current = session
        else:
            session = self.sessions.resume(conversation_id)
            self.skip_history(session)
        self.renderer.reset()
        conversation = self.sessions.store.conversation(conversation_id, self.sessions.uid)
        last = next((m for m in reversed(conversation["messages"]) if m["role"] == "assistant"), None) if conversation else None
        self.say(f"Resumed: {session.title}", "leo.ok")
        if last:
            self.answer(last["content"])
        if session.busy:
            self.say("Its last task was interrupted and continues now.", "leo.warn")

    async def cmd_cd(self, arguments):
        """Move this window to another folder: new sessions work there, with that folder's own rules and skills."""
        if not arguments:
            self.say(f"Working in {home_path(self.sessions.project)}. Usage: /cd PATH")
            return
        folder = os.path.expanduser(arguments.strip().strip('"'))
        folder = os.path.realpath(folder if os.path.isabs(folder) else os.path.join(self.root(), folder))
        if not os.path.isdir(folder):
            self.say(f"No such folder: {home_path(folder)}", "leo.error")
            return
        if any(s.busy or s.approval for s in self.sessions.items):
            self.say("A session is still working; stop it with Esc (or wait for it) before changing folders.", "leo.error")
            return
        from .sessions import Sessions  # sessions imports the engine; this module stays light.
        old = self.sessions
        await old.close()
        self.sessions = Sessions(self.account, folder, transport=old.transport, notify=old.notify)
        self.renderer = Renderer(self.console, folder)
        self.cursors, self.history, self._commands = {}, [], (0.0, [])
        mode = "autorun on" if self.sessions.policy.autorun else "asks before edits and commands"
        self.say(f"Now working in {home_path(folder)} ({mode}).", "leo.ok")
        if old.items:
            self.say(f"Sessions on {home_path(old.project)} are saved; /cd back and /resume to reopen them.")

    async def cmd_clear(self, arguments):
        current = self.sessions.current
        if current and (current.busy or current.approval):
            self.say("This session is working; stop it with Esc first, or open another with /new.", "leo.error")
            return
        if current:
            current.id = current.run_id = current.title = None
            current.written = set()
        self.renderer.reset()
        self.say("Started over: the next message begins a new conversation.", "leo.ok")

    async def cmd_autorun(self, arguments):
        words = arguments.split()
        if not words or words[0] not in ("on", "off"):
            self.say(f"Autorun is {'on' if self.sessions.policy.autorun else 'off'}. Usage: /autorun on|off [--save]")
            return
        self.sessions.policy.set_autorun(words[0] == "on", remember="--save" in words)
        state = "on: edits and commands run without asking; risky commands, pushes, and skill installs still ask" if words[0] == "on" \
            else "off: edits and commands ask first"
        self.say(f"Autorun {state}{' (saved for this project)' if '--save' in words else ''}.", "leo.ok")

    async def cmd_models(self, arguments):
        if arguments == "add":
            from .models import add_model  # models imports this module.
            try:
                await add_model(self.account, self.console, self.sessions.transport)
            except AccountError as exc:
                self.say(str(exc), "leo.error")
            return
        if arguments:
            self.say("Use /models to list your models, /models add to add some, or /sync to fetch changes from the website.", "leo.warn")
            return
        self.console.print(models_table(self.sessions.store, self.sessions.uid, self.g))
        self.say("Add more with /models add: sign in with ChatGPT, Copilot, and others, or use an API key.")

    cmd_model = cmd_models

    async def cmd_sync(self, arguments):
        try:
            summary = await self.account.sync()
        except AccountError as exc:
            self.say(str(exc), "leo.error")
            return
        self.say(f"Synced {summary['models']} models.", "leo.ok")

    async def cmd_login(self, arguments):
        from .app import login  # app imports this module.
        try:
            await login(self.account, self.console, arguments or None, inside=True)
        except AccountError as exc:
            self.say(str(exc), "leo.error")
        except asyncio.CancelledError:
            self.say("Sign-in cancelled.", "leo.warn")

    async def cmd_theme(self, arguments):
        if arguments and arguments not in theme.THEMES:
            self.say(f"Unknown theme {arguments!r}; choose one of: {', '.join(theme.THEMES)}.", "leo.error")
            return
        if arguments:
            self.console.push_theme(theme.use(arguments))
            self.account.save(theme=arguments)
            self.console.print(theme_preview(self.g.dot == UNICODE["dot"]))
        else:
            await choose_theme(self.account, self.console)

    async def cmd_logout(self, arguments):
        await self.account.logout()
        self.say("Signed out; the models and API keys synced to this device were removed. /login signs in again.", "leo.ok")

    async def cmd_usage(self, arguments):
        from .panels import usage_view  # Imports the engine's agent module; loaded when first asked for.
        current = self.sessions.current
        self.console.print(usage_view(self.sessions.store, self.sessions.uid, current.id if current else None,
                                      [c["id"] for c in self.sessions.history()], self.g,
                                      tree=tree_panel(current.run if current else None, self.sessions.store, self.g, self.console.width, always=True)))

    async def cmd_tree(self, arguments):
        current = self.sessions.current
        tree = tree_panel(current.run if current else None, self.sessions.store, self.g, self.console.width, always=True)
        if tree:
            self.console.print(tree)
        else:
            self.say("No task yet in this session: send one, then /tree draws its agents.")

    async def cmd_context(self, arguments):
        from .panels import context_view
        current = self.sessions.current
        self.console.print(Panel(context_view(current.run if current else None, self.sessions.store, self.sessions.uid,
                                              self.renderer.model, self.g),
                                 box=self.g.box, border_style="leo.border", padding=(1, 2), expand=False))

    async def cmd_mcp(self, arguments):
        from . import mcp
        servers = mcp.configured(self.sessions.project, self.account.home)
        if servers:
            self.say(f"Checking {plural(len(servers), 'MCP server')}…")
        results = await asyncio.gather(*(mcp.check(server) for server in servers))
        self.console.print(mcp.view(mcp.chat_connectors(self.account.home), servers, results, self.g, self.account.home,
                                    self.sessions.project))

    async def cmd_cost(self, arguments):
        current = self.sessions.current
        totals = self.sessions.cost(current) if current else None
        if not totals or not totals.get("calls"):
            self.say("No model calls in this session yet.")
            return
        self.console.print(Text.assemble(("Session cost  ", "bold"), usage_line(
            {"calls": totals["calls"], "input_tokens": totals["input_tokens"], "output_tokens": totals["output_tokens"],
             "estimated_cost": totals["cost"], "estimated": totals["estimated"]})))
        self.say(f"  {tokens(totals['input_tokens'])} in · {tokens(totals['output_tokens'])} out"
                 + (" · estimated (the model did not report usage)" if totals.get("estimated") else ""))


def tree_panel(run, store, g, width, always=False):
    """A run's tree with what each agent used: an ASCII graph, or a table if that does not fit. Without `always`, None
    when the root worked alone. None for a run that is not a tree."""
    nodes = treeview.nodes_of(run)
    if not nodes or (len(nodes) < 2 and not always):
        return None
    usage = treeview.agent_usage(store.call_rows(run["id"]))
    art = treeview.graph(nodes, usage, width, run["status"])
    if art is None:
        return treeview.full(nodes, usage, g, run["status"])
    parts = [Text("Agent tree of the latest task", style="leo.title"), art]
    if len(nodes) < 2:
        parts.append(Text(treeview.alone_note(run), style="leo.muted"))
    return Group(*parts)


def models_table(store, uid, g=None):
    g = g or glyphs()
    table = Table(title=Text("Models synced from your L.E.O. server", style="leo.title"), title_justify="left", box=g.box,
                  border_style="leo.border", header_style="leo.muted")
    for column in ("Model", "Strength", "Skills", "Tool calling", "Cost"):
        table.add_column(column)
    for connection in sorted(store.list_connections(uid), key=lambda c: (not c["enabled"], c["name"])):
        tools = {True: Text(f"{g.ok} yes", style="leo.ok"), False: Text(f"{g.fail} no", style="leo.error"),
                 None: Text("? untried", style="leo.muted")}[connection.get("tools")]
        priced = connection.get("input_price") or connection.get("output_price")
        cost = ("free" if connection["free"] or connection["local"] else
                f"${connection.get('input_price') or 0}/{connection.get('output_price') or 0} per 1M" if priced else "price not set")
        name = Text.assemble((f"{g.dot if connection['enabled'] else g.idle} ", "leo.ok" if connection["enabled"] else "leo.muted"),
                             (connection["name"], "" if connection["enabled"] else "leo.muted"))
        if "/api/cli/relay/" in connection["base_url"]:
            name.append(" · via your L.E.O. server", style="leo.muted")
        if not connection["enabled"]:
            name.append(" (off)", style="leo.muted")
        table.add_row(name, Text(connection.get("tier", "")), Text(", ".join(connection.get("skills") or []) or "any", style="leo.muted"),
                      tools, Text(cost, style="leo.ok" if cost == "free" else ""))
    return table
