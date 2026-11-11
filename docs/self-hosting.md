# Host L.E.O. on your server

[← README](../README.md) · [Configuration reference](configuration.md)

Personal installs use `./start.sh`, SQLite, and local 9Router processes. For a shared or remote installation,
Docker Compose runs the app, background worker, PostgreSQL, model routers, search, and optional sandbox services.
No hosted L.E.O. account or external managed database is required.

## Start the Docker version

```bash
git clone https://github.com/albertleooonardi/L.E.O.git
cd L.E.O
cp .env.example .env
```

Edit `.env`: replace the development values for `LEO_DB_PASSWORD`, `LEO_SANDBOX_TOKEN`, and `SEARXNG_SECRET`.
For the initial loopback-only bootstrap, leave `LEO_API_TOKEN` empty and retain the default loopback host settings. Then:

```bash
docker compose up --build -d
docker compose ps
curl --fail http://127.0.0.1:8000/api/health
```

Create the first account on the loopback website before enabling the public hostname. On a remote server, use an
SSH tunnel (`ssh -L 8000:127.0.0.1:8000 user@your-server`) and open `http://localhost:8000` on your computer.
Registration is restricted by default; the first user is the administrator.
`LEO_ALLOW_REGISTRATION=true` enables public registration if you choose it. The app account password is selected on
the website; it is separate from the PostgreSQL password and internal router passwords.

## Use a public domain

Compose intentionally binds HTTP to `127.0.0.1:8000`. Put an HTTPS reverse proxy on the same host in front of it.
For example, set these variables for your own domain:

```dotenv
LEO_PUBLIC_URL=https://leo.example.com
LEO_ALLOWED_HOSTS=leo.example.com,localhost,127.0.0.1
LEO_ALLOWED_ORIGINS=https://leo.example.com
```

After creating the owner account, set `LEO_API_TOKEN` to a separate strong admin token and restart Compose.
Keep it set when using non-loopback hostnames. A Caddy configuration on the host can proxy to L.E.O.:

```caddyfile
leo.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Configure DNS for your server and allow the proxy to obtain its TLS certificate. A proxy running in another container
needs access to the app's Docker network rather than its own `127.0.0.1`. Preserve streaming responses for chat events.
The built-in ChatGPT/Codex browser callback uses `localhost:1455`, so that sign-in flow requires a local installation
or an SSH tunnel from the browser machine to the server. Device-code providers use the displayed verification link.

The CLI connects with `leocode login --server https://leo.example.com`. App accounts, chats, provider credentials,
and the database remain on your server. CLI transcripts remain on each CLI user's own computer; usage totals sync back.

## Persistent storage and backups

| Store | Docker location |
| --- | --- |
| Accounts, chats, settings, usage, and model connections | PostgreSQL volume `postgres-data` |
| L.E.O. encryption key | `leo-data` volume mounted at `/app/.leo` |
| Provider accounts and router databases | Per-user volumes named `leo-router-<user-id>` |

Volume names for Compose services usually include the project prefix. Inspect actual names with `docker volume ls`.
Back up PostgreSQL using its backup tools, the encryption key, and **all** per-user router volumes as one recoverable set.
For a consistent filesystem snapshot, stop the services and router containers before copying their data.
Keep `.env` private and retain any configured `LEO_ENCRYPTION_KEY`. Do not publish backups or commit them to Git.
An existing personal `.leo/` database is not automatically imported into PostgreSQL.

`docker compose down` preserves named volumes. `docker compose down -v` removes Compose-managed volumes and is a data-reset
operation; per-user router containers and volumes are created separately by `sandboxd`.

## Updates and diagnostics

```bash
git pull --ff-only
docker compose up --build -d
docker compose logs --tail=100 api worker sandboxd
```

Back up before updating: schema migrations are not available yet. The personal launcher and Compose should not both
use port 8000 on the same host. Search from Compose uses `http://searxng:8080`; host Ollama is available through
`http://host.docker.internal:11434/v1` where Docker provides that host mapping.

Server-side code execution remains optional (`LEO_SERVER_AGENT=1`) and uses Docker sandboxes. Local coding is handled
by L.E.O.C.O.D.E, which runs with the CLI user's own permissions and approval controls.
