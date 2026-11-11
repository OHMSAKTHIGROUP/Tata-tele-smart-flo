"""Direct Google Gemini Live audio adapter; no ElevenLabs or OpenAI calls."""
import asyncio
import base64
from contextlib import asynccontextmanager
import json
import re

from eleven_bridge import Codec

MODEL = re.compile(r'^(?:models/)?[A-Za-z0-9][A-Za-z0-9._-]{0,150}$')


def configured(key, model):
    return bool(key and MODEL.fullmatch(model))


def session_config(instructions, voice):
    return {
        'response_modalities': ['AUDIO'],
        'system_instruction': instructions,
        'speech_config': {'voice_config': {
            'prebuilt_voice_config': {'voice_name': voice},
        }},
    }


@asynccontextmanager
async def connect_session(key, model, config):
    # Explicit Developer API backend. Credentials never enter a URL or logs.
    from google import genai
    client = genai.Client(api_key=key, vertexai=False,
                          http_options={'api_version': 'v1beta'})
    async with client.aio as api:
        async with api.live.connect(model=model, config=config) as session:
            yield session


async def run(socket, stream, key, model, voice, instructions, event,
              session_factory=None):
    if not configured(key, model):
        raise ValueError('Gemini credentials and a Live model are required')
    factory = session_factory or connect_session
    context = factory(key, model, session_config(instructions, voice))
    # Setup may fail due to quota/model access; do not hold the phone indefinitely.
    async with asyncio.timeout(20):
        session = await context.__aenter__()
    try:
        event('gemini_session_ready')
        codec = Codec('pcm_16000', 'pcm_24000')
        state = {'tail': b'', 'chunk': 0, 'first': False}
        # Send once before consuming caller audio, preserving system instructions.
        await session.send_client_content(turns={
            'role': 'user', 'parts': [{'text':
                'Greet the caller briefly now. Follow your configured company, '
                'language and AI-disclosure instructions.'}],
        }, turn_complete=True)

        async def send_audio(data=b'', final=False):
            state['tail'] += data
            if final and len(state['tail']) % 160:
                state['tail'] += b'\xff' * (160 - len(state['tail']) % 160)
            while len(state['tail']) >= 160:
                size = min(len(state['tail']) // 160 * 160, 1600)
                payload, state['tail'] = state['tail'][:size], state['tail'][size:]
                state['chunk'] += 1
                await socket.send(json.dumps({
                    'event': 'media', 'streamSid': stream,
                    'media': {'chunk': state['chunk'],
                              'payload': base64.b64encode(payload).decode()},
                }))
                if not state['first']:
                    state['first'] = True
                    event('first_gemini_audio')

        async def caller():
            async for raw in socket:
                message = json.loads(raw)
                if message.get('streamSid') not in (None, stream):
                    raise ValueError('Mismatched stream')
                if message.get('event') == 'stop':
                    event('smartflo_stop_received')
                    return
                if message.get('event') == 'media':
                    data = base64.b64decode(
                        message.get('media', {}).get('payload', ''), validate=True)
                    if not data or len(data) > 16000:
                        raise ValueError('Invalid phone audio frame')
                    await session.send_realtime_input(audio={
                        'data': codec.to_agent(data),
                        'mime_type': 'audio/pcm;rate=16000',
                    })

        async def agent():
            # SDK receive() finishes at each turn boundary, not only call end.
            while True:
                received = False
                async for message in session.receive():
                    received = True
                    if message.go_away:
                        event('gemini_session_expiring')
                        raise RuntimeError('Gemini session is expiring')
                    if message.tool_call:
                        # No CRM/booking tools are configured by this voice adapter.
                        event('gemini_unconfigured_tool')
                        raise RuntimeError('Unconfigured voice tool')
                    content = message.server_content
                    if not content:
                        continue
                    if content.interrupted:
                        state['tail'] = b''
                        codec.output_state = None
                        await socket.send(json.dumps({'event': 'clear', 'streamSid': stream}))
                        event('gemini_interrupted')
                        continue
                    for part in (content.model_turn.parts or []) if content.model_turn else []:
                        blob = part.inline_data
                        if blob is None:
                            continue
                        if blob.mime_type not in ('audio/pcm;rate=24000', 'audio/pcm;rate=24000;channels=1'):
                            raise ValueError('Unexpected Gemini output audio format')
                        if not isinstance(blob.data, bytes) or len(blob.data) > 262144:
                            raise ValueError('Invalid Gemini audio frame')
                        await send_audio(codec.to_phone(blob.data))
                    if content.turn_complete:
                        await send_audio(final=True)
                        codec.output_state = None
                if not received:
                    # A closed iterator must never spin or reconnect automatically.
                    raise RuntimeError('Gemini stream ended')

        tasks = [asyncio.create_task(caller()), asyncio.create_task(agent())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await context.__aexit__(None, None, None)
