# OHMSAKTHI Smartflo integration

Single-process backend connecting Tata Tele Smartflo Voice Streaming with a
selectable OpenAI Realtime, ElevenLabs, or direct Gemini Live voice provider.
It includes authenticated call records and supervised outbound calling.

- [Service health](https://ohmsakthi-smartflo-integration.onrender.com/healthz)
- [Setup and limitations](SETUP.md)
- [Direct Gemini Live setup (pending live validation)](GEMINI_SETUP.md)
- [Custom GPT Action schema](openapi.json)
- [Automated tests](test_server.py)

**Gemini status:** Adapter and mock tests prepared; Google credentials, model
access and live audio still need verification before selecting Gemini on Render.
Actual provider selection is controlled by the deployed environment.
Outbound calls default to off on the legacy management endpoint.
The free Render service is for supervised testing, not production phone traffic.

```sh
pip install -r requirements.txt
python -m unittest -v
python server.py
```

Keep all API keys in Render Environment, never this repository.
