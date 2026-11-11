"""One bounded Google audio-generation test. Never dials a telephone number."""
import argparse
import asyncio
import json
import time

import gemini_bridge
from server import Settings


async def probe(settings, session_factory=None):
    incoming = asyncio.Queue()
    events = set()
    started = time.monotonic()
    result = {'audio_received': False}

    class Phone:
        async def send(self, raw):
            message = json.loads(raw)
            if message.get('event') == 'media' and not result['audio_received']:
                result.update(audio_received=True,
                              first_audio_seconds=round(time.monotonic() - started, 3))
                await incoming.put(json.dumps({'event': 'stop'}))

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await incoming.get()

    async with asyncio.timeout(30):
        await gemini_bridge.run(Phone(), 'synthetic-no-phone', settings.gemini_key,
                                settings.gemini_model, settings.gemini_voice,
                                settings.instructions, events.add,
                                session_factory=session_factory)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--confirmed', action='store_true',
                        help='Confirm one potentially billable Google API audio test')
    args = parser.parse_args()
    if not args.confirmed:
        parser.error('--confirmed is required; no API request was made')
    try:
        result = asyncio.run(probe(Settings.from_env()))
    except Exception as exc:
        # Provider exception text can contain credentials or generated content.
        print(json.dumps({'audio_received': False, 'error_type': type(exc).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(result))


if __name__ == '__main__':
    main()
