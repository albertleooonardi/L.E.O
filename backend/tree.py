"""A tree of agents: the strongest model is the root, and each level down is weaker.

The root reads the project, then answers, does an easy task itself, or splits it. Only the bottom level of a branch
edits files and runs commands; parents read, split, and review. Each level uses one strength of model (strong, standard,
light), so a tree has at most 3 levels, and the root decides for each subtask whether it splits again.

A workspace asks for this with `tree = True` (L.E.O CLI does); the server's agent stays flat.
"""
import contextlib
import hashlib

from .agent import STRING, SUB_MAX_STEPS, WORKSPACE, AgentRun, schema, SCHEMAS
from .router import TIERS

MAX_AGENTS = 12  # Children in one run, not counting the root or retries.
PRIVATE = {"files", "commands", "collisions", "reviews", "nudges"}  # A node's working notes, not for the display.


class TreeRun(AgentRun):
    PARALLEL = {"delegate", "review_work"}

    def __init__(self, orchestrator):
        super().__init__(orchestrator)
        self.tree = self.state.setdefault("tree", {"levels": None, "solo": False, "nodes": {}})

    # -- the tree ---------------------------------------------------------

    def levels(self):
        """The strengths among usable models, strongest first; fixed for the run, so a resume sees the same tree."""
        if not self.tree["levels"]:
            present = {p.get("tier", "standard") for p in self.o.candidates("agent", tools=True)[0]}
            self.tree["levels"] = sorted(present, key=lambda tier: -TIERS.get(tier, 1)) or ["strong"]
        return self.tree["levels"]

    def add_node(self, node_id, parent, kind, level, task, skill=None):
        self.tree["nodes"][node_id] = {"id": node_id, "parent": parent, "kind": kind, "level": level, "tier": self.levels()[level], "task": task,
                                       "skill": skill, "model": None, "status": "waiting", "attempt": 1, "files": [], "commands": [],
                                       "collisions": [], "reviews": [], "nudges": 0}
        self.publish(node_id)

    def publish(self, node_id):
        node = self.tree["nodes"][node_id]
        self.store.event(self.o.id, "tree", {key: value for key, value in node.items() if key not in PRIVATE})

    def update(self, node_id, **changes):
        node = self.tree["nodes"][node_id]
        if any(node.get(key) != value for key, value in changes.items()):
            node.update(changes)
            self.o.save()
            self.publish(node_id)

    def works(self, node_id):
        """Whether this node has the tools that change things: a worker, or a root with nobody below it."""
        node = self.tree["nodes"][node_id]
        return node["kind"] == "worker" or len(self.levels()) == 1 or self.tree["solo"]

    # -- the loop ---------------------------------------------------------

    async def run(self):
        if "main" not in self.tree["nodes"]:
            self.levels()
            self.add_node("main", None, "root", 0, self.o.run["prompt"])
        self.update("main", status="planning")
        answer = await super().run()
        if answer is not None:
            self.update("main", status="done")
        return answer

    def call_options(self, name):
        return {"tier": self.tree["nodes"][name]["tier"]}

    def current_tools(self, name, tools):
        return self.toolset(name, lead=name == "main")  # do_it_myself changes them.

    def after_step(self, name, result):
        self.update(name, model=result.get("model"))

    def slot(self):
        return contextlib.nullcontext()  # A parent waiting for its children must not hold a worker's place.

    def toolset(self, name, lead):
        node = self.tree["nodes"][name]
        if self.works(name):
            tools = super().toolset(name, lead=False)
            if node["kind"] == "root":
                tools = tools + [SCHEMAS["todo_write"]]
                self.allowed[name].add("todo_write")
                if self.agent_skills:
                    tools.append(SCHEMAS["install_skill"])
                    self.allowed[name].add("install_skill")
            return tools
        names = ["read_file", "list_files", "read_url"] + (["web_search"] if self.prefs.get("search_url") else []) + (["use_skill"] if self.agent_skills else [])
        tools = [{**SCHEMAS[n], "description": SCHEMAS[n]["description"].replace(WORKSPACE, self.root)} for n in names]
        names += ["todo_write", "delegate"]
        tools += [SCHEMAS["todo_write"], self.delegate_schema(node)]
        if node["kind"] == "root":
            names.append("do_it_myself")
            tools.append(schema("do_it_myself", "The task is easy: do it yourself instead of splitting it. You get the tools that edit files and run "
                                                "commands, and you can no longer delegate.", {}))
        read_only = getattr(self.sandbox, "extra_read_tools", None)
        extra = [spec for spec in read_only() if spec["name"] not in SCHEMAS] if read_only else []
        names += [spec["name"] for spec in extra]
        tools += extra
        self.allowed[name] = set(names)
        return tools

    def delegate_schema(self, node):
        skills = [s["id"] for s in self.skills] or ["general"]
        catalog = "; ".join(f"{s['id']}: {s.get('description', '')}" for s in self.skills)
        kinds = ["worker", "manager"] if node["kind"] == "root" and len(self.levels()) == 3 else ["worker"]
        return schema("delegate", f"Give a self-contained subtask to a child agent on a weaker model, and get back its report. Several calls in one turn "
                                  f"run in parallel. Skills: {catalog}",
                      {"agent": {"type": "string", "enum": skills, "description": "The skill the subtask needs"}, "task": STRING,
                       "kind": {"type": "string", "enum": kinds, "description": "worker: does the subtask itself. manager: splits it further and reviews "
                                                                                "its own workers; only for a subtask too large for one worker."}},
                      ["agent", "task"])

    # -- prompts ----------------------------------------------------------

    def split_rules(self):
        return """- Give each child a self-contained subtask: what to do, which files, and how to verify it. Children see nothing else.
- Never give two children the same files; children running at once share this folder.
- You cannot edit files or run commands: read to understand, delegate the changes.
- Several delegate calls in one turn run in parallel, and each costs model calls, so split only as far as the work needs."""

    def lead_system(self):
        catalog = self.agent_skills.catalog() if self.agent_skills else ""
        skills = f"\n\nAgent skills (load one with use_skill before work it covers):\n{catalog}" if catalog else ""
        search = "" if self.prefs.get("search_url") else " Web search is not set up, but read_url can read a page you know."
        head = f"""You are the root of L.E.O.'s tree of agents, on its strongest model, working in {self.place}.

Workspace: {self.root} (relative paths start there). It persists between messages in this conversation.
{self.environment}{search}{skills}
"""
        finish = """- When the task is done, reply without calling a tool: what was done, how it was verified, and what is not done. Never claim something works unless it was run."""
        if len(self.levels()) == 1:
            return head + f"""
How to work:
- Explore the workspace first when it has files. For multi-step work, keep a todo list with todo_write and update it as you go.
- Change files with write_file and edit_file, then verify by running the code or tests with bash.
- Commands stop after {self.prefs['agent_command_timeout']} seconds. Start servers in the background with output redirected and check them with curl.
{finish}"""
        return head + f"""
How to work:
- Read what you need first, then decide. If no change is needed, answer. If the task is easy, call do_it_myself and do it. Otherwise split it with delegate.
{self.split_rules()}
- Choose kind="manager" only for a subtask large enough to split again.
- After do_it_myself: change files with write_file and edit_file, and verify by running with bash. Commands stop after {self.prefs['agent_command_timeout']} seconds.
{finish}"""

    def sub_system(self, skill):
        return (super().sub_system(skill).replace("sub-agent of L.E.O.", "worker in L.E.O.'s tree of agents")
                .replace("the lead agent and other sub-agents may", "other agents may")
                .replace("The lead agent sees only this report.", "Your parent sees only this report and what you really did, and reviews your work."))

    def manager_system(self, skill):
        return f"""You are a manager in L.E.O.'s tree of agents, working in {self.place}. Your parent gave you one subtask; other agents may be changing other files at the same time.
{self.environment}

How to work:
- Read what you need, then split your subtask into pieces for workers with delegate.
{self.split_rules()}
- When finished, reply without calling a tool, with a short report: what was done, how it was verified, and what is not done. Your parent sees only this report and what the agents really did."""

    # -- tools ------------------------------------------------------------

    async def tool_do_it_myself(self, name, call):
        if any(node["parent"] == name for node in self.tree["nodes"].values()):
            return "Error: you have already delegated subtasks; carry on with those."
        self.tree["solo"] = True
        self.o.save()
        return "You now have bash, write_file, and edit_file, and can no longer delegate. Do the task, and verify it by running it."

    def fail(self, node_id, reason):
        self.update(node_id, status="failed")
        return f"Error: this subtask failed: {reason}"

    async def run_child(self, node_id):
        """Run a child until it reports; the report, or why there is none."""
        node = self.tree["nodes"][node_id]
        system = self.sub_system(node["skill"]) if node["kind"] == "worker" else self.manager_system(node["skill"])
        tools = self.toolset(node_id, lead=False)
        limit = min(SUB_MAX_STEPS, self.prefs["agent_max_steps"])
        try:
            if node["kind"] == "worker":
                async with self.gate:  # Workers wait here for a place; parents never hold one.
                    self.update(node_id, status="working")
                    report = await self.loop(node_id, system, node["task"], tools, "subagent", node["skill"], node["tier"], limit)
            else:
                self.update(node_id, status="planning")
                report = await self.loop(node_id, system, node["task"], tools, "subagent", node["skill"], node["tier"], limit)
        except ValueError as exc:  # No model left for it, or its allowance ran out: its siblings carry on.
            return self.fail(node_id, exc)
        self.update(node_id, status="in_review")
        return report

    async def tool_delegate(self, name, call, agent, task, kind="worker"):
        parent = self.tree["nodes"][name]
        skills = [s["id"] for s in self.skills]
        if not str(task).strip():
            return "Error: give the child a task to do."
        if kind not in ("worker", "manager") or (kind == "manager" and (name != "main" or len(self.levels()) != 3)):
            return f"Error: kind must be one of the kinds listed for delegate here, not {kind!r}."
        if skills and agent not in skills:
            return f"Error: there is no skill called {agent!r}. Use one of: {', '.join(skills)}."
        node_id = "sub-" + hashlib.sha1(call["id"].encode()).hexdigest()[:8]
        if node_id not in self.tree["nodes"]:
            if len(self.tree["nodes"]) - 1 >= MAX_AGENTS:
                return (f"Error: this run has reached its limit of {MAX_AGENTS} agents. Give the remaining work to the agents you have, "
                        "or report it as not done.")
            self.add_node(node_id, name, kind, parent["level"] + 1, task, agent)
            self.o.save()
        return await self.run_child(node_id)
