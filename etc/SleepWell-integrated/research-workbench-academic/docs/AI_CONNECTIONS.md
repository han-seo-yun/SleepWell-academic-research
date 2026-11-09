# AI connections

`researchbench.ai_connections` is the bounded provider layer used by the report-agent harness. It supports OpenAI, Anthropic, and OpenAI-compatible HTTP APIs without adding an SDK dependency. Project files contain only non-secret settings; API keys are stored in the operating-system credential vault through the existing `OptionalAI` keyring helper.

## Public interface

- `provider_catalog()` returns the providers, default API base URLs, subscription flags, and official setup/help URLs.
- `load_settings(project_path)` and `save_settings(project_path, settings)` read and write `<project>/ai_connection.json`. Unknown fields are rejected on save, and credential-like fields are always rejected.
- `store_key(settings, key)`, `delete_key(settings)`, and `credential_status(settings)` manage or inspect credentials without returning a secret.
- `list_models(settings, cancel_event=None)` performs the provider's read-only `GET /models` request. A model can also be entered manually and is required before generation.
- `test_connection(settings, cancel_event=None)` performs only model listing for API providers or a read-only authentication-status check for subscription entries. It never generates content.
- `ProviderClient(...).complete(...)` performs one bounded generation request and returns `content`, `provider`, `model`, `usage`, `request_id`, `actual_llm`, `cost_known`, `cost_usd`, and `limit_status`. The latter distinguishes a provider-requested output-token limit from the ChatGPT-plan preview's time/byte-only local limits.

The default limits are six calls per client, 2,500 output tokens per call, a 90-second request timeout, and a 300-second total client lifetime. A USD budget is optional. Enabling it requires explicit input and output prices per million tokens; no current model price is guessed. If either token usage or explicit prices are unavailable, `cost_known` is false and `cost_usd` is null.

## Provider behavior

| Provider ID | Credential | Connection test | Generation |
| --- | --- | --- | --- |
| `openai` | API key in OS keyring | `GET https://api.openai.com/v1/models` | Chat Completions with `max_completion_tokens` and `store:false` |
| `anthropic` | API key in OS keyring | `GET https://api.anthropic.com/v1/models` | Messages API |
| `openai_compatible` | API key in OS keyring | `GET <base>/models` | `<base>/chat/completions` |
| `chatgpt_subscription` | App-owned Sign in with ChatGPT OAuth stored by `chatgpt_auth` | account-entitled visible slugs from `GET https://api.openai.com/v1/models` | streamed Responses API with `store:false` and no tools |
| `codex_subscription` | Existing Codex login, legacy status only | literal `codex login status` with API-key environment variables removed | Disabled; select `chatgpt_subscription` for the app-owned OAuth flow |
| `claude_subscription` | Not accepted by third-party apps | policy/status explanation only | Disabled; use an Anthropic API key |

No login command is launched automatically. In particular, the service does not read CLI authentication files, borrow another application's OAuth client identifier, copy tokens, or assume that an API call is covered by a consumer subscription.

OpenAI documents an app-owned Sign in with ChatGPT flow for local/open-source applications. The separate `chatgpt_auth` module owns PKCE, ID-token validation, host/client registration, token refresh, scope checking, and secure credential storage. This provider obtains an access token only immediately before a request and never persists or returns it. It discovers the selected account's current model catalog, retaining server-ordered entries whose `visibility` is `list`; it does not guess a model. Requests use the public Responses endpoint, convert system text to `instructions`, omit tools and all preview-unsupported fields, and succeed only after `response.completed`. The Codex CLI bridge remains status-only because `codex exec` is an agent surface and does not provide a certified text-only/no-local-tools execution mode.

The current ChatGPT-plan preview explicitly disallows the `max_output_tokens` field. The client therefore does not send that field for this provider; it still caps the received stream at 2 MB and applies its request/total time limits. API-key providers continue to send their supported output-token limit. API-style USD token prices and budgets are cleared for ChatGPT-plan settings because they are not an enforceable subscription billing cap.

Anthropic's official policy states that third-party developers must not offer Claude.ai login or route Free/Pro/Max/Ultra credentials on users' behalf. Therefore the installed `claude` executable is not used as a subscription bridge.

## Security and failure behavior

- Official OpenAI and Anthropic providers are pinned to their official HTTPS hosts.
- Compatible endpoints require HTTPS, except explicit loopback HTTP for a local server. URL credentials, fragments, sensitive query parameters, private/link-local literal addresses, preflight DNS resolutions to non-public addresses, and HTTP redirects are rejected. The standard-library request currently resolves a hostname again when connecting, so the preflight is not a DNS-rebinding-proof network sandbox; prefer the pinned official providers or an explicit loopback service for sensitive work.
- Requests and responses are JSON, and response bodies are capped at 2 MB.
- ChatGPT plan generation is an SSE stream capped at 2 MB. Failed, incomplete, malformed, interrupted, or unterminated streams are not treated as successful; usage is accepted only from `response.completed`.
- Provider errors are reduced to stable status codes. Bounded JSON error bodies may contribute allowlisted `code`, `param`, and request-ID fields for usage-limit recovery, but raw bodies, messages, URLs, API keys, subprocess output, and internal exception text are not returned.
- A failed generation is not automatically retried because doing so could duplicate cost or side effects.
- OpenAI responses ending because of the output-length bound or carrying a refusal are explicit safe failures; refusal text and partial output are not included in the error. OpenAI-compatible providers retain the broadly supported `max_tokens` request field.
- Cancellation is checked before and after network I/O and between SSE reads. SSE processing also uses a monotonic absolute deadline, so periodic events cannot extend the configured call time; processing stops immediately at `response.completed`. `urllib` cannot interrupt one in-flight blocking read, so the socket timeout remains the upper bound for that individual read.
- Subscription status subprocesses use literal argument arrays, `shell=False`, a hidden Windows process, a ten-second timeout, and an environment with API credential variables removed.

## Official references

- [OpenAI Models API](https://platform.openai.com/docs/api-reference/models/list)
- [OpenAI Chat Completions API](https://platform.openai.com/docs/api-reference/chat/create)
- [Sign in with ChatGPT for local/open-source applications](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [Anthropic Messages API](https://docs.anthropic.com/en/api/messages)
- [Anthropic Models API](https://docs.anthropic.com/en/api/models-list)
- [Anthropic authentication and credential-use policy](https://code.claude.com/docs/en/legal-and-compliance#authentication-and-credential-use)

## Testing

Run the dependency-free test module from the repository root:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_ai_connections -v
```

The tests use injected transports and mocks. They do not log in, contact a paid model, or store a real credential.
