"""Authenticated ElevenLabs adapter. Never logs credentials, audio or transcripts."""
import asyncio
import audioop
import base64
import json
import urllib.parse
import urllib.request
import urllib.error

AGENT_API = 'https://api.elevenlabs.io/v1/convai/conversation/get-signed-url'


def signed_url(key, agent_id):
    request = urllib.request.Request(AGENT_API + '?' + urllib.parse.urlencode({'agent_id': agent_id}),
                                    headers={'xi-api-key': key, 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=15) as result:
            url = json.load(result)['signed_url']
    except urllib.error.HTTPError as exc:
        # Expose only HTTP status and an allowlisted classification, never body text.
        reason = 'provider_rejection'
        try:
            status = json.loads(exc.read(65536)).get('detail', {}).get('status')
            if status in {'missing_permissions', 'invalid_api_key', 'quota_exceeded',
                          'insufficient_credits', 'agent_not_found', 'not_found', 'invalid_authorization_header'}:
                reason = status
        except (ValueError, AttributeError, TypeError):
            pass
        raise type('ElevenHTTP' + str(exc.code) + '_' + reason, (RuntimeError,), {})() from None
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != 'wss' or parsed.hostname != 'api.elevenlabs.io' or parsed.port not in (None, 443):
        raise ValueError('Unexpected voice endpoint')
    return url


class Codec:
    def __init__(self, input_format, output_format):
        self.input_rate = self.rate(input_format)
        self.output_rate = self.rate(output_format)
        self.input_state = self.output_state = None

    @staticmethod
    def rate(fmt):
        if fmt == 'ulaw_8000':
            return None
        if fmt in ('pcm_8000', 'pcm_16000', 'pcm_22050', 'pcm_24000', 'pcm_44100', 'pcm_48000'):
            return int(fmt.split('_')[1])
        raise ValueError('Unsupported voice audio format')

    def to_agent(self, data):
        if self.input_rate is None:
            return data
        pcm = audioop.ulaw2lin(data, 2)
        if self.input_rate != 8000:
            pcm, self.input_state = audioop.ratecv(pcm, 2, 1, 8000, self.input_rate, self.input_state)
        return pcm

    def to_phone(self, data):
        if self.output_rate is None:
            return data
        if len(data) % 2:
            raise ValueError('Invalid PCM audio frame')
        if self.output_rate != 8000:
            data, self.output_state = audioop.ratecv(data, 2, 1, self.output_rate, 8000, self.output_state)
        return audioop.lin2ulaw(data, 2)


async def run(socket, stream, key, agent_id, connector, event):
    url = await asyncio.to_thread(signed_url, key, agent_id)
    async with connector(url, max_size=1048576, open_timeout=15) as ai:
        event('elevenlabs_connected')
        await ai.send(json.dumps({'type': 'conversation_initiation_client_data'}))
        ready = asyncio.Event()
        state = {'codec': None, 'tail': b'', 'chunk': 0, 'first': False, 'interrupted': -1}

        async def caller():
            async with asyncio.timeout(15):
                await ready.wait()
            async for raw in socket:
                message = json.loads(raw)
                if message.get('streamSid') not in (None, stream):
                    raise ValueError('Mismatched stream')
                if message.get('event') == 'stop':
                    event('smartflo_stop_received')
                    return
                if message.get('event') == 'media':
                    data = base64.b64decode(message.get('media', {}).get('payload', ''), validate=True)
                    if not data or len(data) > 16000:
                        raise ValueError('Invalid phone audio frame')
                    encoded = base64.b64encode(state['codec'].to_agent(data)).decode()
                    await ai.send(json.dumps({'user_audio_chunk': encoded}))

        async def agent():
            async for raw in ai:
                message = json.loads(raw)
                kind = message.get('type')
                if kind == 'conversation_initiation_metadata':
                    metadata = message['conversation_initiation_metadata_event']
                    state['codec'] = Codec(metadata['user_input_audio_format'], metadata['agent_output_audio_format'])
                    event('elevenlabs_session_ready')
                    ready.set()
                elif kind == 'ping':
                    await ai.send(json.dumps({'type': 'pong', 'event_id': message['ping_event']['event_id']}))
                elif kind == 'interruption':
                    state['interrupted'] = message.get('interruption_event', {}).get('event_id', -1)
                    state['tail'] = b''
                    await socket.send(json.dumps({'event': 'clear', 'streamSid': stream}))
                elif kind == 'audio':
                    if not ready.is_set():
                        raise ValueError('Audio arrived before format negotiation')
                    audio = message['audio_event']
                    if audio.get('event_id', 0) <= state['interrupted']:
                        continue
                    data = base64.b64decode(audio['audio_base_64'], validate=True)
                    state['tail'] += state['codec'].to_phone(data)
                    # Smartflo accepts complete 20 ms mulaw frames, buffered for playback.
                    length = len(state['tail']) // 160 * 160
                    if length:
                        payload, state['tail'] = state['tail'][:length], state['tail'][length:]
                        state['chunk'] += 1
                        await socket.send(json.dumps({'event': 'media', 'streamSid': stream, 'media': {
                            'chunk': state['chunk'], 'payload': base64.b64encode(payload).decode()}}))
                        if not state['first']:
                            state['first'] = True
                            event('first_elevenlabs_audio')
                elif kind in ('client_error', 'error'):
                    event('elevenlabs_provider_error')
                    raise RuntimeError('Voice provider error')
                elif kind == 'client_tool_call':
                    call = message['client_tool_call']
                    if call.get('expects_response'):
                        await ai.send(json.dumps({'type': 'client_tool_result', 'tool_call_id': call['tool_call_id'],
                                                  'is_error': True, 'result': 'This tool is not configured.'}))

        tasks = [asyncio.create_task(caller()), asyncio.create_task(agent())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def probe(settings, connector):
    """Bounded synthetic audio test: does not dial or include customer data."""
    queue = asyncio.Queue()
    reached = set()

    class SyntheticPhone:
        async def send(self, raw):
            message = json.loads(raw)
            if message.get('event') == 'media':
                reached.add('audio')
                await queue.put(json.dumps({'event': 'stop'}))

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await queue.get()

    async with asyncio.timeout(30):
        await run(SyntheticPhone(), 'synthetic-probe', settings.elevenlabs_key,
                  settings.elevenlabs_agent, connector, reached.add)
    return 'audio' in reached
