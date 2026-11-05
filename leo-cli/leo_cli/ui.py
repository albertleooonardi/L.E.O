"""The terminal: what the agent is doing, approval questions, and the prompt.

Events come from the local store, the same event log the web app streams, so this module only reads and renders them.
The prompt stays usable while sessions work: typing steers the current one, Esc stops it, a status bar lists every
session, and an approval question takes over the prompt as soon as the current session asks one.
"""
import asyncio
import difflib
import html
import os
import shutil
import sys
import time

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts.choice_input import ChoiceInput
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from . import theme
from .account import AccountError

TERMINAL = {"completed", "failed", "cancelled"}
INTERRUPTED = "\0interrupted"  # The prompt was closed to show an approval question.
CHOICES = {"y": "once", "s": "session", "p": "project", "n": "deny"}
HELP = [("/new [--worktree]", "start another session (in its own git worktree with --worktree)"),
        ("/sessions", "list sessions in this window"), ("/switch N", "show session N"),
        ("/resume [N]", "list earlier sessions on this folder, or reopen one"), ("/clear", "start over in this session"),
        ("/autorun on|off [--save]", "run edits and commands without asking (risky ones still ask)"),
        ("/models [add]", "list your models, or add one with an API key"), ("/sync", "fetch model changes made on the website"),
        ("/cost", "tokens and cost of this session"),
        ("/login [server]", "sign in in the browser (another account or server)"), ("/logout", "sign out and remove the synced models"),
        ("/theme [name]", "change the colours (dark, light, contrast, terminal, mono)"),
        ("/help", "this list"), ("/exit", "quit (Ctrl-C twice also quits)")]
BANNER = ["██╗        ███████╗    ██████╗ ",
          "██║        ██╔════╝   ██╔═══██╗",
          "██║        █████╗     ██║   ██║",
          "██║        ██╔══╝     ██║   ██║",
          "███████╗██╗███████╗██╗╚██████╔╝",
          "╚══════╝╚═╝╚══════╝╚═╝ ╚═════╝ "]


def banner():
    """L.E.O in block letters, shaded top to bottom in the theme's colours."""
    return Text("\n").join(Text(line, style=f"leo.banner{i}") for i, line in enumerate(BANNER))


def clip(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def tokens(n):
    return f"{n / 1000:.1f}K" if n >= 1000 else str(n)


def usage_line(usage, seconds=None):
    total = (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
    parts = [f"{usage.get('calls', 0)} calls", f"{'~' if usage.get('estimated') else ''}{tokens(total)} tokens",
             f"${usage.get('estimated_cost') or 0:.4f}"]
    if seconds is not None:
        parts.append(f"{int(seconds // 60)}m {int(seconds % 60):02d}s" if seconds >= 60 else f"{seconds:.0f}s")
    return " · ".join(parts)


def suggested_prefix(command):
    """What to remember for "allow commands like this": the program and its subcommand, e.g. `npm test` or `pytest`."""
    words = command.strip().split()
    if not words:
        return command
    keep = words[:2] if len(words) > 1 and not words[1].startswith(("-", "/", ".", "'", '"')) and "=" not in words[1] else words[:1]
    return " ".join(keep)


class Renderer:
    """Prints a session's events as they arrive."""

    def __init__(self, console, root=None):
        self.console, self.root = theme.apply(console), root
        utf = "utf" in (console.encoding or "").lower()
        self.dot, self.hook, self.sub = ("⏺", "⎿", "↳") if utf else ("*", "|", ">")
        self.model = None

    def event(self, kind, data):
        show = getattr(self, f"on_{kind}", None)
        if show:
            show(self.short(data))

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

    def on_model(self, data):
        if str(data.get("stage", "")).startswith("agent:main") and data.get("model") != self.model:
            self.model = data.get("model")
            self.console.print(Text(f"  model: {self.model}", style="leo.muted"))

    def on_agent_step(self, data):
        lead = data.get("agent") == "main"
        if lead and data.get("text") and data.get("tools"):
            self.console.print(Markdown(data["text"], code_theme=theme.code()))  # Thinking out loud between tool calls; the final answer comes with done.
        for call in data.get("tools") or []:
            line = Text()
            if not lead:
                line.append(f"  {self.sub} {data['agent'][4:]} ", style="leo.accent")
            line.append(f"{self.dot} ", style="leo.lead" if lead else "leo.accent")
            line.append(call["name"], style="bold")
            if call.get("summary"):
                line.append(f"({clip(call['summary'], 100)})")
            self.console.print(line)

    def on_agent_tool(self, data):
        output = str(data.get("output") or "")
        lines = output.splitlines()
        indent = "    " if data.get("agent") != "main" else "  "
        if not data.get("ok"):
            self.console.print(Text(f"{indent}{self.hook} {clip(output, 160)}", style="leo.error"))
            return
        if data.get("name") == "bash" and lines:
            body = [l for l in lines[1:] if l.strip() and l.strip() != "(no output)"]
            failed = not lines[0].startswith("exit code 0")
            note = lines[0] + (f" · {len(body)} line{'s' if len(body) != 1 else ''} of output" if body else "")
            self.console.print(Text(f"{indent}{self.hook} {note}", style="leo.warn" if failed else "leo.muted"))
            for line in body[-5:] if failed else []:
                self.console.print(Text(f"{indent}  {clip(line, 160)}", style="leo.muted"))
        elif data.get("name") == "read_file":
            self.console.print(Text(f"{indent}{self.hook} {len(lines)} lines", style="leo.muted"))
        else:
            self.console.print(Text(f"{indent}{self.hook} {clip(lines[0] if lines else 'done', 140)}", style="leo.muted"))

    def on_todos(self, data):
        marks = {"completed": ("[x]", "leo.muted"), "in_progress": ("[>]", "bold"), "pending": ("[ ]", "")}
        for item in data.get("items") or []:
            mark, style = marks.get(item["status"], marks["pending"])
            self.console.print(Text(f"  {mark} {item['content']}", style=style))

    def on_subagent(self, data):
        who = f"{data.get('skill') or 'general'} sub-agent {data['agent'][4:]}"
        if data.get("status") == "started":
            self.console.print(Text(f"  {self.sub} {who} started: {clip(data.get('task', ''), 120)}", style="leo.accent"))
        else:
            self.console.print(Text(f"  {self.sub} {who} finished: {clip(data.get('report', ''), 140)}", style="leo.accent"))

    def on_compacted(self, data):
        self.console.print(Text("  (older steps were summarized to save context)", style="leo.muted"))

    def on_warning(self, data):
        self.console.print(Text(f"  ! {data.get('message', '')}", style="leo.warn"))

    def on_steer(self, data):
        self.console.print(Text(f"  you → agent: {clip(data.get('message', ''), 160)}", style="leo.muted"))

    def on_approval_resolved(self, data):
        self.console.print(Text(f"  {'approved' if data.get('approved') else 'declined'}", style="leo.muted"))

    def on_done(self, data):
        status = data.get("status")
        if status == "completed" and data.get("output"):
            self.console.print(Markdown(data["output"], code_theme=theme.code()))
        elif status == "failed":
            self.console.print(Text(f"✗ {data.get('error') or 'The run failed'}", style="leo.error"))
        elif status == "cancelled":
            self.console.print(Text("Stopped.", style="leo.warn"))
        if data.get("usage", {}).get("calls"):
            self.console.print(Text(usage_line(data["usage"]), style="leo.muted"))


def approval_view(approval):
    """The question, with what exactly would happen: the command, the change, or the push."""
    detail = approval.get("detail") or {}
    kind = approval.get("kind")
    parts = [Text(approval.get("message", ""), style="bold")]
    if kind == "command":
        parts.append(Syntax(detail.get("command", ""), "bash", theme=theme.code(), word_wrap=True))
        if detail.get("risky"):
            parts.append(Text("This matches L.E.O.'s list of risky commands, so it is always asked and never remembered.", style="leo.warn"))
    elif kind == "edit" and detail.get("old") is not None:
        diff = difflib.unified_diff(detail["old"].splitlines(), detail.get("new", "").splitlines(), detail.get("path", ""), detail.get("path", ""), lineterm="")
        parts.append(Syntax("\n".join(diff), "diff", theme=theme.code(), word_wrap=True))
    elif kind == "edit":
        preview = detail.get("new", "").splitlines()
        more = (detail.get("lines") or len(preview)) - 20
        parts.append(Syntax("\n".join(preview[:20]) + (f"\n… {more} more lines" if more > 0 else ""), "text", theme=theme.code(), word_wrap=True))
    elif detail:
        parts.append(Text("\n".join(f"{k}: {v}" for k, v in detail.items() if v not in (None, "")), style="leo.muted"))
    return Panel(Group(*parts), border_style="leo.warn", title="Approval needed", title_align="left")


def remembers(approval):
    """Whether "this session" and "always in this project" are offered (never for risky commands, pushes, or installs)."""
    return approval.get("kind") in ("command", "edit") and not (approval.get("detail") or {}).get("risky")


def choices_text(approval):
    if remembers(approval):
        return "[y] yes  [s] yes, for this session  [p] yes, always in this project  [n] no"
    return "[y] yes  [n] no"


async def pick(message, options, default=None):
    """An arrow-key menu (Enter chooses, or type the option's number); None if cancelled with Ctrl-C."""
    try:
        return await ChoiceInput(message=message, options=options, default=default, style=theme.prompt_style()).prompt_async()
    except (EOFError, KeyboardInterrupt):
        return None


def theme_preview():
    """A few lines as the agent would print them, in the active theme."""
    dot, hook, sub = ("⏺", "⎿", "↳")
    diff = "--- greet.py\n+++ greet.py\n@@ -1 +1 @@\n-print('hello')\n+print('hello from leo')"
    label = theme.THEMES[theme.current()]["label"]
    return Panel(Group(
        Text.assemble((f"{dot} ", "leo.lead"), ("edit_file", "bold"), "(greet.py)"),
        Syntax(diff, "diff", theme=theme.code(), word_wrap=True),
        Text.assemble((f"{dot} ", "leo.lead"), ("bash", "bold"), "(python greet.py)"),
        Text(f"  {hook} exit code 0 · 1 line of output", style="leo.muted"),
        Text(f"  {sub} coding sub-agent 1 finished: tests pass", style="leo.accent"),
        Text("  ! Rate limited; switching to the next model", style="leo.warn"),
        Text("✗ Example error", style="leo.error")), title=f"Theme: {label}", title_align="left", border_style="leo.brand")


async def choose_theme(account, console, current=None):
    """Ask for a theme, save it, and show what it looks like; None if cancelled."""
    options = [(key, f"{spec['label']} · {spec['description']}") for key, spec in theme.THEMES.items()]
    picked = await pick("Choose the look that suits your terminal (you can change it later with /theme):", options,
                        default=current or theme.current())
    if picked is None:
        return None
    console.push_theme(theme.use(picked))
    account.save(theme=picked)
    console.print(theme_preview())
    return picked


class App:
    """The interactive session: prompt, background event printing, approvals, and slash commands."""

    def __init__(self, sessions, account, console=None):
        self.sessions, self.account = sessions, account
        self.console = theme.apply(console or Console())
        self.renderer = Renderer(self.console, sessions.project)
        self.cursors, self.notified, self.saved_text, self.last_interrupt = {}, set(), "", 0.0
        self.history = []  # Earlier sessions listed by /resume, so /resume N refers to that list.
        self.prompt = None

    # -- printing -----------------------------------------------------------------

    def say(self, text, style="leo.muted"):
        self.console.print(Text(text, style=style))

    def notify(self, session, message):
        prefix = f"[{session.number}] " if session is not None and len(self.sessions.items) > 1 else ""
        self.say(prefix + message, "leo.warn")

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
                    self.prompt.app.exit(result=INTERRUPTED)
            except Exception as exc:  # Printing must never take the session down.
                self.say(f"(display error: {exc})", "leo.error")
            await asyncio.sleep(0.15)

    # -- prompt -------------------------------------------------------------------

    def message(self):
        current = self.sessions.current
        return HTML("<ansiyellow>steer › </ansiyellow>") if current and current.busy else HTML("<b>› </b>")

    def toolbar(self):
        parts = []
        for session in self.sessions.items:
            status = "needs approval" if session.approval else session.status
            text = f"[{session.number}] {html.escape(session.title or 'new')[:24]} · {status}"
            if session.branch:
                text += f" · {html.escape(session.branch)}"
            if session is self.sessions.current:
                text = f"<b>{text}</b>"
            elif session.approval:
                text = f"<style bg='ansiyellow' fg='ansiblack'>{text}</style>"
            parts.append(text)
        autorun = "autorun on" if self.sessions.policy.autorun else "asks before edits and commands"
        keys = "Esc stop · type to steer" if self.sessions.current and self.sessions.current.busy else "/help"
        return HTML("  ".join(parts + [f"<i>{autorun}</i>", keys]))

    def keys(self):
        bindings = KeyBindings()

        @bindings.add("escape", eager=True)
        def _stop(event):
            current = self.sessions.current
            if current and (current.busy or current.approval):
                self.sessions.stop(current)
                event.app.exit(result=INTERRUPTED)
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
                self.say("Press Ctrl-C again to quit (sessions keep their progress: leo resume).", "leo.warn")

        return bindings

    async def run(self):
        color, width = self.console.color_system, shutil.get_terminal_size().columns
        with patch_stdout(raw=True):
            # Print through prompt_toolkit's stdout, so output appears above the prompt instead of through it.
            self.console = theme.apply(Console(file=sys.stdout, force_terminal=True, color_system=color or "standard", width=width))
            self.renderer = Renderer(self.console, self.sessions.project)
            self.sessions.notify = self.notify
            self.prompt = PromptSession(history=FileHistory(str(self.account.home / "history")), key_bindings=self.keys(),
                                        bottom_toolbar=self.toolbar, refresh_interval=0.5)
            watcher = asyncio.create_task(self.watch())
            try:
                await self.loop()
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
                await self.sessions.close()
        if any(s.status in ("running", "queued", "waiting") for s in self.sessions.items):
            self.console.print(Text("Unfinished sessions keep their progress; continue with: leo resume", style="leo.muted"))

    async def loop(self):
        while True:
            current = self.sessions.current
            approval = current.approval if current else None
            if approval and approval["id"] not in self.notified:
                await self.ask(current, approval)
                continue
            text, self.saved_text = self.saved_text, ""
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
            if text.startswith("/"):
                if await self.command(text) == "exit":
                    return
                continue
            await self.submit(text)

    async def ask(self, session, approval):
        """Show an approval question and send the answer; Ctrl-C or Esc counts as no."""
        self.notified.add(approval["id"])
        self.console.print(approval_view(approval))
        options = CHOICES if remembers(approval) else {"y": "once", "n": "deny"}
        asker = PromptSession()
        while True:
            try:
                answer = (await asker.prompt_async(f"{choices_text(approval)} › ")).strip().lower()[:1]
            except (EOFError, KeyboardInterrupt):
                answer = "n"
            if answer in options:
                break
            self.say("Type one of the letters in brackets.", "leo.warn")
        decision, prefix = options[answer], None
        if decision in ("session", "project") and approval["kind"] == "command":
            command = approval["detail"]["command"]
            try:
                prefix = (await asker.prompt_async("Allow commands starting with: ", default=suggested_prefix(command))).strip() or command
            except (EOFError, KeyboardInterrupt):
                prefix = command
        if session.approval and session.approval["id"] == approval["id"]:
            self.sessions.answer(session, decision, prefix)

    async def submit(self, text):
        session = self.sessions.current or await self.sessions.open()
        if session.busy:
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
        table = Table(show_header=False, box=None, padding=(0, 2))
        for command, description in HELP:
            table.add_row(f"[bold]{command}[/]", description)
        skills, commands = self.sessions.skills.discover(), self.sessions.skills.commands()
        for name in sorted(set(skills) | set(commands)):
            table.add_row(f"[bold]/{name}[/]", (skills.get(name) or {}).get("description") or f"command file {commands[name].name}")
        self.console.print(table)
        self.say("While the agent works: type to steer it, Esc to stop it, Ctrl-C twice to quit.")

    async def cmd_exit(self, arguments):
        return "exit"

    cmd_quit = cmd_exit

    async def cmd_new(self, arguments):
        session = await self.sessions.open(worktree="--worktree" in arguments)
        where = f" on branch {session.branch} in {session.root}" if session.branch else ""
        self.say(f"Session {session.number}{where}. Earlier sessions keep running; /sessions lists them.", "leo.ok")

    async def cmd_sessions(self, arguments):
        if not self.sessions.items:
            self.say("No sessions yet: type a task to start one.")
        for session in self.sessions.items:
            marker = "›" if session is self.sessions.current else " "
            self.console.print(Text(f"{marker} {session.label()}" + ("  · needs approval" if session.approval else "")))

    async def cmd_switch(self, arguments):
        session = next((s for s in self.sessions.items if str(s.number) == arguments), None)
        if not session:
            self.say("Usage: /switch N (see /sessions)", "leo.error")
            return
        self.sessions.current = session
        self.say(session.label(), "leo.ok")
        run = session.run
        if run and run["status"] in TERMINAL and run.get("output"):
            self.console.print(Markdown(run["output"]))  # Its events went by while another session was shown.

    async def cmd_resume(self, arguments):
        if not arguments:
            self.history = self.sessions.history()
            if not self.history:
                self.say("No earlier sessions on this folder.")
            for number, item in enumerate(self.history[:20], 1):
                when = time.strftime("%d %b %H:%M", time.localtime(item["updated_at"]))
                self.console.print(Text(f"  {number}. {item['title']}  ({when})"))
            if self.history:
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
        conversation = self.sessions.store.conversation(conversation_id, self.sessions.uid)
        last = next((m for m in reversed(conversation["messages"]) if m["role"] == "assistant"), None) if conversation else None
        self.say(f"Resumed: {session.title}", "leo.ok")
        if last:
            self.console.print(Markdown(last["content"]))
        if session.busy:
            self.say("Its last task was interrupted and continues now.", "leo.warn")

    async def cmd_clear(self, arguments):
        current = self.sessions.current
        if current and (current.busy or current.approval):
            self.say("This session is working; stop it with Esc first, or open another with /new.", "leo.error")
            return
        if current:
            current.id = current.run_id = current.title = None
            current.written = set()
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
            await add_model(self.account, self.console, self.sessions.transport)
            return
        if arguments:
            self.say("Use /models to list your models, /models add to add one, or /sync to fetch changes from the website.", "leo.warn")
            return
        self.console.print(models_table(self.sessions.store, self.sessions.uid))

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
            self.console.print(theme_preview())
        else:
            await choose_theme(self.account, self.console)

    async def cmd_logout(self, arguments):
        await self.account.logout()
        self.say("Signed out; the models and API keys synced to this device were removed. /login signs in again.", "leo.ok")

    async def cmd_cost(self, arguments):
        current = self.sessions.current
        totals = self.sessions.cost(current) if current else None
        if not totals or not totals.get("calls"):
            self.say("No model calls in this session yet.")
            return
        self.say(usage_line({"calls": totals["calls"], "input_tokens": totals["input_tokens"], "output_tokens": totals["output_tokens"],
                             "estimated_cost": totals["cost"], "estimated": totals["estimated"]}), "default")


def models_table(store, uid):
    table = Table(title="Models synced from your L.E.O. server", title_justify="left")
    for column in ("Model", "Strength", "Skills", "Tool calling", "Cost"):
        table.add_column(column)
    for connection in sorted(store.list_connections(uid), key=lambda c: (not c["enabled"], c["name"])):
        tools = {True: "yes", False: "no", None: "untried"}[connection.get("tools")]
        cost = "free" if connection["free"] or connection["local"] else f"${connection.get('input_price') or 0}/{connection.get('output_price') or 0} per 1M"
        via = " · via your L.E.O. server" if "/api/cli/relay/" in connection["base_url"] else ""
        table.add_row(connection["name"] + via + ("" if connection["enabled"] else " (off)"), connection.get("tier", ""),
                      ", ".join(connection.get("skills") or []) or "any", tools, cost)
    return table
