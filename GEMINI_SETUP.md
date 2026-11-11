# Direct Gemini Live voice — prepared, not activated

Flow: Tata Smartflo Voice Streaming → this bridge → Google Gemini Live → Smartflo.
The Gemini route never connects to ElevenLabs or OpenAI. Google handles the live
audio conversation. Smartflo still owns telephone numbers and call routing.

## Configuration

Keep secrets only in the server's environment or a secure secret manager:

| Variable | Value/purpose |
| --- | --- |
| `GEMINI_API_KEY` | Google Gemini Developer API key; never put it in Git or chat |
| `GEMINI_LIVE_MODEL` | Exact Live audio model ID available to that Google project |
| `GEMINI_VOICE` | Default `Aoede`; verify voice availability and Tamil quality live |
| `ASSISTANT_INSTRUCTIONS` | Company identity, AI disclosure, language and approved knowledge |
| `VOICE_PROVIDER` | Set to `gemini` only after a successful synthetic test |

`GEMINI_LIVE_MODEL` intentionally has no hard-coded preview default. A text-only
Gemini Flash model is not sufficient. Confirm current Live model access in
Google AI Studio before activation. The bridge uses Google's official Python
SDK pinned to 2.34.0 and the Developer API v1beta endpoint.

The existing Smartflo stream URL, bearer authentication, call limits and
concurrency limit remain in force. The bridge decodes phone mu-law 8 kHz to
16-bit mono PCM 16 kHz, then resamples Gemini PCM 24 kHz output back to phone
mu-law 8 kHz. Gemini interruption signals clear Smartflo's playback buffer.

## Verify before switching live calls

1. Install dependencies and run `python -m unittest -v`.
2. Configure the Gemini key and Live model securely in an isolated test runner.
   Retain the existing live provider selection during testing.
3. Run `python gemini_probe.py --confirmed`. This makes one bounded, potentially
   billable Google API greeting request, but no telephone call. Success reports
   `audio_received: true` and time to first phone-format audio. No content is logged.
4. Deploy the reviewed Gemini-capable code. Use an idle maintenance window when
   changing environment variables on the live Render service because changes can
   restart it. Select `VOICE_PROVIDER=gemini` only after the probe succeeds.
5. Check authenticated `/api/status`: `voice_provider=gemini`,
   `voice_configured=true`, `gemini_configured=true`, and `active_calls=0`.
6. Make one separately authorised outbound test and one inbound test. Confirm
   Tamil and English audio, short responses, interruption and call duration.
   API acceptance alone is not a completed phone call.

Rollback: select the previous provider and retain its existing secrets/agent.
There is no automatic switch to another paid provider or automatic redial.

## Scope and limits

Mock tests verify conversion, setup, multiple conversation turns, interruption,
partial-frame padding, invalid input, cleanup and routing without OpenAI keys.
They do not establish Google quota/model entitlement or live interoperability.
This is a single-provider receptionist bridge, not the planned multi-company
dashboard. It does not sync ChatGPT projects or perform CRM, booking or human
transfer actions. These integrations require separate work.

Google session-expiry (`go_away`) currently ends the stream; resumption is not
implemented. The existing maximum call duration is still enforced. Verify the
chosen model's live session limits before production. The existing free Render
service can sleep; paid hosting has not been provisioned by this change.

Official implementation references:
- https://github.com/googleapis/python-genai/blob/main/google/genai/live.py
- https://github.com/googleapis/python-genai/blob/main/google/genai/_live_converters.py
- https://ai.google.dev/gemini-api/docs/live
- https://ai.google.dev/gemini-api/docs/live-guide
