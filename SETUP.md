# Smartflo / OpenAI integration

This repository contains a test backend, not a completed account connection.
It relays Smartflo Voice Streaming to OpenAI Realtime and exposes authenticated
call-record and click-to-call endpoints for a Custom GPT Action.
It does not attach a phone number to an existing ChatGPT sidebar chat.

## Run and verify

Python 3.12 or newer:

```
pip install -r requirements.txt
python -m unittest -v
python server.py
```

Render: Python runtime, build `pip install -r requirements.txt`, start
`python server.py`, health `/healthz`, Singapore region. One process only.
The first deployment can run without credentials; all sensitive endpoints fail
closed. A free service is for a supervised test only: cold starts can break calls.
Use an always-on paid plan before production, after cost approval.

## Secrets (Render Environment, never Git)

| Key | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | Voice API access; API billing must be enabled |
| `SMARTFLO_API_TOKEN` | Dedicated scoped Smartflo token for CDR and click-to-call |
| `ADMIN_API_KEY` | Random secret of at least 32 characters for management API |
| `SMARTFLO_STREAM_KEY` | Different random secret of at least 32 characters for voice WebSocket |

Optional non-secret settings: `OPENAI_REALTIME_MODEL` (default
`gpt-realtime-2.1`), `OPENAI_VOICE` (`marin`), `MAX_CALL_SECONDS` (`300`),
`MAX_CONCURRENT_CALLS` (`2`), `ASSISTANT_INSTRUCTIONS`.
`ENABLE_OUTBOUND_CALLS` defaults to `false`; enabling it permits billable calls
through the authenticated management endpoint. Do so only for approved testing.

## Smartflo setup — preserve existing bot

After backend credentials are securely configured, create a SEPARATE Voice
Streaming endpoint named `OHMSAKTHI OpenAI Test` with:

- Static URL: `wss://YOUR_SERVICE_HOST/voice/stream`
- Authentication header: `Authorization: Bearer <SMARTFLO_STREAM_KEY>`
- Expected media: mu-law, mono, 8000 Hz.

Confirm the account supports authentication headers on its streaming connection.
Never place tokens in query parameters. Test the endpoint first. Assign exactly
one explicitly chosen test DID only after the endpoint is verified. Preserve the
existing Expedify bot and all current number routes until that choice is made.
Configure a human failover destination before production.

An OpenAI API key and the streaming connection are sufficient for voice audio;
the Smartflo REST token is separately required for call logs and click-to-call.

## Management API / Custom GPT

Use `Authorization: Bearer <ADMIN_API_KEY>` on every `/api/*` request.

- `GET /api/status`: configuration readiness, never secret values.
- `GET /api/call-records?from_date=YYYY-MM-DD HH:MM:SS&to_date=...`: bounded
  call records; use Smartflo account time. Optional page, limit (1–100),
  call_type (`c`/`m`), direction, did_number filters. Sensitive recording URLs,
  contact details and caller numbers are deliberately excluded.
- `POST /api/calls`: agent_number, destination_number, optional caller_id,
  and `confirmed: true`. Requires a unique `Idempotency-Key` header (16–100
  letters/digits/hyphens). A 202 response means accepted, not connected.

Retry protection is in memory for one hour and only covers this single process.
Never automatically retry an uncertain call result. Check Smartflo before a new
request, particularly across restarts. Add durable idempotency storage before
production or scaling. No autonomous outbound campaigns are implemented.

Import `openapi.json` into a Custom GPT Action and replace its server URL with
the deployed HTTPS host. Configure the management secret in the GPT Action's
secure Bearer API-key authentication settings. Call initiation is marked
consequential so the GPT requests confirmation. The current chat has no direct
Smartflo connector until this separate connection is configured.

## Test scope and limitations

Automated tests use synthetic audio and mock provider responses; they never
make real calls. They cover authentication, disabled endpoints, media format,
bidirectional conversion, interruptions, retry protection and response redaction.
Live Tata/OpenAI interoperability, model entitlement, caller audio quality,
Tamil performance and failover require an actual supervised test.

No transcripts, recordings, leads or company/number mappings are stored here.
Number/company/project assignment and persistent summaries are the next phase.
Provider-side retention follows each configured provider's policies. This test
backend identifies itself as AI and has no booking or transfer tools.

Official references:
- https://docs.smartflo.tatatelebusiness.com/docs/bi-directional-audio-streaming-integration-document
- https://docs.smartflo.tatatelebusiness.com/reference/v1callrecords
- https://docs.smartflo.tatatelebusiness.com/reference/v1click_to_call-1
- https://developers.openai.com/api/docs/guides/realtime-conversations
- https://render.com/docs/web-services
