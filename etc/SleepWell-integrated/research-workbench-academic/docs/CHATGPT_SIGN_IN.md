# ChatGPT plan sign-in (research-only report synthesis)

SleepWell can optionally use an eligible user's ChatGPT Plus or Pro plan for
bounded report synthesis. This is an alternative to an OpenAI API key, not a
way to read or reuse a Codex login. It is available only after the user selects
**Continue with ChatGPT** and completes the official browser consent flow.

## What the application does

- Creates one opaque `urn:uuid:` host ID per local installation.
- Uses the official open-source dynamic registration entry point
  `dynamic_agent_client`; it never borrows a Codex client ID.
- Opens a local `127.0.0.1` callback, with state, nonce, and PKCE S256.
- Requests `openid profile email offline_access resource.invoke
  chatgpt.tokens.use.direct` for `https://api.openai.com/v1`.
- Verifies the OpenAI ID-token signature against the official JWKS, plus its
  issuer, audience, expiration, nonce, and subject before accepting a session.
- Stores registration metadata under the local SleepWell application-data
  directory and stores access, refresh, and ID tokens only in the OS keyring.
- Splits the token bundle into bounded ASCII keyring chunks. A short root
  manifest contains only a version, chunk count, random bundle identifier, and
  SHA-256 integrity hash; no plaintext token is written to a file. New chunks
  are written before the root is replaced, so a failed write leaves the prior
  root bundle usable.
- Refreshes a token only when necessary; sign-out attempts refresh-token
  revocation, then clears local keyring material even if revocation is offline.
- Retains the selected registration and its opaque verified account/client
  binding after sign-out. A normal reconnect must validate to that same binding
  before tokens can be stored. Account switching requires an explicit local
  selection or an explicit new-account action; it never silently overwrites a
  registration with another account's tokens.

The provider receives an access token only in memory. It must send report
evidence directly to `POST https://api.openai.com/v1/responses` with
`store:false`, `stream:true`, and no tools, MCP servers, filesystem access, or
shell commands. The caller must consume the stream through `response.completed`
and must validate structured output locally before producing a PDF.

## Limits and user-visible failures

Identity sign-in alone is insufficient. The connection is usable only when the
returned scope includes `chatgpt.tokens.use.direct`. A declined scope,
ineligible plan/workspace, usage limit, token failure, cancellation, timeout,
or failed signature validation leaves the model disabled. The application must
offer the user's own API key as a separate billing path and must not silently
switch billing. A verified identity and its granted scopes are retained even
when direct plan use is declined; `credential_status()` then reports
`plan_scope_granted: false` and `connected: false`.

The connection is for this local research application. It does not expose
ChatGPT conversations, an OpenAI API key, or any existing Codex credentials.

## Local auth API

`credential_status()` returns connection metadata only, never credentials.
`login(cancel_event=None, on_progress=None, *, new_account=False)` opens the
official browser flow only after an explicit user action. `new_account=True` is
the deliberate account-switch route; ordinary sign-in reuses the selected
issued client ID. `select_profile(profile_id)` is local-only and changes the
selected retained registration without network activity. `get_access_token()`
is provider-internal and must not be exposed to the UI or reports. `logout()`
returns a non-secret revocation result and warning; its local keyring cleanup
still succeeds when online revocation fails.

## Windows credential-store smoke check

Automated tests use an in-memory keyring double and never touch a user's vault.
For a release candidate, run one local, keyless smoke check with a randomly
named **synthetic** service and profile: write a 12 KB synthetic token payload
through `_tokens_set`, read it through `_tokens_get`, and delete it through
`_tokens_delete` in `finally`. The command must print only a pass/fail marker,
never a service name, profile name, or payload.

2026-11-05 evidence: native Windows Credential Manager roundtrip for a
synthetic 12 KB bundle printed `WINDOWS_KEYRING_CHUNK_SMOKE_PASS`; the random
test service/profile were deleted in the same process. This check does not
inspect, modify, or authenticate any user ChatGPT or API-key credential.

## Official sources

- [Registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [Profiles and sessions](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- [SIWC errors and recovery](https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery)
- [OpenAI OIDC discovery and token validation](https://developers.openai.com/siwc/website)
- [Responses / Codex app-server completion contract](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server)
