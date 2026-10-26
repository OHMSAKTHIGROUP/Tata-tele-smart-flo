# OHMSAKTHI Smartflo integration

Test backend connecting Tata Tele Smartflo Voice Streaming with OpenAI Realtime,
with authenticated call records and confirmed click-to-call endpoints.

- [Service health](https://ohmsakthi-smartflo-integration.onrender.com/healthz)
- [Setup and limitations](SETUP.md)
- [Custom GPT Action schema](openapi.json)
- [Automated tests](test_server.py)

**Status:** Backend prepared. Smartflo and OpenAI credentials still need secure
configuration; no phone numbers have been rerouted. Outbound calls default to off.
The free Render service is for supervised testing, not production phone traffic.

```sh
pip install -r requirements.txt
python -m unittest -v
python server.py
```

Keep all API keys in Render Environment, never this repository.
