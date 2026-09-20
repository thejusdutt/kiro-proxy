# kiroproxy

One Python file. It speaks the Anthropic Messages API on localhost and forwards to
Kiro's `generateAssistantResponse`, so Claude Code runs on your Kiro subscription.

No venv, no dependencies, no Docker. Python 3.8+ and a `kiro login` that worked.

## Run

```
python kiroproxy.py            # 127.0.0.1:9100
python kiroproxy.py -v         # with request logging
claude-kiro.cmd                # starts the proxy if needed, then Claude Code
```

`claude-kiro.cmd` passes its arguments through, so `claude-kiro.cmd -p "hi"` works.

## How it gets credentials

Reads `%LOCALAPPDATA%\kiro-cli\data.sqlite3` — the database `kiro login` writes:

- `auth_kv` / `kirocli:odic:token` → access token, refresh token, expiry, region
- `auth_kv` / `kirocli:odic:device-registration` → OIDC client id and secret
- `state` / `api.codewhisperer.profile` → profile ARN

A token within 5 minutes of expiry gets refreshed against
`https://oidc.{region}.amazonaws.com/token` and written back to the same row. If
there's no client registration it falls back to the Kiro desktop auth endpoint.

Override the path with `KIRO_DB` if your install lives elsewhere.

## Models

Claude Code sends names like `claude-opus-5-20260115`. The proxy normalizes
those to what Kiro accepts (`claude-opus-5`), falls back to
`KIRO_DEFAULT_MODEL` for anything unrecognized, and never 400s on a name.

Default is `claude-opus-5`. Available on your account (probed live 2026-09-20):
`claude-opus-5`, `claude-sonnet-5`, `claude-opus-4.8/4.7/4.6/4.5`,
`claude-sonnet-4.6/4.5/4`, `claude-haiku-4.5`, `gpt-5.6-luna/sol/terra`, `glm-5`,
`minimax-m2.5/m2.1`, `deepseek-3.2`, `qwen3-coder-next`, `auto`. `GET /v1/models`
lists them.

There is no 1M-context model id on Kiro: `claude-opus-5[1m]`, `claude-opus-5-1m`
and similar all return `INVALID_MODEL_ID`. The proxy maps them to `claude-opus-5`
so nothing breaks. `claude-haiku-5` doesn't exist there either, so the small/fast
model stays `claude-haiku-4.5`.

Environment overrides: `KIRO_PORT`, `KIRO_DEFAULT_MODEL`, `KIRO_SMALL_MODEL`,
`KIRO_TIMEOUT`, `KIRO_MAX_PAYLOAD_BYTES`, `KIRO_DB`.

## What it deliberately does not do

The failure modes of the big gateway were the reason for this file, so:

- **Never rejects a request over shape.** Unknown content blocks get stringified
  instead of raising. `thinking` blocks are dropped, `cache_control` ignored,
  unknown roles become `user`. No 422 path exists.
- **No shared lock on the request path.** A valid cached token is returned with no
  locking at all. Only a real refresh serializes, and the second caller re-reads
  sqlite before refreshing again — so a slow refresh can't stall every request.
- **Long tool names survive.** Kiro caps names at 64 chars, which MCP names blow
  past. They're shortened with a hash suffix on the way out and restored on the
  way back, so Claude Code sees the name it sent.
- **History is always valid.** Messages are merged, forced to alternate, forced to
  start with `user`, and the oldest pairs get dropped when the payload exceeds
  600KB.
- **Binary frames are parsed properly** (`vnd.amazon.eventstream` prelude, headers,
  payload), with a JSON-scan fallback if the framing ever looks wrong.

## Endpoints

| Route | Behaviour |
| --- | --- |
| `POST /v1/messages` | streaming and non-streaming |
| `POST /v1/messages/count_tokens` | rough estimate, length/4 |
| `GET /v1/models` | model list |
| `GET /health` | liveness + region |

## Known limits

- Token counts are estimates. Kiro doesn't report real usage, so the context meter
  in Claude Code is approximate.
- Extended thinking isn't a Kiro feature; `thinking` requests are accepted and
  ignored rather than faked.
- Throttling and truncation come from Kiro. No proxy fixes those — if replies get
  cut off under load, that's upstream.
