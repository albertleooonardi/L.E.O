# Checkpoint 3: Sandbox timeout cleanup fixed; real-Docker rerun pending

**Date:** 4 November 2026  
**Repo:** `/Users/leonardi/Documents/Project/L.E.O` (macOS; checkpoints 1 and 2 were on Windows)  
**Previous checkpoint:** [2026-11-04-agent-mode-checkpoint-2.md](2026-11-04-agent-mode-checkpoint-2.md)

## 1. State at the start of this session

- All agent-mode work from checkpoints 1 and 2 is committed in `41f9765` ("update 04/11/2026"). The tree was clean,
  so the earlier "dirty working tree" warning no longer applies.
- Docker is **not installed on this Mac**: `/usr/local/bin/docker` points to a missing `/Applications/Docker.app`.
  The real-Docker tests could not be run here.
- The `setsid` change from checkpoint 2 had broken a unit test that checkpoint 2 never reran:
  `tests/test_sandbox.py::test_sandboxd_exec_demultiplexes_output_and_runs_unprivileged` (42 passed, 1 failed).

## 2. Root cause of `test_command_timeout_kills_the_process_group`

The test measured the wrong thing. Every sandbox container runs `CMD ["sleep", "infinity"]` as its keep-alive, so
`ps -eo comm | grep -c sleep` is always at least `1`. The "1 leftover sleep" seen in checkpoint 2 was the container's
own keep-alive, not a leak. PID 1 is docker-init (`"Init": True`), so orphaned children are reaped.

Reading the cleanup code turned up two real bugs:

1. `kill -KILL -<info["Pid"]>` used the exec-inspect `Pid`, which is a **host** PID. Inside the container's PID
   namespace it names the wrong process group or none at all.
2. The cleanup exec was started with `Detach: True`, so `exec()` could return before the kill had run.

GNU `timeout` (without `--foreground`) already makes its own process group and signals the whole group, so the
explicit kill mattered only for children that left that group (`setsid`, `set -m` job control). Those were exactly
the ones the host-PID kill could never reach.

## 3. Changes

- `backend/sandboxd.py` `Docker.exec()`:
  - `setsid --wait` removed; the command is `timeout --kill-after=5 <s> bash -lc <command>` again.
  - Each exec gets a random `LEO_EXEC=<marker>` environment variable, inherited by every process the command starts.
  - On timeout, a cleanup exec running as uid 1000 scans `/proc/*/environ` for the marker three times and
    `kill -KILL`s every match. It is attached, not detached, so `exec()` returns only after the sweep finishes.
  - Known gap: a process that clears its own environment (`env -i ...`) escapes the sweep.
- `tests/test_sandbox.py`: the fake Docker records every exec spec and start body and has a configurable exit code.
  New test `test_sandboxd_timeout_kills_every_process_the_command_started`. The existing test also asserts that no
  cleanup runs after a normal exit.
- `tests/test_sandbox_docker.py::test_command_timeout_kills_the_process_group` now runs
  `sleep 300 & set -m; sleep 301 >/dev/null 2>&1 & set +m; sleep 302`. The middle job escapes timeout's process
  group, so the marker sweep has to work. Leftovers are counted by exact command line, so `sleep infinity` and
  `<defunct>` zombies don't count.
- Root `Dockerfile`: `COPY backend/` moved out of the shared `python` stage into each final stage, after sandboxd's
  apt layer. A backend source change no longer reinstalls git.

## 4. Verification

```text
tests/test_sandbox.py + test_agent.py + test_discovery.py + test_agent_benchmark.py   44 passed, 1 warning
full suite                                    142 passed, 7 skipped, 9 failed, 1 warning
```

- The 9 failures are the pre-existing `tests/test_orchestrator.py` failures listed in checkpoint 1, not agent mode.
- The 7 skips are `tests/test_sandbox_docker.py`, which run only when `LEO_TEST_SANDBOX_URL` is set.

### Real Docker (Docker Desktop 29.9.2, arm64)

- All images build, including the reordered root `Dockerfile`.
- `tests/test_sandbox_docker.py`: **7 passed**.
- The test exercises the sweep. In a throwaway `leo-sandbox` container, `timeout` alone leaves `sleep 301` running
  in its own process group, next to the container's `sleep infinity` (PID 7). Through sandboxd, nothing is left.

### Full compose stack and browser smoke test

- `docker compose -f compose.yaml -f tests/compose.sandbox-test.yaml up -d --build` starts all seven services; api and
  postgres are healthy, and the api container reaches `http://sandboxd:8100/health`.
- In the browser on `http://localhost:8000`: the sign-in page renders, registering the first local user works and lands
  on the chat page, and the composer offers and accepts "Agent · works in a sandbox". The only console error is the
  expected 401 from the signed-out session check. No errors in the api, worker or sandboxd logs.
- Test account for this Postgres stack: `.leo-bench/qa/compose-smoke-account.txt` (git-ignored).

### End-to-end agent run

- The user connected OpenRouter in Settings. Nine free models were adopted, all with `tools: true`, which confirms the
  discovery change on a real catalog.
- Task in Agent mode: "Create calc.py with add(a, b) and a pytest test for it, run the tests, and show me the output."
  It was routed to `nvidia/nemotron-3.5-super-vl:free` and completed in 9.6 s: 4 calls, 7,496 input and 614 output
  tokens, $0 estimated.
- Checked independently in the workspace container: `calc.py` and `test_calc.py` exist, owned by uid 1000, and
  rerunning `pytest` gives 4 passed. No stray processes were left.

## 5. Bug found by the end-to-end run, and fixed

Files written by the agent had mtime 1 January 1970: `tarfile.TarInfo` defaults to `mtime = 0`, and
`backend/sandbox.py` never set it. A real-Docker reproduction (`test_rewritten_module_is_not_served_from_a_stale_pyc`)
showed the effect: the agent writes `VALUE = 2` over `VALUE = 1`, Python reuses the cached `.pyc` because mtime and
size match, and prints `1`. An agent could edit code and keep testing the old version.

- `backend/sandbox.py`: file and directory tar entries get the current time, which `make` and similar tools need.
- That alone does not fix the reproduction: `.pyc` freshness uses whole-second mtime plus size, and an agent can
  rewrite a file within one second. That applies to in-sandbox edits like `sed -i` too.
  `backend/sandboxd.py` now starts sandbox containers with `PYTHONDONTWRITEBYTECODE=1`, which pytest's
  assertion-rewrite cache also honours. Existing sandbox containers keep their old environment until recreated.
- Tests: unit tests for the current mtime and the container env; the real-Docker regression test above.

Results after the fix: real Docker **8 passed**; `tests/test_sandbox.py` 19 passed; full suite 143 passed, 8 skipped,
9 failed (all nine the known `tests/test_orchestrator.py` failures).

## 6. Next steps

1. Ask the user before running the live benchmark (it spends model quota).
2. Stop the stack when done: `docker compose -f compose.yaml -f tests/compose.sandbox-test.yaml down`.
3. Possible follow-up, not changed here: a background process that keeps stdout open (`npm run dev &`) holds Docker's
   output stream open, so `exec()` returns only at `timeout + 20` and then reports `timed_out`.
