"""The `leo` command.

    leo [FOLDER]                 work with the agent on this folder, or on FOLDER (signs in through the browser the first time)
    leocode login [--server URL]     sign in again, or to another server
    leocode -p "task"                one task, then print the answer (exit code 1 if it failed)
    leocode resume [N]               continue an earlier session on this folder
    leocode sync | models [add] | logout
    leocode update                   upgrade L.E.O.C.O.D.E to the latest version
"""
import argparse
import asyncio
import os
import sys
import webbrowser

from rich.console import Console
from rich.panel import Panel
from prompt_toolkit import PromptSession
from rich.text import Text

from rich.console import Group
from rich.table import Table

from . import NAME, __version__, models, theme
from .account import Account, AccountError, SignedOut
from .mentions import attach
from .ui import (ANSWERS, TERMINAL, App, Renderer, answers, approval_view, banner, choose_theme, clip_left, glyphs, home_path,
                 key_hint, LOGO_WIDTH, models_table, parse_answer, pick, tree_panel)

COMMANDS = ("login", "logout", "sync", "models", "resume", "update")
DEFAULT_SERVER = "http://localhost:8000"
SIGN_IN_FIRST = "Not signed in yet. Run [bold]leocode[/] in a terminal to sign in (or [bold]leocode login --server URL[/])."


def parser():
    p = argparse.ArgumentParser(prog="leocode", usage="leocode [FOLDER] [options]  |  leocode COMMAND",
                                description="L.E.O.C.O.D.E: the multi-agent LLM Efficiency Orchestrator in your terminal.",
                                epilog="Commands: login, logout, sync, models [add], resume [N], update. With no command, leo works on FOLDER, "
                                       "or on the current folder. In a message, @path attaches a file or folder.")
    p.add_argument("command", nargs="*", help=argparse.SUPPRESS)
    p.add_argument("-p", "--print", dest="task", metavar="TASK", help="run one task, print the answer, and exit")
    p.add_argument("--autorun", action="store_true", help="edit files and run commands without asking (risky ones still ask)")
    p.add_argument("--worktree", action="store_true", help="work in a new git worktree and branch instead of the folder itself")
    p.add_argument("--project", default=os.getcwd(), help="the folder to work on (default: the current folder)")
    p.add_argument("--server", help=f"L.E.O. server to sign in to (default {DEFAULT_SERVER})")
    p.add_argument("--name", help="a name for this device on the server (login)")
    p.add_argument("--version", action="version", version=f"leocode {__version__}")
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.command and args.command[0] not in COMMANDS:
        folder = os.path.expanduser(args.command[0])
        if not os.path.isdir(folder) or len(args.command) > 1:
            p.error(f"unknown command or folder {args.command[0]!r}; give a task with -p \"...\", a folder to work on, "
                    "or start leo with no command")
        args.project, args.command = folder, []  # `leo ~/my-project`: work on that folder.
    if os.name == "nt" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Code and box drawing in a legacy console code page.
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    try:
        return asyncio.run(dispatch(args))
    except KeyboardInterrupt:
        return 130


async def dispatch(args, account=None, transport=None):
    account = account or Account()
    theme.use(account.config().get("theme"))
    console = theme.apply(Console())
    command = args.command[0] if args.command else None
    try:
        if command == "login":
            return await login(account, console, args.server, args.name)
        if command == "logout":
            await account.logout()
            console.print(Text(f"{glyphs(console).ok} Signed out. The models and API keys synced to this device were removed.", style="leo.ok"))
            return 0
        if command == "update":
            from .update import update
            return update(console)
        if command == "sync":
            summary = await account.sync()
            console.print(Text(f"{glyphs(console).ok} Synced {summary['models']} models for {summary['username']}.", style="leo.ok"))
            return 0
        if command == "models" and args.command[1:] == ["add"]:
            return 0 if await models.add_model(account, console, transport) else 1
        if command == "models":
            if not account.store().list_connections(account.user_id):
                console.print(SIGN_IN_FIRST)
                return 1
            console.print(models_table(account.store(), account.user_id, glyphs(console)))
            return 0
        art = False
        if not args.task and not signed_in(account) and sys.stdin.isatty():
            art = True  # The first run: sign in right here, like the other terminal agents, then start working.
            if not await first_sign_in(account, console, args.server, args.name):
                return 0  # Quit from the welcome screen.
        if not await ready(account, console, interactive=not args.task and sys.stdin.isatty()):
            return 1
        from .sessions import Sessions  # Imported here so `leo --help` and `leocode login` start fast.
        sessions = Sessions(account, args.project, autorun=True if args.autorun else None, transport=transport)
        if args.task:
            return await one_shot(sessions, args.task, args.worktree, transport)
        app = App(sessions, account, console)
        console.print(welcome(account, sessions, art=not art, width=console.width))
        if command == "resume":
            history = sessions.history()
            if not history:
                console.print("No earlier sessions on this folder; starting a new one.")
            else:
                index = int(args.command[1]) - 1 if len(args.command) > 1 and args.command[1].isdigit() else 0
                await app.reopen(history[min(index, len(history) - 1)]["id"])
        if args.worktree and not sessions.items:
            await app.cmd_new("--worktree")
        await app.run()
        return 0
    except AccountError as exc:  # SignedOut is one too.
        console.print(Text(f"{glyphs(console).fail} {exc}", style="leo.error"))
        return 1


def signed_in(account):
    return bool(account.secrets.get("device-token") or account.store().list_connections(account.user_id))


async def first_sign_in(account, console, server=None, name=None):
    """The welcome screen of a device that is not signed in: a theme the first time, then Sign in or Quit.

    Signing in opens the L.E.O. website, where the user signs in or creates an account and allows this device.
    True once signed in; False if the user quit."""
    if console.width >= LOGO_WIDTH:
        console.print(banner())
    console.print(Text.assemble("\nWelcome to ", (NAME, "leo.title"), ", the multi-agent LLM Efficiency Orchestrator.\n"))
    steps = 1 if account.config().get("theme") else 2
    if steps == 2:
        console.print(step(1, 2, "Choose a theme"))
        if await choose_theme(account, console) is None:
            return False
    console.print(step(steps, steps, "Sign in"))
    server = server or account.server or os.getenv("LEO_SERVER") or DEFAULT_SERVER
    while True:
        choice = await pick("Sign in with your L.E.O. account to use your models here:", [
            ("sign-in", f"Sign in · opens the L.E.O. website ({server}) in your browser"),
            ("server", "Use another L.E.O. server"),
            ("quit", "Quit")])
        if choice in (None, "quit"):
            return False
        if choice == "server":
            try:
                server = (await PromptSession().prompt_async("L.E.O. server address: ", default=server)).strip() or server
            except (EOFError, KeyboardInterrupt):
                pass
            continue
        try:
            await login(account, console, server, name, inside=True)
            return True
        except AccountError as exc:  # Unreachable server, denied, expired: say so and offer the menu again.
            console.print(Text(f"{glyphs(console).fail} {exc}", style="leo.error"))


def step(number, total, title):
    return Text.assemble((f"Step {number} of {total} · ", "leo.muted"), (title, "leo.title"))


async def login(account, console, server=None, name=None, inside=False):
    server = server or account.server or os.getenv("LEO_SERVER") or DEFAULT_SERVER
    g = glyphs(console)

    def show(code, url):
        try:
            opened = webbrowser.open(url)
        except Exception:  # No browser here (SSH): the link below works from any device.
            opened = False
        steps = Table.grid(padding=(0, 2))
        steps.add_column(style="leo.accent")
        steps.add_column()
        steps.add_row("1", "Your browser opens the L.E.O. website" if opened else "Open the link below on any device")
        steps.add_row("2", "Sign in, or create an account")
        steps.add_row("3", "Check the page shows this code, then click Allow")
        code_line = Text.assemble(("code  ", "leo.muted"), (f" {code} ", "leo.key"))
        console.print(Panel(Group(steps, Text(""), code_line), box=g.box, title=f"Sign in to {server}", title_align="left",
                            border_style="leo.brand", padding=(1, 2)))
        # The link goes outside the box, on one line, so it copies cleanly.
        console.print(Text("If the browser did not open, go to:" if opened else "Open this link on any device:", style="leo.muted"))
        console.print(Text(url, style="underline"), soft_wrap=True)
        console.print(Text(f"{g.spinner[0]} Waiting for you to allow this device… (Ctrl-C to cancel)", style="leo.muted"))

    summary = await account.login(server, name, show)
    relayed = f" ({summary['relayed']} through your L.E.O. server)" if summary.get("relayed") else ""
    console.print(Text.assemble((f"{g.ok} Signed in as {summary['username']}", "leo.ok"),
                                f": {summary['models']} models synced{relayed}. Keys are kept in {account.secrets.where}.",
                                "" if inside else " Run leocode in a project folder to start."))
    return 0


async def ready(account, console, interactive=False, transport=None):
    """Fresh models before working: re-sync when the last sync is old, but keep working offline with what is here.

    With no usable model, an interactive leo offers to set one up right here instead of stopping."""
    signed_out = False
    if account.secrets.get("device-token") and account.stale():
        try:
            await account.sync()
        except SignedOut as exc:
            signed_out = True
            console.print(Text(f"{glyphs(console).warn} {exc}. Using the models already on this device.", style="leo.warn"))
        except AccountError:
            console.print(Text(f"{glyphs(console).warn} Your L.E.O. server is not reachable; using the models synced earlier.",
                               style="leo.warn"))
    if models.usable(account):
        return True
    if interactive:
        return await models.setup(account, console, transport, signed_out=signed_out or not signed_in(account),
                                  login=lambda account, console: login(account, console, inside=True))
    if not signed_in(account):
        console.print(SIGN_IN_FIRST)
        return False
    console.print("No model on this device can run the agent (it needs tool calling). Run [bold]leocode[/] to add one, "
                  "or add one in L.E.O. Settings → Models and run [bold]leocode sync[/].")
    return False


def welcome(account, sessions, art=True, width=100):
    """The session's first screen: who, where, and how it will behave, in a card; the logo above it when it fits."""
    config, g = account.config(), glyphs()
    count = sum(1 for c in account.store().list_connections(account.user_id) if c["enabled"])
    info = Table.grid(padding=(0, 2))
    info.add_column(style="leo.muted")
    info.add_column()
    info.add_row("account", Text(f"{config['username']} · {config['server']}") if config.get("username") else Text("offline", style="leo.warn"))
    # Room left beside the labels; a long path keeps its end, the folder's own name.
    info.add_row("project", Text(clip_left(home_path(sessions.project), max(width - 19, 12)), style="leo.accent"))
    if sessions.items and sessions.current and sessions.current.branch:
        info.add_row("branch", Text(sessions.current.branch))
    info.add_row("models", Text(f"{count} ready", style="leo.ok" if count else "leo.warn"))
    info.add_row("mode", Text("autorun: edits and commands run without asking", style="leo.warn") if sessions.policy.autorun
                 else Text("asks before edits and commands"))
    head = Text.assemble((NAME, "leo.title"), (f"  v{__version__}", "leo.muted"), "\n",
                         ("multi-agent LLM Efficiency Orchestrator", "leo.muted"))
    card = Group(head, Text(""), info)
    logo = [banner(), Text("")] if art and width >= LOGO_WIDTH else []
    tips = [("enter", "send a task"), ("/", "commands"), ("esc", "stop"), ("ctrl-c x2", "quit")]
    tips = key_hint(tips if width >= 72 else tips[:3])  # One line, whatever the width.
    return Group(Text(""), *logo, Panel(card, box=g.box, border_style="leo.border", padding=(1, 2), expand=False), tips, Text(""))


async def one_shot(sessions, task, worktree=False, transport=None, stdin=None):
    """Run one task. Progress goes to stderr and the answer to stdout, so `leocode -p` works in pipes and scripts.

    Approvals are asked on the terminal when there is one; without a terminal, anything that needs approval is
    declined (use --autorun for unattended runs)."""
    stdin = stdin or sys.stdin
    err = Console(stderr=True)
    renderer = Renderer(err, sessions.project)
    sessions.notify = lambda session, message: err.print(Text(f"{renderer.g.warn} {message}", style="leo.warn"))

    def show(event):
        if event["kind"] == "done":  # The answer goes to stdout; stderr gets the closing line with what the run cost.
            tree = tree_panel(session.run, sessions.store, renderer.g)
            if tree:
                err.print(tree)
            err.print(renderer.footer(event["data"]))
        else:
            renderer.event(event["kind"], event["data"])
    session = await sessions.open(worktree=worktree)
    expanded = sessions.skills.expand(task[1:].partition(" ")[0], task[1:].partition(" ")[2].strip()) if task.startswith("/") else None
    task, notes = attach(expanded or task, session.root)
    for note in notes:
        err.print(renderer.line(renderer.g.info, f"attached {note}"))
    await sessions.send(session, task)
    cursor = 0
    try:
        while True:
            await asyncio.sleep(0.15)
            for event in sessions.events(session, cursor):
                cursor = event["id"]
                show(event)
            if session.task and not session.task.done():
                continue
            run = session.run
            if run["status"] in TERMINAL:
                break
            approval = session.approval
            if not approval:
                break  # Neither working, waiting, nor finished: another leocode window holds it (the session said so).
            err.print(approval_view(approval, unicode=renderer.g.dot != "*"))
            sessions.answer(session, await ask_line(approval, stdin, err))
    finally:
        await sessions.close()
    for event in sessions.events(session, cursor):
        show(event)
    if run["status"] == "completed":
        print(run["output"])
        return 0
    err.print(Text(f"{renderer.g.fail} {run.get('error') or run['status']}", style="leo.error"))
    return 1


async def ask_line(approval, stdin, err):
    """The approval answer from a plain terminal line (the full-screen prompt is not used by `leocode -p`)."""
    g = glyphs(err)
    if not stdin.isatty():
        err.print(Text(f"{g.warn} Declined: no terminal to ask on. Use --autorun for unattended runs.", style="leo.warn"))
        return "deny"
    options = answers(approval)
    while True:
        err.print(key_hint([(f"{number}", ANSWERS[decision]) for number, decision in enumerate(options.values(), 1)])
                  + Text(f"{g.prompt} ", style="leo.accent"), end="")
        answer = parse_answer(await asyncio.to_thread(stdin.readline), options)
        if answer:
            return options[answer]
        err.print(Text(f"{g.warn} Answer with a number, 1 to {len(options)}.", style="leo.warn"))


if __name__ == "__main__":
    sys.exit(main())
