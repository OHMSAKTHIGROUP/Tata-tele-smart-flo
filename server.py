"""Single-process Smartflo / OpenAI test bridge. Never logs audio or credentials."""
import asyncio
import base64
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from aiohttp import web, WSMsgType
from websockets.asyncio.client import connect

SMARTFLO = 'https://api-smartflo.tatateleservices.com'
PHONE = re.compile(r'^\+?[0-9]{10,15}$')
DATE = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')
BASE_INSTRUCTIONS = (
    'You are the OHMSAKTHI GROUP AI test receptionist. Identify yourself as an AI assistant. '
    'Speak clearly and briefly in English or Tamil, following the caller. '
    'Ask which company or project they need and collect the enquiry. '
    'Do not invent prices, availability, bookings, project facts or completed actions. '
    'You cannot make bookings, transfer calls or access existing ChatGPT chats. '
    'Explain these limits when relevant. Do not request payment or authentication secrets.'
)


@dataclass
class Settings:
    openai_key: str = ''
    smartflo_token: str = ''
    admin_key: str = ''
    stream_key: str = ''
    model: str = 'gpt-realtime-2.1'
    voice: str = 'marin'
    outbound_enabled: bool = False
    max_seconds: int = 300
    max_connections: int = 2
    instructions: str = BASE_INSTRUCTIONS

    @classmethod
    def from_env(cls):
        return cls(
            openai_key=os.getenv('OPENAI_API_KEY', ''),
            smartflo_token=os.getenv('SMARTFLO_API_TOKEN', ''),
            admin_key=os.getenv('ADMIN_API_KEY', ''),
            stream_key=os.getenv('SMARTFLO_STREAM_KEY', ''),
            model=os.getenv('OPENAI_REALTIME_MODEL', 'gpt-realtime-2.1'),
            voice=os.getenv('OPENAI_VOICE', 'marin'),
            outbound_enabled=os.getenv('ENABLE_OUTBOUND_CALLS', 'false').lower() == 'true',
            max_seconds=max(10, min(900, int(os.getenv('MAX_CALL_SECONDS', '300')))),
            max_connections=max(1, min(10, int(os.getenv('MAX_CONCURRENT_CALLS', '2')))),
            instructions=os.getenv('ASSISTANT_INSTRUCTIONS', BASE_INSTRUCTIONS),
        )


def authorized(headers, key):
    value = headers.get('Authorization', '')
    return bool(len(key) >= 32 and hmac.compare_digest(value, 'Bearer ' + key))


def response(status, data):
    return web.json_response(data, status=status, headers={
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
    })


def provider_request(path, token, *, params=None, payload=None):
    """Fixed provider origin and allowlisted paths; tokens never leave the backend."""
    if path not in ('/v1/call/records', '/v1/click_to_call'):
        raise ValueError('Unsupported operation')
    url = SMARTFLO + path + ('?' + urllib.parse.urlencode(params) if params else '')
    auth = token if token.lower().startswith('bearer ') else 'Bearer ' + token
    request = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None,
        headers={'Authorization': auth, 'Accept': 'application/json', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=15) as result:
        return json.load(result)


class Bridge:
    def __init__(self, settings, connector=None):
        self.settings = settings
        self.active = 0
        self.connector = connector or connect
        self.call_requests = {}  # Single-process retry protection, expires after one hour.

    async def process_request(self, request):
        path = request.path
        if path == '/voice/stream':
            if not authorized(request.headers, self.settings.stream_key):
                return response(401, {'error': 'Unauthorized'})
            if not self.settings.openai_key:
                return response(503, {'error': 'Voice credentials are not configured'})
            # Capacity is checked again synchronously at handler entry.
            if self.active >= self.settings.max_connections:
                return response(503, {'error': 'Call capacity reached'})
            raw = web.WebSocketResponse(max_msg_size=65536, heartbeat=20)
            await raw.prepare(request)
            await self.handle(Socket(raw))
            await raw.close()
            return raw
        if request.headers.get('Upgrade', '').lower() == 'websocket':
            return response(404, {'error': 'Unknown stream'})
        if path in ('/', '/healthz'):
            return response(200, {'service': 'ohmsakthi-smartflo', 'status': 'running'})
        if not path.startswith('/api/'):
            return response(404, {'error': 'Not found'})
        if not authorized(request.headers, self.settings.admin_key):
            return response(401, {'error': 'Unauthorized'})
        if path == '/api/status':
            return response(200, {
                'openai_configured': bool(self.settings.openai_key),
                'smartflo_configured': bool(self.settings.smartflo_token),
                'voice_auth_configured': len(self.settings.stream_key) >= 32,
                'active_calls': self.active,
                'number_assignments_configured': False,
                'outbound_calls_enabled': self.settings.outbound_enabled,
            })
        if path == '/api/calls' and request.method == 'POST':
            if not self.settings.outbound_enabled or not self.settings.smartflo_token:
                return response(503, {'error': 'Outbound calls are disabled or credentials are missing'})
            try:
                body = await request.json()
            except Exception:
                return response(400, {'error': 'Invalid JSON'})
            if not isinstance(body, dict) or body.get('confirmed') is not True:
                return response(400, {'error': 'Explicit caller confirmation is required'})
            if not all(isinstance(body.get(k), str) and PHONE.fullmatch(body[k]) for k in ('agent_number', 'destination_number')):
                return response(400, {'error': 'Invalid agent or destination number'})
            if body.get('caller_id') and not PHONE.fullmatch(str(body['caller_id'])):
                return response(400, {'error': 'Invalid caller ID'})
            key = request.headers.get('Idempotency-Key', '')
            if not re.fullmatch(r'[A-Za-z0-9-]{16,100}', key):
                return response(400, {'error': 'Supply a unique Idempotency-Key (16–100 letters, digits or hyphens)'})
            payload = {k: body[k] for k in ('agent_number', 'destination_number', 'caller_id') if body.get(k)}
            payload.update({'async': 1, 'call_timeout': self.settings.max_seconds})
            now = time.monotonic()
            self.call_requests = {k: v for k, v in self.call_requests.items() if now - v['time'] < 3600}
            if key in self.call_requests:
                old = self.call_requests[key]
                if old['payload'] != payload:
                    return response(409, {'error': 'Idempotency key already used with different call details'})
                return response(old['status'], old['result'])
            if len(self.call_requests) >= 1000:
                return response(429, {'error': 'Test call request capacity reached'})
            entry = {'time': now, 'payload': payload, 'status': 409,
                     'result': {'error': 'Call request in progress; do not redial'}}
            self.call_requests[key] = entry
            try:
                data = await asyncio.to_thread(provider_request, '/v1/click_to_call', self.settings.smartflo_token, payload=payload)
                result = {k: data.get(k) for k in ('success', 'ref_id')}
                result['accepted_only'] = data.get('success') is True
                entry.update(status=202 if result['accepted_only'] else 400, result=result)
            except Exception:
                entry.update(status=502, result={'error': 'Call result unknown; check Smartflo before attempting another call'})
            return response(entry['status'], entry['result'])
        if path == '/api/call-records':
            if not self.settings.smartflo_token:
                return response(503, {'error': 'Smartflo credentials are not configured'})
            supplied = urllib.parse.parse_qs(request.query_string)
            allowed = {'from_date', 'to_date', 'page', 'limit', 'call_type', 'direction', 'did_number'}
            if any(k not in allowed or len(v) != 1 for k, v in supplied.items()):
                return response(400, {'error': 'Unsupported or duplicate filter'})
            params = {k: v[0] for k, v in supplied.items()}
            if not all(DATE.fullmatch(params.get(k, '')) for k in ('from_date', 'to_date')):
                return response(400, {'error': 'Supply from_date and to_date as YYYY-MM-DD HH:MM:SS (Smartflo account time)'})
            if params['from_date'] > params['to_date']:
                return response(400, {'error': 'Invalid date range'})
            if not params.get('limit', '20').isdigit() or not 1 <= int(params.get('limit', '20')) <= 100:
                return response(400, {'error': 'limit must be 1–100'})
            if not params.get('page', '1').isdigit() or int(params.get('page', '1')) < 1:
                return response(400, {'error': 'page must be positive'})
            if 'call_type' in params and params['call_type'] not in ('c', 'm'):
                return response(400, {'error': 'call_type must be c or m'})
            if 'direction' in params and params['direction'] not in ('inbound', 'outbound'):
                return response(400, {'error': 'Invalid direction'})
            if 'did_number' in params and not PHONE.fullmatch(params['did_number']):
                return response(400, {'error': 'Invalid DID'})
            params.setdefault('limit', '20')
            params.setdefault('page', '1')
            try:
                data = await asyncio.to_thread(provider_request, '/v1/call/records', self.settings.smartflo_token, params=params)
                # Deliberately omit recording URLs, contact data, notes, and credentials.
                fields = ('call_id', 'direction', 'status', 'date', 'time', 'call_duration',
                          'answered_seconds', 'agent_name', 'did_number')
                return response(200, {'count': data.get('count'), 'page': data.get('page'),
                    'results': [{k: row.get(k) for k in fields} for row in data.get('results', [])]})
            except Exception:
                return response(502, {'error': 'Smartflo request failed; check token permissions and provider availability'})
        return response(404, {'error': 'Not found'})

    async def handle(self, socket):
        if self.active >= self.settings.max_connections:
            await socket.close(1013, 'Call capacity reached')
            return
        self.active += 1
        try:
            async with asyncio.timeout(self.settings.max_seconds):
                await self._call(socket)
        except TimeoutError:
            await socket.close(1000, 'Test call time limit reached')
        except Exception:
            # Do not include exception text: provider failures may contain credential URLs.
            await socket.close(1011, 'Voice connection ended')
        finally:
            self.active -= 1

    async def _call(self, socket):
        # OpenAI is contacted only after Smartflo sends a valid start message.
        async with asyncio.timeout(10):
            async for raw in socket:
                message = json.loads(raw)
                if message.get('event') == 'start':
                    start = message.get('start', {})
                    break
                if message.get('event') != 'connected':
                    await socket.close(1008, 'Expected stream start')
                    return
            else:
                return
        stream = start.get('streamSid')
        fmt = start.get('mediaFormat', {})
        if not isinstance(stream, str) or not stream or fmt.get('encoding') != 'audio/x-mulaw' or fmt.get('sampleRate') != 8000:
            await socket.close(1008, 'Expected Smartflo mulaw 8000 Hz stream')
            return
        url = 'wss://api.openai.com/v1/realtime?model=' + urllib.parse.quote(self.settings.model, safe='')
        async with self.connector(url, additional_headers={
            'Authorization': 'Bearer ' + self.settings.openai_key,
        }, max_size=1048576, open_timeout=15) as ai:
            await ai.send(json.dumps({'type': 'session.update', 'session': {
                'type': 'realtime', 'instructions': self.settings.instructions,
                'output_modalities': ['audio'],
                'audio': {
                    'input': {'format': {'type': 'audio/pcmu'}, 'turn_detection': {
                        'type': 'server_vad', 'create_response': True, 'interrupt_response': True,
                    }},
                    'output': {'format': {'type': 'audio/pcmu'}, 'voice': self.settings.voice},
                },
            }}))
            state = {'ready': False, 'pending': [], 'pending_bytes': 0,
                     'tail': b'', 'chunk': 0, 'item': None, 'sent': 0,
                     'played': 0, 'marks': {}, 'mark_counter': 0}

            async def send_audio(data, final=False):
                state['tail'] += data
                size = len(state['tail']) // 160 * 160
                if final and len(state['tail']) % 160:
                    state['tail'] += b'\xff' * (160 - len(state['tail']) % 160)
                    size = len(state['tail'])
                if size:
                    payload, state['tail'] = state['tail'][:size], state['tail'][size:]
                    state['chunk'] += 1
                    state['sent'] += size
                    await socket.send(json.dumps({'event': 'media', 'streamSid': stream,
                        'media': {'chunk': state['chunk'], 'payload': base64.b64encode(payload).decode()}}))
                    state['mark_counter'] += 1
                    mark = str(state['mark_counter'])
                    state['marks'][mark] = (state['item'], state['sent'] // 8)
                    await socket.send(json.dumps({'event': 'mark', 'streamSid': stream, 'mark': {'name': mark}}))

            async def receive_caller():
                async for raw in socket:
                    m = json.loads(raw)
                    if m.get('event') == 'stop':
                        return
                    if m.get('streamSid') not in (None, stream):
                        raise ValueError('Mismatched stream')
                    if m.get('event') == 'media':
                        audio = m.get('media', {}).get('payload', '')
                        decoded = base64.b64decode(audio, validate=True)
                        if not decoded or len(decoded) > 16000:
                            raise ValueError('Invalid audio frame')
                        event = json.dumps({'type': 'input_audio_buffer.append', 'audio': audio})
                        if state['ready']:
                            await ai.send(event)
                        else:
                            state['pending_bytes'] += len(decoded)
                            if state['pending_bytes'] > 80000:
                                raise ValueError('Startup audio queue full')
                            state['pending'].append(event)
                    elif m.get('event') == 'mark':
                        marker = state['marks'].pop(m.get('mark', {}).get('name'), None)
                        if marker and marker[0] == state['item']:
                            state['played'] = max(state['played'], marker[1])

            async def receive_ai():
                async for raw in ai:
                    event = json.loads(raw)
                    kind = event.get('type')
                    if kind == 'session.updated' and not state['ready']:
                        state['ready'] = True
                        # Keep the configured company, language and AI disclosure in force
                        # for the greeting as well as the rest of the conversation.
                        await ai.send(json.dumps({'type': 'response.create'}))
                        for pending in state['pending']:
                            await ai.send(pending)
                        state['pending'].clear()
                    elif kind == 'response.output_audio.delta':
                        if event.get('item_id') != state['item']:
                            state.update(item=event.get('item_id'), sent=0, played=0, marks={}, tail=b'')
                        await send_audio(base64.b64decode(event['delta'], validate=True))
                    elif kind == 'response.output_audio.done':
                        await send_audio(b'', final=True)
                    elif kind == 'input_audio_buffer.speech_started':
                        await socket.send(json.dumps({'event': 'clear', 'streamSid': stream}))
                        if state['item']:
                            await ai.send(json.dumps({'type': 'conversation.item.truncate',
                                'item_id': state['item'], 'content_index': 0,
                                'audio_end_ms': state['played']}))
                        state.update(item=None, sent=0, played=0, marks={}, tail=b'')
                    elif kind == 'error':
                        raise RuntimeError('Realtime provider error')

            tasks = [asyncio.create_task(receive_caller()), asyncio.create_task(receive_ai())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)


class Socket:
    """Small adapter shared by the real Smartflo connection and test doubles."""
    def __init__(self, socket):
        self.socket = socket

    async def send(self, text):
        await self.socket.send_str(text)

    async def close(self, code=1000, reason=''):
        await self.socket.close(code=code, message=reason.encode())

    def __aiter__(self):
        return self.messages()

    async def messages(self):
        async for message in self.socket:
            if message.type == WSMsgType.TEXT:
                yield message.data
            else:
                return


def create_app(settings=None, connector=None):
    bridge = Bridge(settings or Settings.from_env(), connector)
    app = web.Application(client_max_size=65536)
    app.router.add_route('*', '/{path:.*}', bridge.process_request)
    return app


if __name__ == '__main__':
    web.run_app(create_app(), host='0.0.0.0', port=int(os.getenv('PORT', '10000')), access_log=None)
