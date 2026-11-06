# L.E.O.C.O.D.E Tree Agents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the CLI's flat lead agent and sub-agents with a tree of weaker and weaker models, where only the bottom level works and parents review, show that tree live and in `/usage`, and rename the product to L.E.O.C.O.D.E.

**Architecture:** A new `backend/tree.py` holds `TreeRun`, a subclass of `AgentRun`, selected when the workspace sets `tree = True`. `AgentRun` gains five small no-op hooks, and `Orchestrator.call` a `tier` argument, so the server's flat agent does not change. The CLI draws from a new `tree` event through a new `leo_cli/treeview.py`.

**Tech Stack:** Python 3.11+, asyncio, rich, prompt_toolkit, pytest with fake models (`httpx.MockTransport`).

**Spec:** `docs/superpowers/specs/2026-11-06-leo-code-tree-agents-design.md`

## Global Constraints

- At most 3 levels, one per strength (`TIERS` in `backend/router.py`): strong, standard, light. A child is one level below its parent.
- Only the bottom level of a branch edits files and runs commands. The tool list enforces it.
- Rejections: attempt 1 retries the same model with feedback; attempt 2 moves to the next stronger level's model, same conversation; a rejected attempt 3 is `failed`.
- `MAX_AGENTS = 12`: the 13th `delegate` of a run is refused. Retries are not new agents; the root is not counted.
- Evidence is cut to 4,000 characters, and a cut says so. A parent gets 2 reminders before unreviewed children become `not_reviewed`.
- `agent_parallel` limits workers only. Parents waiting for children hold no slot.
- Node statuses: `waiting`, `planning`, `working`, `in_review`, `retrying`, `accepted`, `failed`, `not_reviewed`, and `done` (the root only; this one is an addition to the spec's list, for showing a finished root).
- The live tree takes at most a third of the terminal's height and never more than 10 lines.
- The server's agent, Chat, and the benchmarks do not change behaviour: all of `tests/` passes throughout, with only rename edits to its assertions in Task 5.
- Not renamed: `~/.config/leo-cli`, `.leo-cli`, `LEO_CLI_HOME`, Python packages `leo_cli` and `leo-cli`, npm package `leo-orchestrator`.
- Run tests from the repo root: `uv run --frozen python -m pytest leo-cli/tests -q` and `uv run --frozen python -m pytest tests -q`.

## Review Focus

- A strength whose only model does not really work (your `Openrouter` entry is `standard`, tools untried): its node must fall through to another model, not fail the run. Pinned in Task 1 and Task 2.
- Stopping mid-tree with Esc: every running sibling stops, no node stays "working" forever, and the session can take a new task. Pinned in Task 3.
- Every model unavailable for one child (quota out): that child is `failed` and its parent is told; the rest of the tree continues. Pinned in Task 2.
- A model sending bad arguments: an unknown `kind` or `agent`, an empty `task`, or `review_work` on a child that is not its own. It gets an error it can read, and the run continues. Pinned in Task 2 and Task 3.
- A short or narrow terminal (10 rows, 50 columns) with a large tree: the prompt stays usable. Pinned in Task 4.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/orchestrator.py` | `tier` in `call` and `candidates`; chooses `TreeRun` or `AgentRun`. |
| `backend/agent.py` | Five hooks. No behaviour change. |
| `backend/tree.py` (new) | `TreeRun`: levels, nodes, tool sets, prompts, delegate, review, evidence, limits. |
| `leo-cli/leo_cli/workspace.py` | `tree = True`, `extra_read_tools()`. |
| `leo-cli/leo_cli/treeview.py` (new) | Pure drawing: nodes to live fragments and a full view; ledger to per-agent usage. |
| `leo-cli/leo_cli/ui.py`, `app.py`, `panels.py` | The `tree` event, the prompt, `/usage`, `-p`. |
| `leo-cli/tests/test_tree.py` (new) | The engine through real sessions and fake models. |
| `leo-cli/tests/test_treeview.py` (new) | Drawing. |

---

### Task 1: Engine hooks and routing by strength

**Files:**
- Modify: `backend/orchestrator.py` (`candidates`, `call`, `execute`)
- Modify: `backend/agent.py` (`loop`, `run_tools`)
- Test: `tests/test_routing.py`, `tests/test_agent.py`

**Interfaces:**
- Produces: `Orchestrator.candidates(role, skill=None, difficulty=None, tools=False, tier=None)` and `Orchestrator.call(..., stream=False, tier=None)`.
- Produces on `AgentRun`, each a no-op by default:
  - `call_options(self, name) -> dict`: extra keyword arguments for `self.o.call` (default `{}`).
  - `current_tools(self, name, tools) -> list`: the tools for the next step (default `tools`).
  - `finish_blocked(self, name, t) -> bool`: True if it added a message and the agent must go on (default `False`).
  - `after_step(self, name, result) -> None`: called right after `agent_step` is stored.
  - `slot(self) -> async context manager`: wraps parallel `delegate` calls (default `self.gate`).
- Produces: class attribute `AgentRun.PARALLEL = {"delegate"}`: the tool names `run_tools` runs in parallel.

- [ ] **Step 1: Write the failing test** in `tests/test_routing.py`, named `test_a_tier_orders_candidates_by_strength_then_stronger_then_weaker`

With fake connections `strong-a`, `standard-untried` (`tools=None`), `standard-a` (`tools=True`), `light-a`:

```python
assert names(tier='standard') == ['standard-a', 'standard-untried', 'strong-a', 'light-a']
assert names(tier='light') == ['light-a', 'standard-a', 'standard-untried', 'strong-a']
assert names(tier='strong') == ['strong-a', 'standard-a', 'standard-untried', 'light-a']
```

- [ ] **Step 2: Run it**

Run: `uv run --frozen python -m pytest tests/test_routing.py -q -k tier_orders`
Expected: FAIL, unexpected keyword `tier`.

- [ ] **Step 3: Implement `tier` in `candidates` and pass it through `call`**

With a tier, replace the final `tools` sort by one stable sort on `(rank, p.get("tools") is not True)`, where `rank` is `0` for that strength, `1 + distance` for stronger ones, and `10 + distance` for weaker ones. Without a tier, ordering stays exactly as it is.

- [ ] **Step 4: Add the hooks to `AgentRun`, and select the class in `execute`**

In `loop`: call `finish_blocked` where a finished assistant message would return (`continue` if True), take `tools = self.current_tools(name, tools)` before `compact`, spread `**self.call_options(name)` into `self.o.call`, and call `after_step` after the `agent_step` event. In `run_tools`, use `self.PARALLEL` and `self.slot()` in place of the literal `"delegate"` and `self.gate`.

In `Orchestrator.execute`: `from .tree import TreeRun` beside the existing import, and `run_class = TreeRun if getattr(self.sandbox, "tree", False) else AgentRun`.

- [ ] **Step 5: Run everything**

Import `TreeRun` inside `execute`, as `AgentRun` is, so `backend.tree` is only needed when a workspace asks for a tree.

Run: `uv run --frozen python -m pytest tests leo-cli/tests -q`
Expected: all pass.

- [ ] **Step 6: Commit** `Engine: route by strength, and hooks for a tree of agents`

---

### Task 2: The tree runs: levels, roles, delegation

**Files:**
- Create: `backend/tree.py`
- Modify: `leo-cli/leo_cli/workspace.py`
- Test: create `leo-cli/tests/test_tree.py`

**Interfaces:**
- Consumes: the Task 1 hooks and `tier`.
- Produces in `backend/tree.py`: `MAX_AGENTS = 12`, `class TreeRun(AgentRun)`.
- Node state lives in `self.state["tree"]`: `{"levels": ["strong", ...], "solo": bool, "nodes": {id: node}}`. The root's id is `"main"`. A child's id is `"sub-" + sha1(call id)[:8]`, as today.
- A node is `{id, parent, kind, level, tier, task, skill, model, status, attempt, files, commands, collisions, reviews, nudges}`. `kind` is `root`, `manager`, or `worker`; `level` counts from 0.
- Produces: `self.store.event(run_id, "tree", <node without files, commands, collisions, reviews, nudges>)` on every change, through `TreeRun.update(node_id, **changes)`.
- Produces: `LocalWorkspace.tree = True` and `LocalWorkspace.extra_read_tools() -> list` (the specs of MCP tools with `read_only` set; `[]` without MCP).

- [ ] **Step 1: Write the test helper and failing tests**

A `Team` helper in `test_tree.py`: three connections, `strong-m` (tier strong), `standard-m`, `light-m`, all `tools=True`, `free=True`; it routes each request by `body["model"]` to a script `(body, done) -> reply(...)` and records `(model, tool names, task)` per call. Call IDs in scripts must be unique, because node IDs come from them.

```python
async def test_a_question_gets_an_answer_and_no_tree()          # Root replies; nodes == {'main'}; status 'done'.
async def test_do_it_myself_gives_the_root_worker_tools()       # First call has no write_file, next call has it and no delegate; hello.txt is written by strong-m.
async def test_two_levels_when_two_strengths_exist()            # Only strong-m and light-m: the child runs on light-m.
async def test_one_strength_means_the_root_works_alone()        # First call already has write_file and todo_write, no delegate or do_it_myself.
async def test_a_deep_branch_beside_a_shallow_one()             # Root delegates kind=manager and kind=worker; manager on standard-m delegates to light-m; the worker runs on standard-m.
async def test_parents_cannot_work_and_workers_cannot_delegate()# Root and manager tool lists hold no write_file, edit_file, bash; worker's holds no delegate. A root calling write_file gets 'there is no tool called write_file'.
async def test_manager_kind_is_only_offered_on_three_levels()   # On two levels the delegate schema's kind enum is ['worker'].
async def test_read_only_mcp_tools_reach_parents()              # A read-only MCP tool is in the root's list; a changing one only in the worker's.
async def test_the_thirteenth_agent_is_refused()                # 13 delegates: 12 run, the 13th result starts with 'Error' and names the limit.
async def test_a_level_of_managers_does_not_deadlock()          # agent_parallel=1, 3 managers with 2 workers each, under asyncio.wait_for(..., 30): completes.
async def test_bad_delegate_arguments_get_a_readable_error()    # kind='boss', and task='': results start with 'Error'; the run completes.
async def test_a_broken_model_falls_through_to_another()        # standard-m answers HTTP 500: its node's next calls go to another model; the run completes.
async def test_a_child_with_no_model_left_fails_alone()         # light-m and every fallback fail for one child only: status 'failed', the parent's result says so, its sibling finishes, the run completes.
async def test_the_server_agent_stays_flat()                    # A workspace without tree: AgentRun, delegate has difficulty and no kind.
```

- [ ] **Step 2: Run them**

Run: `uv run --frozen python -m pytest leo-cli/tests/test_tree.py -q`
Expected: FAIL, no module `backend.tree`, or no tree behaviour.

- [ ] **Step 3: Implement levels, node state, and `update`** in `backend/tree.py`

`levels()`: the distinct `tier` values of `self.o.candidates("agent", tools=True)[0]`, strongest first by `TIERS`, computed once and saved in the state, so a resume sees the same tree. With no candidates, fall back to `["strong"]` and let the call report the error.

`run()`: create the root node, then `await super().run()`; on a non-None answer, set the root to `done`.

- [ ] **Step 4: Implement `toolset`, `current_tools`, `call_options`, `after_step`, and `slot`**

- Root, more than one level, not solo: read tools, `todo_write`, `delegate`, `do_it_myself`.
- Root, one level or solo: `super().toolset(name, lead=False)` plus `todo_write`. Also `install_skill` when the workspace has skills, as today's lead does.
- Manager: read tools, `todo_write`, `delegate`.
- Worker: `super().toolset(name, lead=False)`.
- Read tools: `read_file`, `list_files`, `read_url`, `web_search` when set up, `use_skill` when the workspace has skills, and `extra_read_tools()`.
- `delegate` schema: `agent` (enum of skills), `task`, `kind` (enum `["worker", "manager"]` only for the root on 3 levels, else `["worker"]`).
- `call_options`: `{"tier": node["tier"]}`. `difficulty` stays the argument `loop` already passes, which `tool_delegate` sets to the node's tier.
- `after_step`: set the node's `model` from `result["model"]`.
- `slot`: `contextlib.nullcontext()`. `PARALLEL = {"delegate", "review_work"}`.
- `current_tools` returns `self.toolset(name, lead=name == "main")`, so `do_it_myself` takes effect on the next step.

- [ ] **Step 5: Implement `tool_do_it_myself(self, name, call)`**

Sets `solo`, saves, and returns a message that names the new tools and tells the root to verify by running. If children exist, it returns an `Error:` instead.

- [ ] **Step 6: Implement `tool_delegate(self, name, call, agent, task, kind="worker")`**

Order: validate (`task` not blank, `kind` allowed, else `Error: ...`); resolve the skill as the base class does; if the node does not exist, check `MAX_AGENTS` and create it at `parent level + 1`, `tier = levels[level]`, status `waiting`. A worker runs inside `async with self.gate:` with status `working`; a manager runs with no slot, status `planning`. Both use `self.loop(id, system, task, tools, "subagent", skill, tier, min(SUB_MAX_STEPS, agent_max_steps))`.

Afterwards the node is `in_review`, and the result is the report. A `ValueError` from the child's loop (no model left), other than `BudgetExceeded`, marks it `failed` and returns `Error: this subtask failed: <reason>`, so it does not stop its siblings.

- [ ] **Step 7: Implement the prompts:** `lead_system` (root), `manager_system(skill)`, and `sub_system(skill)` for workers.

They reuse `self.place`, `self.root`, `self.environment`, and the skill catalog. The root's must say:
- Read before deciding. Answer directly if no change is needed; call `do_it_myself` if the task is easy; otherwise split with `delegate`.
- Choose `kind="manager"` only for a subtask large enough to split again.
- Never give two children the same files. Each subtask is self-contained: what, which files, how to verify.
- Your children's work comes back for review; accept it only on the evidence.
- The final reply says what was done, how it was verified, and what is not done.

The manager's says the same without `do_it_myself`, and that its final message is its report to its parent. On one level, the root's prompt is today's working instructions with no mention of delegation.

- [ ] **Step 8: Set `tree = True` and add `extra_read_tools()`** on `LocalWorkspace`, reading `read_only` from `self.mcp.index`.

- [ ] **Step 9: Run them**

Run: `uv run --frozen python -m pytest leo-cli/tests -q`
Expected: all pass. Until Task 3, children stay `in_review`; that is fine here, because no test in this task asserts a verdict.

- [ ] **Step 10: Commit** `Tree agents: levels, roles, and delegation for the CLI`

---

### Task 3: Review, evidence, retries, and resume

**Files:**
- Modify: `backend/tree.py`
- Test: `leo-cli/tests/test_tree.py`

**Interfaces:**
- Consumes: Task 2's nodes, `update`, `tool_delegate`.
- Produces: `TreeRun.tool_review_work(self, name, call, agent, verdict, feedback="")`, `TreeRun.record` (extends the base), `TreeRun.finish_blocked`, and `TreeRun.evidence(node_id) -> str`.
- Produces: a run's final answer ends with this block when any node is `failed` or `not_reviewed` (exact heading): `\n\n**Not finished:**\n- <task> (<status>)`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_evidence_comes_from_what_ran_not_the_report()   # A worker reports 'all tests pass' but bash exited 1: the parent's tool result holds 'exit code 1' and the file it wrote.
async def test_long_evidence_is_cut_and_says_so()              # len(evidence) <= 4000 + the note; '…' note present.
async def test_two_workers_changing_a_file_warns_the_parent()  # Siblings write the same path: a 'warning' event names it, and both parents' evidence says so.
async def test_accept_marks_it_accepted()                      # status 'accepted'; the root then finishes.
async def test_a_reject_retries_with_the_feedback()            # Second attempt on the same model; its last message holds the feedback; review result holds the new report.
async def test_a_second_reject_moves_to_a_stronger_model()     # Attempt 3 under the root runs on strong-m; the transcript is kept.
async def test_a_third_reject_fails_and_reaches_the_answer()   # status 'failed'; output ends with '**Not finished:**' and the task.
async def test_a_reject_needs_feedback()                       # Empty feedback: result starts with 'Error'; attempt unchanged.
async def test_reviewing_someone_elses_child_is_refused()      # Unknown id, or another parent's child: 'Error'.
async def test_a_parent_cannot_finish_without_reviewing()      # A final message with an open child gets a reminder naming it.
async def test_two_reminders_then_not_reviewed()               # Third try ends it; status 'not_reviewed'; a warning event; the block in the answer.
async def test_a_manager_is_reviewed_by_the_root_with_its_childrens_evidence()
async def test_an_approval_deep_in_the_tree_pauses_and_resumes()  # A worker's write_file waits; answer once; it finishes; no call repeats.
async def test_a_restart_repeats_no_finished_work()            # Cancel the task mid-review, Sessions.resume: finished children get no new calls.
async def test_a_repeated_review_call_does_not_apply_twice()   # review_work re-run for the same call id after a pause: attempt goes up once.
async def test_stopping_stops_every_node()                     # sessions.stop mid-tree with siblings running: run 'cancelled', the tasks end, a new send works.
```

- [ ] **Step 2: Run them**

Run: `uv run --frozen python -m pytest leo-cli/tests/test_tree.py -q`
Expected: the new tests FAIL.

- [ ] **Step 3: Implement `record` and `evidence`**

`record` calls the base, then for a result that does not start with `Error`: `write_file` and `edit_file` add the path to the node's `files` (no duplicates); `bash` adds `{command, exit, tail}` to `commands`, with `tail` the last 3 output lines, from the `exit code N` first line `tool_bash` writes.

When a path is already in another worker's `files`, also store a `warning` event naming the path, and add it to the node's `collisions`, which `evidence` lists first.

`evidence(id)`: collisions, files, commands, then each accepted child's evidence, indented. It is cut to 4,000 characters, ending `… (evidence cut; read the files to check more)`. `tool_delegate` and `tool_review_work` return `report + "\n\nEvidence the engine collected:\n" + evidence`, and an instruction to call `review_work`.

- [ ] **Step 4: Implement `tool_review_work`**

Refuse an unknown id, a child of another parent, or a node not `in_review`, and an unknown verdict or a reject with blank feedback.

A new call id (tracked in the node's `reviews`, so a pause and resume cannot apply it twice):
- `accept`: status `accepted`; return a short confirmation.
- `reject`: at `attempt == 3`, status `failed`; return that no attempts are left. Otherwise `attempt += 1`, reset its step count, append the feedback as a user message to its transcript; if `attempt` is now 3, `tier = levels[level - 1]`. Status `retrying`, save.

Then run it, as in `tool_delegate` (workers in the gate), back to `in_review`, and return the report and evidence. A repeated call id skips straight to the running step: `loop` returns at once if it had already finished.

- [ ] **Step 5: Implement `finish_blocked` and the final block**

`finish_blocked`: for a parent with children in `in_review`: under 2 `nudges`, add a user message naming them, count it, and return True. Otherwise mark them `not_reviewed`, store a `warning` event, and return False.

`run()`: if any node is `failed` or `not_reviewed`, append the exact block. A stopped or cancelled run leaves working nodes as they are; the renderer treats them as stopped because the run ended (Task 4).

- [ ] **Step 6: Run them**

Run: `uv run --frozen python -m pytest leo-cli/tests tests -q`
Expected: all pass.

- [ ] **Step 7: Commit** `Tree agents: parents review with evidence, retries, and the failure report`

---

### Task 4: Showing the tree

**Files:**
- Create: `leo-cli/leo_cli/treeview.py`
- Modify: `leo-cli/leo_cli/ui.py` (`Renderer`, `App.message`, `App.cmd_usage`), `leo-cli/leo_cli/panels.py` (`usage_view`), `leo-cli/leo_cli/app.py` (`one_shot`)
- Test: create `leo-cli/tests/test_treeview.py`; add to `leo-cli/tests/test_app.py`, `leo-cli/tests/test_panels.py`

**Interfaces:**
- Consumes: the `tree` event, `run["state"]["agent"]["tree"]["nodes"]`, `Store.call_rows(run_id)`.
- Produces in `treeview.py`:
  - `agent_usage(rows) -> dict`: `{node_id: {calls, input_tokens, output_tokens, cost}}`, plus key `"compact"` for `agent:compact:n`; adds up completed calls only, from stages `agent:<id>:<step>`.
  - `live(nodes, usage, width, height, g, frame, activity=None) -> list`: prompt_toolkit fragments ending in a newline; `[]` when there is only the root.
  - `full(nodes, usage, g) -> rich renderable`: every node with its model, status, calls, tokens, and cost.
- Produces: `Renderer.tree: dict` (node id to the latest event), `Renderer.on_tree`, cleared by `reset()`.

- [ ] **Step 1: Write the failing tests**

```python
def test_rows_nest_with_branches_and_order()         # The spec's example: ├─ └─ │ prefixes, children under their parent, in creation order.
def test_finished_branches_collapse_first()          # Over the limit: '✔ 3 finished' before anything else collapses; then '○ 2 waiting'.
def test_the_tree_fits_short_and_narrow_terminals()  # height 10, width 50, 12 nodes: at most 3 lines (a third), each <= 50 cells; status text intact, task clipped.
def test_never_more_than_ten_lines()                 # height 100: at most 10.
def test_only_a_root_shows_nothing()                 # live(...) == [].
def test_ascii_terminals_get_ascii()                 # glyphs(unicode=False): the output is ASCII.
def test_usage_adds_up_from_the_ledger()             # Rows with agent:main:1, agent:sub-1:2, agent:compact:1, one failed call: totals per node; failed not counted.
def test_a_stopped_run_does_not_show_working()       # full() with a 'cancelled' run status: former 'working' rows read 'stopped'.
# test_app.py
async def test_the_prompt_shows_the_tree_while_working()   # Busy with 2+ nodes: message() has a tree line before the prompt; idle: it does not.
def test_the_answer_block_is_not_printed_twice()           # Streamed answer, done output == answer + '**Not finished:**...': the answer once, the block once.
# test_panels.py
async def test_usage_starts_with_the_latest_tree()         # /usage after a tree run: full tree above the model table; no tree after a flat session.
async def test_print_mode_shows_the_tree_on_stderr()       # one_shot: stderr holds the tree, stdout the answer alone.
```

- [ ] **Step 2: Run them**

Run: `uv run --frozen python -m pytest leo-cli/tests/test_treeview.py -q`
Expected: FAIL, no module `leo_cli.treeview`.

- [ ] **Step 3: Implement `treeview.py`**

Row format and glyphs follow spec 6.2: `g.ok` for accepted, `g.fail` for failed and not reviewed, the spinner for working, `g.idle` for waiting, `g.warn` for retrying. A parent with a child `in_review` reads `reviewing`. Add `tee`, `elbow`, and `pipe` to the `UNICODE` (`├─`, `└─`, `│ `) and `ASCII` (`|-`, `` `- ``, `| `) glyph sets. Measure with `get_cwidth`. The line limit is `min(10, height // 3)`.

- [ ] **Step 4: Wire the renderer and prompt**

`Renderer.on_tree` stores the node and prints nothing. `App.message` returns `treeview.live(...) + <prompt fragments>` while busy. Terminal height comes from `get_app().output.get_size()`, falling back to `shutil.get_terminal_size()`. Usage is cached for 1 second, because `message` runs on every redraw.

- [ ] **Step 5: Handle the answer block in `Renderer.on_done`**

If `output` starts with the streamed `answer`, print only what follows it, as Markdown. Today it would print the whole answer again.

- [ ] **Step 6: Wire `/usage` and `-p`**

`usage_view(..., tree=None)` prepends the view. `cmd_usage` passes the current session's latest run's nodes and `agent_usage(store.call_rows(run_id))` when there is more than the root. `one_shot` prints the same to stderr before the footer.

- [ ] **Step 7: Run them**

Run: `uv run --frozen python -m pytest leo-cli/tests -q`
Expected: all pass.

- [ ] **Step 8: Commit** `Show the agent tree live above the prompt and in /usage`

---

### Task 5: Rename to L.E.O.C.O.D.E

**Files:**
- Modify: `leo-cli/pyproject.toml`, `leo-cli/npm/package.json`, `leo-cli/npm/bin/leo.js`, `leo-cli/npm/README.md`
- Modify: `leo-cli/leo_cli/{__init__,app,ui,account,update,models,mcp,workspace}.py`
- Modify: `backend/api.py`, `backend/mcp_server.py` (text people read only)
- Modify: `frontend/src/components/AuthPage.astro`, `frontend/src/pages/{cli,usage,index}.astro`, `frontend/src/scripts/chat.ts`
- Test: `leo-cli/tests/test_app.py`, `test_account.py`; `tests/test_agent.py`, `test_cli_server.py`

**Interfaces:**
- Produces: `leo_cli.__init__.NAME = "L.E.O.C.O.D.E"`, used by the Python strings in this task.
- Produces: `ui.BANNER`, the block-letter L.E.O.C.O.D.E, 6 lines, at most 78 columns.

- [ ] **Step 1: Write the failing tests**

```python
def test_leocode_and_leo_are_both_commands()        # tomllib on pyproject: both scripts point at leo_cli.app:main; npm package.json bin has both.
def test_the_logo_fits_at_eighty_columns()          # welcome at width 80: logo present, every line <= 80. At width 79: no logo, 'L.E.O.C.O.D.E' as text.
def test_nothing_says_the_old_name()                # grep leo_cli/*.py and the listed frontend files for 'L.E.O CLI': none.
```

Update the existing assertions: `'leo login'` becomes `'leocode login'` (`test_app.py`, `test_account.py`, `tests/test_cli_server.py`), `'L.E.O CLI'` in `tests/test_agent.py` becomes `'L.E.O.C.O.D.E'`, and the old logo strings in `test_app.py` become the new logo's bottom row.

- [ ] **Step 2: Run them**

Run: `uv run --frozen python -m pytest leo-cli/tests/test_app.py -q -k "leocode or logo or old_name"`
Expected: FAIL.

- [ ] **Step 3: Add the commands**

`leocode = "leo_cli.app:main"` beside `leo` in `pyproject.toml`; both names in the npm `bin`. `prog="leocode"` in the argparse parser, and its `usage` text.

- [ ] **Step 4: Replace the name in text people read**

Replace `L.E.O CLI` with `L.E.O.C.O.D.E`, and `leo <command>` in messages with `leocode <command>`, across the files listed. In `AuthPage.astro`, also remove `<em>Soon</em>`. Code comments in `backend/` may stay.

- [ ] **Step 5: Draw the new logo**

Compose `BANNER` in the same block font as today's letters. In `welcome`, print `banner()` above the panel at width 80 or more, and the name as text inside the card otherwise. This replaces the side-by-side layout. `first_sign_in` shows the logo only at that width too. Its 6 gradient styles still apply line by line.

- [ ] **Step 6: Run them, and build the website**

Run: `uv run --frozen python -m pytest leo-cli/tests tests -q`
Expected: all pass.

Run: `npm run build --prefix frontend`
Expected: exit 0.

- [ ] **Step 7: Reinstall the command on this Mac**

Run: `uv tool install --editable --reinstall leo-cli`, then `leocode --version` and `leo --version`.
Expected: both print the version.

- [ ] **Step 8: Commit** `Rename L.E.O CLI to L.E.O.C.O.D.E, with a leocode command`

---

### Task 6: Docs and checks in a real terminal

**Files:**
- Modify: `leo-cli/README.md`, `README.md` (mentions of the CLI), `docs/superpowers/specs/2026-11-06-leo-code-tree-agents-design.md` (status)

- [ ] **Step 1: Update the README**

Rename the title and move the examples to `leocode`, noting that `leo` also works. Replace "hands focused subtasks to sub-agents" with a short "How the tree works" section: levels, who works, review and retries, the limit of 12, and what shows in the prompt and `/usage`. Keep the `.leo-cli` paths: they did not change.

- [ ] **Step 2: Check it in a real terminal**

With a demo script in the scratchpad (a fake team of 3 models whose scripts sleep between steps), run the real `App` in tmux at 110x45, 64x24, and 50x12. Check that:
- the tree appears above the prompt, updates, and collapses;
- the prompt stays usable while it runs;
- an approval deep in the tree shows its menu and resumes;
- Esc stops all of it;
- `/usage` shows the finished tree.

Expected: no stray lines, nothing wrapped, and no error in stderr.

- [ ] **Step 3: Mark the spec implemented, run everything, and commit**

Run: `uv run --frozen python -m pytest leo-cli/tests tests -q`
Expected: all pass.

Commit: `Docs: how L.E.O.C.O.D.E's agent tree works`

- [ ] **Step 4: Hand over the benchmark comparison, without running it**

The spec's cost goal needs real quota, so it is not run automatically. Note that `benchmarks/agent.py` drives the server's flat agent, so it is the baseline, not the tree. The comparison is to run the prompts of `benchmarks/agent_tasks.py` with `leocode -p`, then compare passes and strong-model tokens from `/usage`. Tell the user this, and offer to run it when they say the quota can be spent.
