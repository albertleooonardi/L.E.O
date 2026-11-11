# Personal-install verification — 11 November 2026

[← README](../../README.md) · [Server hosting](../self-hosting.md)

The Docker-free personal setup was tested from a fresh temporary checkout with its own Python environment,
frontend dependencies, router runtime, CLI tool directory, app database, and CLI data directory. Existing Docker
services and user accounts were not reset. The repository was also confirmed publicly accessible without GitHub authentication.

| Boundary | Evidence |
| --- | --- |
| Installer | `./setup.sh` completed: locked app dependencies installed, frontend built, pinned 9Router 0.5.112 installed, and both `leo`/`leocode` commands installed. |
| Standalone router | Real Node server booted on loopback; authenticated `/api/providers` succeeded; a SQLite router database was created. No Docker router was used. |
| Local accounts | Fresh registration and password login succeeded against the personal API. Safari displayed the signed-in account. |
| Models UI | Safari showed **Model router connected**. The provider catalog correctly requested a provider connection on the fresh empty router. |
| App execution | A local deterministic model fixture produced a completed Quick run through the app API, durable worker, and SQLite store. No paid model request was made. |
| CLI | Device authorization, real `Account.sync()`, the installed `leocode models`, and the checkout's `./leocode --version` succeeded. |
| Restart | After graceful shutdown and restart, password login, the completed answer, model connection, CLI device token, and authenticated router access all remained valid. |
| Storage | App `leo.db` and per-user router `db/data.sqlite` existed under the personal data directory; CLI data used a separate local QA directory. |
| Docker | `docker compose config --quiet` passed; `docker compose build api worker sandboxd` completed successfully. Existing server deployments continue to use sandboxd and private per-user containers. |
| Regression | **426 passed, 9 skipped**, one existing Starlette/httpx deprecation warning. The skipped cases are opt-in live Docker sandbox tests. |
| Frontend | Astro checked 16 files with 0 errors and 2 existing hints. |

The first full regression pass hit the existing timing-sensitive approval-menu test. Its isolated rerun passed,
followed by a clean full-suite run. Restart testing also found and fixed macOS port reuse during process recovery;
the lifecycle test now exercises this case along with user-data isolation and refusal to attach to an occupied port.

Provider account sign-in and API-key calls use the same connection handlers as Docker, with the runtime manager
swapped to local processes. Automated tests cover provider setup and CLI sync on this path. This run did not perform
a new real-provider OAuth login, spend hosted model quota, or run native Windows/Linux installation tests. Windows
personal setup is documented through WSL2; the live personal smoke test used macOS with Node 24 and Python 3.12.

## Reproduce

```bash
./setup.sh
./start.sh
# Create a local account and connect a provider in Settings → Models.
# Keep the app running; in another terminal, start leocode in your project.

make check
docker compose config --quiet
docker compose build api worker sandboxd
```

Set `LEO_PORT=8001` if another app uses port 8000. A different app store can be selected with
`./start.sh /path/to/private-qa-data`, and `LEO_CLI_HOME` selects an isolated CLI store. Keep test credentials and
database files outside version control. The two previously documented model-selection defects remain unchanged;
see [algorithm QA](algorithm-verification-2026-11-11.md).
