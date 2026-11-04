# Accounts: OAuth sign-in, redesigned auth pages, per-user model accounts

**Date:** 4 November 2026  
**Status:** approved in conversation; implementation follows directly (the user asked to skip the separate plan step)  
**Part of:** L.E.O. becomes two products on one account system:

1. **Accounts** (this spec): sign-in, sign-up, and each user's saved model connections.
2. **L.E.O Chat**: the web app as a chatbot only (agent mode leaves the composer). Separate spec.
3. **L.E.O CLI**: a terminal agent working on the local machine, signing in to this account and using its saved
   models. Separate spec.

## 1. Goals

- Sign in and sign up with Google, GitHub, or any OpenID Connect provider. Username and password stay alongside them.
- Only emails or domains on an allowlist may join.
- Each user's API keys and 9Router model accounts belong to that user, survive logout, and are isolated from other
  users.
- Better sign-in and register pages (split-screen layout B), plus a Settings → Account tab.

Out of scope: Chat/CLI split, CLI login (device flow), per-user preferences, email/password reset by mail.

## 2. Configuration (`.env`)

| Variable | Meaning |
|---|---|
| `LEO_PUBLIC_URL` | Base for callback URLs, e.g. `https://leo.example.com`. Empty: taken from the request. |
| `LEO_ALLOWED_EMAILS` | Comma list of exact emails and `@domain` entries. |
| `LEO_GOOGLE_CLIENT_ID`, `LEO_GOOGLE_CLIENT_SECRET` | Google OAuth app. |
| `LEO_GITHUB_CLIENT_ID`, `LEO_GITHUB_CLIENT_SECRET` | GitHub OAuth app. |
| `LEO_OIDC_ISSUER`, `LEO_OIDC_CLIENT_ID`, `LEO_OIDC_CLIENT_SECRET`, `LEO_OIDC_NAME` | Generic OIDC (endpoints from the issuer's discovery document); the name labels the button. |

A provider appears only when its ID and secret are set. Callback URL to register with each provider:
`<LEO_PUBLIC_URL>/api/auth/oauth/<google|github|oidc>/callback`.

## 3. OAuth flow

1. `GET /api/auth/oauth/{provider}/start?next=/` creates `state` and a PKCE verifier (S256). They, `next`, and (when
   linking) the signed-in user's ID go into a `leo_oauth` cookie encrypted with the store's Fernet key: HttpOnly,
   `SameSite=Lax`, 10 minutes. No server-side state, so it works across API processes. Then it redirects.
2. `GET /api/auth/oauth/{provider}/callback` checks `state`, clears the cookie, exchanges the code, and reads the
   identity from the provider: Google/OIDC userinfo, or GitHub `/user` plus `/user/emails`. Only verified emails count;
   OIDC needs `email_verified: true`. Provider tokens are not stored.
3. Who may sign in:
   - If `LEO_ALLOWED_EMAILS` is set, every OAuth sign-in (not only the first) must match it.
   - If it is empty, OAuth signs in accounts that already exist and creates only the very first user, and that only
     when `LEO_API_TOKEN` is unset (the same rule password registration uses).
4. Finding the user, in order:
   - an identity with this provider and subject;
   - a user whose email equals this verified email (the identity is linked);
   - otherwise, a new user.
   A user's `email` is only ever set from a verified OAuth email, so password-only accounts are never taken over.
5. New users get a username from the GitHub login or the email's local part, made unique with a number. The first user
   in the database becomes admin, and rows saved before accounts existed are adopted, as with password registration.
6. The session is the existing `leo_session` cookie, which stays `SameSite=Strict`. Because the browser would not send
   it on the redirect chain coming back from the provider, the callback answers with a small HTML page that navigates
   to `next` itself instead of a 302.
7. Errors return to `/login?error=not_invited|cancelled|failed|linked_elsewhere` (plus `email=` for `not_invited`).
   Failures count toward the existing sign-in throttle.
8. `next` must be a path starting with a single `/`.

Linking from Settings uses the same routes with `?link=1`. The callback attaches the identity to the signed-in user,
or fails with `linked_elsewhere` if another user already has it.

## 4. Data

- New table `identities`: `id`, `user_id`, `provider`, `subject`, `email`, `created_at`; unique `(provider, subject)`.
- `users` gains nullable `email` (unique index), `display_name`, `avatar_url` via the existing `_migrate` helper.
- OAuth-only users store `password_hash = ""`. SQLite cannot drop `NOT NULL` in place, and bcrypt rejects an empty
  hash, so password sign-in simply fails for them.
- New table `routers`: `user_id` (key), `url`, `secret` (Fernet-encrypted router password), `created_at`.

## 5. Per-user 9Router

- sandboxd gains `PUT /routers/{user_id}` and `DELETE /routers/{user_id}`. PUT creates or starts the container
  `leo-router-<user_id>` from `LEO_ROUTER_IMAGE` (default the pinned `decolua/9router:0.5.112`): its own volume,
  `INITIAL_PASSWORD` from the request, the `leo-routers` network, memory/CPU/process limits, `no-new-privileges`, no host
  port, `restart: unless-stopped`, and a label so sandboxd manages only its own routers. All capabilities are dropped
  except `CHOWN`, `SETUID` and `SETGID`: the image's entrypoint chowns the volume, then drops to the `node` user with
  `su-exec`. sandboxd pulls the image the first time it is missing. DELETE removes the container
  and keeps the volume.
- The API creates a user's router on their first model-account sign-in. It generates the password, stores it in
  `routers`, and waits (bounded) for `/api/health`.
- Everything that assumed the shared router becomes per user: account list, sign-in sessions (which already carry the
  user), the Codex callback on port 1455, key creation, model discovery, and disconnect. A user's 9Router
  connections use `<their router>/v1`.
- Signing in to a model account no longer needs admin rights.
- When a user disconnects their last account, their router container is removed. Its volume and password stay, so
  signing in again reuses them.
- Compose: the shared `9router` service is removed. `api` and `worker` join a new `leo-routers` network. Accounts
  signed in on the old shared router must be signed in again.
- Without sandboxd (`LEO_SANDBOX_URL` unset), model-account sign-in is off with a clear message; API keys work as
  before.

## 6. API

- `GET /api/auth/providers` (public): `[{id, name}]` and whether password registration is open.
- `GET /api/auth/me` adds `email`, `display_name`, `avatar_url`, `identities: [provider]`, `has_password`.
- `DELETE /api/auth/identities/{provider}`: refused if it would leave no way to sign in.
- `PUT /api/auth/password` `{current_password?, new_password}`: `current_password` is required when one exists.

## 7. Pages

- `/login` and `/register` render one split-screen page (layout B); `/register` opens on the Create account tab.
- **Left panel:** the L.E.O. pitch plus L.E.O Chat and L.E.O CLI cards. It is hidden below 768px.
- **Right panel, Sign in tab:**
  - a provider button for each configured provider, with "Last used" remembered in `localStorage`;
  - "or with a username", then the form, with a Show toggle on the password and a Caps Lock warning;
  - a loading state on submit, inline errors, and an error banner driven by `?error=`.
- **Right panel, Create account tab:**
  - the same provider buttons;
  - the password form with live rules when password registration is open, otherwise a note pointing to the admin or
    the providers.
- Both themes use the existing `data-theme` tokens. The pre-existing `astro check` errors in these pages are fixed.
- **Settings → Account tab:**
  - profile (avatar, name, email);
  - sign-in methods with Link and Unlink;
  - change or set password;
  - "Your model accounts" (the 9Router sign-in section, moved here and now per user).

## 8. Testing

- **Unit tests, with providers faked on `httpx.MockTransport`:**
  - Google, GitHub (including an email that is only in `/user/emails`) and OIDC (including an unverified email);
  - the allowlist, including emptying it and the first-user rule;
  - linking by verified email, a `state` mismatch, an unsafe `next`, link and unlink, and setting a password.
- **sandboxd routers API:** against the existing fake Docker.
- **Per-user isolation:** one user cannot list, use, or remove another user's model accounts.
- **Real Docker:** a router container starts and answers, and is removed with its owner's last account.
- **Browser:** both tabs, light and dark, phone and desktop widths, and the error banners. A real Google/GitHub sign-in
  can only be tested once the user registers OAuth apps.
