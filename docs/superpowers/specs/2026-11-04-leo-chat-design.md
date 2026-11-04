# L.E.O Chat: the web chatbot, with remote MCP connectors and no agent mode

**Date:** 4 November 2026  
**Status:** approved in conversation; implementation follows directly (the user asked to skip the separate plan step)  
**Part of:** L.E.O. as two products on one account system ([accounts spec](2026-11-04-accounts-oauth-design.md)):

- **L.E.O Chat**: the LLM Efficiency Orchestrator, a web chatbot like the Claude, ChatGPT, or Gemini websites (this
  spec).
- **L.E.O CLI**: the multi-agent LLM Efficiency Orchestrator, a terminal agent working on the local machine. Separate
  spec; still in development.

## 1. Scope

**Stays:**
- Multi-provider routing, including the Skills tab (General, Frontend, Backend, Coding...), which picks the model and
  instructions for each task.
- Web search.
- The HEAD → planner → sub-agents → reviewer process (Auto, Quick, Reviewed, Deep), budgets, usage tracking, and
  recovery.

**Leaves Chat:**
- **Agent mode:** sandbox workspaces, file and command tools, git clone and push.
- **Agent skills** (packaged instructions and scripts for an agent). These belong to the CLI.

**New:** each user's own remote MCP servers ("connectors"), used by the sub-agents. Every tool call needs approval
unless the user has allowed that tool.

## 2. Agent mode leaves Chat

- `LEO_SERVER_AGENT=1` turns server-side agent runs on. It is unset by default, and then the API refuses `mode: agent`
  and the workspace endpoints, and the MCP server does not offer `agent`. The agent code stays as the CLI's starting
  engine.
- The web UI drops the Agent option, the Files button, and the workspace dialog.
- Old agent conversations stay readable. A new message in one is refused with "Agent work is moving to L.E.O CLI".
- sandboxd stays, because it runs each user's model router. The sandbox image stays in compose until the CLI spec
  decides.

## 3. Connectors

**Storage:** a `connectors` table, per user:

| Column | Holds |
|---|---|
| `id`, `user_id` | Owner; only they can see or use it. |
| `config` (JSON) | Name, URL, `enabled`, `auth` (`none`, `token` or `oauth`), status and error, the cached tool list (`name`, `description`, `input_schema`, `read_only`, `destructive`), and a permission per tool (`ask`, `allow` or `deny`; new tools start at `ask`). |
| `secret` (Fernet) | The token, or OAuth tokens plus client credentials. |

**Protocol:** MCP over Streamable HTTP (`backend/mcp_client.py`).
- `initialize`, then `notifications/initialized`, a paged `tools/list`, and `tools/call`.
- Requests are JSON-RPC POSTs with `Accept: application/json, text/event-stream`. Responses can be JSON or an SSE
  stream, and `Mcp-Session-Id` is carried along.
- The older HTTP+SSE transport is not supported.
- Refreshing tools drops the permissions of tools that disappeared and adds new ones at `ask`.

**Signing in:**
- **Token:** a pasted bearer token.
- **OAuth** (`backend/mcp_auth.py`, following the MCP authorization spec):
  1. On a `401`, read the protected-resource metadata, from `WWW-Authenticate`'s `resource_metadata` or
     `/.well-known/oauth-protected-resource`.
  2. Read the authorization server's metadata (`oauth-authorization-server`, then `openid-configuration`).
  3. Register L.E.O. dynamically (RFC 7591), or use a client ID and secret the user pastes when the server does not
     allow that.
  4. Run the authorization-code flow with PKCE and `resource` (RFC 8707), returning to
     `/api/connectors/oauth/callback`.
  5. Store the tokens encrypted. They are refreshed on expiry or on a `401`; if refreshing fails, the status becomes
     "Sign in again".
- This reuses the accounts code's PKCE, `state`, and encrypted-cookie approach.

**Safety:**
- URLs must be HTTPS and resolve to public addresses (the web-search check). `LEO_MCP_ALLOW_PRIVATE=1` allows `http://`
  and private networks for self-hosted servers.
- Every request has a timeout.
- Tool results are cut to 20,000 characters, non-text content becomes a short note, and results are given to models as
  untrusted data.
- Tools are exposed to models as `<connector slug>__<tool>`. Schemas that are not JSON objects are skipped with a
  warning.

## 4. Tools in the task process

**Choosing connectors:** a run takes `connectors: [ids]`, the connectors switched on for that chat in the composer
(all enabled ones by default). Denied tools are never offered.

**Who gets tools:**
- **Quick and Reviewed:** the generating sub-agent gets the tools of every active connector and decides whether to use
  them.
- **Deep:** the planner sees a short catalog (connector names plus a one-line summary of their tools) and can give a
  task `"connectors": [...]`. Only that task's sub-agent gets those tools.
- **Planner, reviewer, and repairs** never call tools. The reviewer sees the tool log (tool, arguments, and a short
  result).
- **Routing:** a task with tools only goes to models with tool support. If there is none, it answers without tools and
  warns.

**The tool loop:**
- Up to `max_tool_calls` rounds per sub-agent (a new preference, default 8).
- Each model turn is a checkpointed stage (`<stage>_turn_<n>`), and each tool result is saved in the run state, so a
  resumed run never repeats a call.
- Calls within one turn run in order.
- Tool rounds count toward the call and cost budget.

**Approvals:** an `ask` tool pauses the run in `waiting` (the existing approval mechanism), with `kind: "mcp_tool"`,
the connector, the tool, and the arguments.
- **Allow once:** run it.
- **Always allow:** run it and set the tool to `allow`.
- **Deny:** the model receives "The user declined this call".
- A connector deleted while a run waits resolves its pending call as Deny.

**Events:** `connector_tool` events (connector, tool, status `done`, `denied` or `failed`, and a short result) appear in the
timeline.

## 5. Interface

- **Settings → Connectors:**
  - the list, with status, tool count, and an on/off switch;
  - Add (name, URL, sign-in: None, Token, or Sign in, with client ID and secret fields when needed);
  - per tool: Ask, Allow, or Deny, with a read-only tag, and "Allow all read-only";
  - Refresh tools, Sign in again, and Remove.
- **Composer:** a connectors button next to Search, with a per-chat switch for each connector, "Manage connectors",
  and a count badge.
- **Chat:**
  - an approval card (connector, tool, readable arguments, Allow once, Always allow, Deny);
  - tool rows in the timeline that expand to the arguments and result.

## 6. Errors

- A connector that fails during a run gives the model an error result; the timeline shows "failed" and the run carries
  on.
- An expired OAuth token is refreshed once. If that fails, the connector is marked "Sign in again" and the run
  continues without it, with a warning.
- A waiting run waits until the user answers or stops it.

## 7. Testing

- **MCP client** against a fake server on `httpx.MockTransport`: JSON and SSE responses, the session ID, paging, tool
  errors, and timeouts.
- **Connector OAuth:** the full flow with discovery and registration, refresh, the pasted-client fallback, and a
  `state` mismatch.
- **URL safety:** `http://`, private, and metadata addresses are refused unless allowed.
- **Orchestrator with fake tool-calling models:**
  - Quick calls a tool, and Deep gives tools only to the assigned task;
  - Allow once, Always allow, and Deny;
  - resuming after an approval without repeating calls, and the round limit;
  - denied tools are never offered, and review and repair never call tools;
  - a task with tools only goes to tool-capable models.
- **Agent mode:** refused without `LEO_SERVER_AGENT`; the existing agent tests still pass with it set.
- **Per-user isolation:** one user cannot see, use, change, or approve another user's connectors.
- **Browser:** the Connectors tab, the composer menu, and the approval card in both themes and at phone width, against
  a local fake MCP server.
