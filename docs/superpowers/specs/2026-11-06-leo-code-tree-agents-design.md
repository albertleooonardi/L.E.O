# L.E.O.C.O.D.E: tree agents, where parents review their children

**Date:** 6 November 2026  
**Status:** design, waiting for review  
**Replaces in the CLI:** the flat "lead agent plus sub-agents" of [agent mode](2026-11-04-agent-mode-design.md).
**Renames:** [L.E.O CLI](2026-11-04-leo-cli-design.md) to L.E.O.C.O.D.E.

## 1. Goal

A team of mostly free models should match one strong model at lower token cost. Strong models judge and cheap models
do the volume: the task is split down a tree of weaker and weaker models, and every result is reviewed by the agent
above it on its way back up.

**Success means:**
- On the existing agent benchmark tasks, results are at least as good as today's flat agent, at equal or lower cost in
  strong-model tokens. This is measured by hand after the build, because it spends real quotas.
- An easy task stays about as cheap as today: the lead agent already runs on the strongest model, so the root adds at
  most one call to decide, not a whole tree.
- A subtask that cannot be finished is reported in the final answer, never hidden.
- You can see the tree working, and afterwards what each agent cost.

## 2. Decisions

| Question | Decision |
|---|---|
| Where does it apply? | The CLI only. The server's agent, Chat's modes, and the benchmarks stay as they are. |
| Who does the work? | Only the bottom level of a branch edits files and runs commands. Parents read, split, and review. |
| How deep? | At most 3 levels, one per strength: strong, standard, light. Every level is weaker than its parent. |
| Who decides depth? | The root, always: it reads the project and decides for each subtask whether it splits further. |
| What if a parent rejects work? | The same child retries with the feedback, then the next stronger model tries once, then the failure goes up. |
| How many agents? | At most 12 per run, on top of the existing budgets. |
| Name | L.E.O.C.O.D.E in everything people read; a `leocode` command, with `leo` kept. Folders and package names stay. |

The approach is to grow the existing loop in `backend/agent.py`, not to plan the whole tree up front (it could not adapt
to what agents find in the code) and not to write a CLI-only engine (it would duplicate routing, budgets, and resume).

## 3. The tree

### 3.1 Agents

| Agent | Level | Tools |
|---|---|---|
| **Root** | Strongest strength available | Read tools, `delegate`, `review_work`, `todo_write`, `do_it_myself` |
| **Manager** | The middle strength; only on 3-level trees | Read tools, `delegate`, `review_work`, `todo_write` |
| **Worker** | The weakest strength on its branch | Today's sub-agent tools: edits, `bash`, MCP tools, and read tools |

Read tools are `read_file`, `list_files`, `read_url`, `web_search` (when set up), and `use_skill`. MCP tools that only
read are read tools too. The tool list is what enforces the roles: a parent has no way to edit a file or run a command.

### 3.2 Levels

When a run starts, the engine lists the strengths among enabled models that can call tools, strongest first. That list
is the run's levels:

| Your models | Levels | Tree |
|---|---|---|
| strong, standard, light | 3 | root → manager or worker (standard) → worker (light) |
| strong, standard | 2 | root → worker (standard) |
| strong, light | 2 | root → worker (light) |
| One strength only | 1 | The root works alone, with worker tools and `todo_write` from the start. |

A child is always one level below its parent. Whether it is a manager or a worker is the parent's choice.

### 3.3 How the root decides depth

The root first reads what it needs, then does one of three things:
- **Answers:** a question that needs no changes gets a reply. The run ends.
- **`do_it_myself`:** the task is easy. The root gets worker tools (and keeps `todo_write`) for the rest of the run, a
  1-level tree, and can no longer delegate. This keeps "only the bottom level works" true.
- **`delegate`:** the task is split. For each subtask the root chooses `kind`: `worker` or, on a 3-level tree, `manager`.
  So one tree can be deep in one branch and shallow in another.

`delegate` keeps its name and gains `kind`. Its `difficulty` argument goes away, because the level now picks the model.
A manager's `delegate` always creates workers.

### 3.4 Models

`Orchestrator.call` gains a `tier` argument. With a tier, candidates are ordered:
1. Models of that strength, ranked by skill and cost as today.
2. The next stronger strengths, nearest first. A rate-limited or failing model hands over to these.
3. Weaker strengths, as a last resort, so that a run does not fail while a model is still available.

Models that are known to call tools stay ahead of untried ones.

### 3.5 Running siblings in parallel

`agent_parallel` (3 by default) limits **workers**, and a worker holds its slot only while it runs. Parents waiting for
children hold no slot: if they did, a full level of managers would wait forever for workers that cannot start.

Siblings share the folder. Parents are told never to give two children the same files. The existing "Sessions both
changed this file" check also runs between workers of a tree, and a collision becomes a warning for the parent, so that
it can look at the file in its review.

### 3.6 Turning it on

The engine stays flat unless the workspace asks for a tree: `getattr(self.sandbox, "tree", False)`, the same kind of
hook as `extra_tools`. `LocalWorkspace` sets it. The server's agent keeps today's behaviour, tools, and prompts.

## 4. Review

### 4.1 What a parent sees

When a child finishes, its `delegate` call returns:
- the child's **report**, and
- **evidence the engine collected**: files written or edited, and commands run with their exit code and last lines. A
  manager's evidence includes its accepted children's. It is cut to 4,000 characters, and a cut says so.

So a child cannot simply claim that the tests pass. The parent can read the files, but it cannot run anything; to get a
check run, it asks for it in its feedback.

### 4.2 The verdict

`review_work(agent, verdict, feedback)` takes `accept` or `reject`, and feedback is required with a reject.

| Attempt | After a reject |
|---|---|
| 1 | The same child continues its own conversation with the feedback. |
| 2 | The next stronger model takes over the same conversation. Only here may a child be as strong as its parent. |
| 3 | No attempts are left. The child is marked `failed`, and the parent is told. |

`review_work` returns the new report and evidence, so the parent reviews again. Several reviews in one turn run in
parallel, like `delegate`.

### 4.3 Nothing skips review

A parent cannot finish while a child is unreviewed: the engine replies to its final message with the list of open
children. After 2 such replies, those children are marked `not reviewed` and the run carries on with a warning, so a
stubborn model cannot hang the run.

### 4.4 Results go up

A manager's final message is its report to its parent, with the evidence above, so the root reviews the managers. The
root's final message is the answer you see.

If any node ended `failed` or `not reviewed`, the engine adds a short factual list to the end of the answer. The root is
also told to say what is not done, but the list does not depend on it doing so.

## 5. Limits and failure

- **Agents:** the 13th `delegate` of a run returns an error that tells the parent to use the agents it has, or to report
  the rest as not done. A retry is not a new agent.
- **Budgets:** `agent_max_calls`, `agent_token_budget`, `agent_max_cost`, and `agent_max_run_seconds` apply to the whole
  tree, and asking to extend them works as today.
- **Steps:** a worker keeps `SUB_MAX_STEPS` per attempt. Parents share the same limit, except the root, which keeps
  `agent_max_steps`.
- **Approvals:** edits, commands, and MCP tools ask with today's menu. A question from any worker pauses the whole tree,
  and answering resumes it.
- **Resume:** nodes live in the run's state with the transcripts, so a closed window or a restarted run carries on from
  the checkpoints: agent IDs come from the call that created them, and a finished `delegate` is not run again.
- **Steering:** your messages go to the root, which can pass them on in a review or a new subtask.

## 6. Showing the tree

### 6.1 Events

A new `tree` event carries a node each time it changes: `{id, parent, kind, level, task, skill, model, status, attempt}`.
Status is one of `waiting`, `planning`, `working`, `in_review`, `retrying`, `accepted`, `failed`, or `not_reviewed`.
Only the renderer reads it, so the website is unaffected.

### 6.2 Live, above the prompt

The tree is drawn directly above the prompt, as extra lines of the prompt's message, so the output keeps scrolling above
it. The wave and shimmer line stays in the status bar below the prompt:

```
◆ Dots3-Note  reviewing                                  2.1K
├─ ✔ Auth module         Nemotron Lightning  accepted    3.4K
│  ├─ ✔ Login route      North Mini          accepted     900
│  └─ ⠹ Tests            North Mini          bash(pytest) 400
└─ ○ Docs                                    waiting
```

- It takes at most a third of the terminal's height, and never more than 10 lines.
- When it does not fit, finished branches collapse first (`✔ 3 finished`), then waiting ones (`○ 2 waiting`).
- Rows are cut to the terminal's width; the status stays and the task text gives way.
- A 1-level tree shows nothing extra.
- An approval menu replaces the prompt while it is open, so the tree is hidden until it is answered.
- Tool lines keep scrolling above it, marked with the agent's `#tag` as today.

### 6.3 `/usage`

`/usage` starts with the current session's latest tree, in full, with calls, tokens, and cost per agent. It adds this up
from the call ledger: stages are already `agent:<id>:<step>`, so no schema change is needed. Context summaries
(`agent:compact:n`) get their own line, because they belong to no agent. The tables by model below it stay.

### 6.4 `leocode -p`

The tree goes to stderr once when the run ends, with the footer. The answer alone goes to stdout, as today.

## 7. The rename

- **Name:** L.E.O.C.O.D.E in the banner, help, messages, the README, and the website's sign-in page, Usage page, Settings,
  and the sign-in card. The card's outdated "Soon" badge goes.
- **Command:** `leocode` is added in `pyproject.toml` and the npm launcher. `leo` keeps working, and README examples move
  to `leocode`.
- **Logo:** a new block-letter L.E.O.C.O.D.E. It is about 75 columns wide, so it sits above the card at 80 columns or more;
  on narrower terminals the card shows the name as text.
- **Unchanged, so your sign-in, settings, and sessions keep working:** `~/.config/leo-cli`, `.leo-cli`, `LEO_CLI_HOME`,
  the Python packages `leo_cli` and `leo-cli`, and the npm package `leo-orchestrator`.

## 8. Testing

Scripted fake models, one per strength, through the real engine and a real folder:
- **Depth:** an answer only; `do_it_myself`; 2 levels; a 3-level branch beside a shallow one; each missing-strength row
  of the table in 3.2.
- **Roles:** a parent calling `write_file` or `bash` gets "no such tool"; workers cannot `delegate`.
- **Models:** each level calls a model of its strength; a rate-limited light model hands over to standard.
- **Review:** accept; reject then accept; both retries and the stronger model; a failure reaching the final answer; a
  parent that will not review; evidence coming from what really ran, not from the report.
- **Limits:** the 13th agent is refused.
- **Parallel:** a level of managers does not deadlock with `agent_parallel` set to 1.
- **Resume:** an approval deep in the tree pauses and resumes, and a restart repeats no finished work.
- **Flat stays flat:** the server's agent tests pass unchanged.
- **Display:** the tree renderer at several widths and heights, collapsing, and `/usage` totals matching the ledger.
- **Real terminal:** a run in tmux against the fake models, as with the earlier CLI work.

## 9. Known risks

- **More calls.** Reviews add calls at every level, and every strong-model call uses a free quota. If the benchmark shows
  that the tree costs more than flat, the first things to try are fewer review rounds and cheaper review prompts.
- **One light model.** You have one light model today, so a 3-level tree sends much of its work there. Rate limits move
  that work up to standard models, which works, but costs quota there.
- **Parents cannot run tests.** A parent can only ask for a check. This keeps the roles strict, at the price of an extra
  round when it wants one.

## 10. Not in this design

- A tree for the server's agent or for Chat.
- More than 3 levels, or a finer ranking than strong, standard, and light.
- Re-splitting rejected work into new, smaller children.
- Changing the config folders or package names.
