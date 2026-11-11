import asyncio
import base64
from contextlib import asynccontextmanager
import json
import unittest
from unittest.mock import patch

from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer
from google.genai import types

import gemini_bridge
from server import Settings, create_app


class Phone:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self):
        return json.dumps(await self.incoming.get())

    async def send(self, raw):
        await self.sent.put(json.loads(raw))


class Session:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = asyncio.Queue()
        self.closed = False
        self.receive_count = 0

    async def send_client_content(self, **data):
        await self.sent.put(('greeting', data))

    async def send_realtime_input(self, **data):
        await self.sent.put(('audio', data))

    async def receive(self):
        self.receive_count += 1
        while True:
            message = await self.incoming.get()
            if message is None:
                return
            yield message
            if message.server_content and message.server_content.turn_complete:
                return


def output(data=b'\x00\x00' * 480, **flags):
    return types.LiveServerMessage(server_content=types.LiveServerContent(
        model_turn=types.Content(parts=[types.Part(inline_data=types.Blob(
            data=data, mime_type='audio/pcm;rate=24000'))]), **flags))


class GeminiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.phone = Phone()
        self.session = Session()
        self.events = []
        self.config = None

        @asynccontextmanager
        async def factory(key, model, config):
            self.config = config
            try:
                yield self.session
            finally:
                self.session.closed = True
        self.factory = factory

    async def start(self):
        self.task = asyncio.create_task(gemini_bridge.run(
            self.phone, 'test', 'fake-google-key', 'test-live-model', 'Aoede',
            'தமிழில் AI உதவியாளராகப் பேசவும்.', self.events.append,
            session_factory=self.factory))
        async def cleanup():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.addAsyncCleanup(cleanup)
        self.assertEqual((await asyncio.wait_for(self.session.sent.get(), 2))[0], 'greeting')

    async def test_audio_both_directions_and_multiple_turns(self):
        await self.start()
        self.assertIn('தமிழில்', self.config['system_instruction'])
        self.assertEqual(self.config['response_modalities'], ['AUDIO'])
        await self.phone.incoming.put({'event': 'media', 'streamSid': 'test',
            'media': {'payload': base64.b64encode(b'\xff' * 160).decode()}})
        kind, data = await asyncio.wait_for(self.session.sent.get(), 2)
        self.assertEqual(kind, 'audio')
        self.assertEqual(data['audio']['mime_type'], 'audio/pcm;rate=16000')
        self.assertIn(len(data['audio']['data']), (638, 640))
        for _ in range(2):
            await self.session.incoming.put(output(turn_complete=True))
            media = await asyncio.wait_for(self.phone.sent.get(), 2)
            self.assertEqual(media['streamSid'], 'test')
            self.assertEqual(len(base64.b64decode(media['media']['payload'])), 160)
        self.assertGreaterEqual(self.session.receive_count, 2)
        await self.phone.incoming.put({'event': 'stop'})
        await asyncio.wait_for(self.task, 2)
        self.assertTrue(self.session.closed)
        self.assertEqual(self.events.count('first_gemini_audio'), 1)

    async def test_interruption_clears_buffer_and_partial_turn_is_padded(self):
        await self.start()
        await self.session.incoming.put(output(data=b'\x00\x00' * 30))
        await self.session.incoming.put(types.LiveServerMessage(
            server_content=types.LiveServerContent(interrupted=True)))
        self.assertEqual((await asyncio.wait_for(self.phone.sent.get(), 2))['event'], 'clear')
        await self.session.incoming.put(output(data=b'\x00\x00' * 30, turn_complete=True))
        media = await asyncio.wait_for(self.phone.sent.get(), 2)
        self.assertEqual(len(base64.b64decode(media['media']['payload'])), 160)
        self.assertIn('gemini_interrupted', self.events)

    async def test_bad_caller_audio_and_mismatched_stream_end_session(self):
        for frame in ({'event': 'media', 'media': {'payload': 'not base64!'}},
                      {'event': 'media', 'streamSid': 'other', 'media': {'payload': '/w=='}},
                      {'event': 'media', 'media': {'payload': ''}}):
            await self.start()
            await self.phone.incoming.put(frame)
            with self.assertRaises(ValueError):
                await asyncio.wait_for(self.task, 2)
            self.assertTrue(self.session.closed)
            self.session.closed = False

    async def test_bad_output_fails_closed(self):
        await self.start()
        await self.session.incoming.put(output(data=b'x'))
        with self.assertRaises(ValueError):
            await asyncio.wait_for(self.task, 2)
        self.assertTrue(self.session.closed)

    async def test_expiring_session_ends_without_reconnect(self):
        await self.start()
        await self.session.incoming.put(types.LiveServerMessage(
            go_away=types.LiveServerGoAway(time_left='10s')))
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(self.task, 2)
        self.assertTrue(self.session.closed)
        self.assertIn('gemini_session_expiring', self.events)

    async def test_synthetic_probe_stops_after_first_audio_without_dialling(self):
        from gemini_probe import probe
        task = asyncio.create_task(probe(
            Settings(gemini_key='fake', gemini_model='test-live-model'), self.factory))
        await asyncio.wait_for(self.session.sent.get(), 2)
        await self.session.incoming.put(output(turn_complete=True))
        result = await asyncio.wait_for(task, 2)
        self.assertTrue(result['audio_received'])
        self.assertTrue(self.session.closed)

    async def test_closed_receive_does_not_spin(self):
        await self.start()
        await self.session.incoming.put(None)
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(self.task, 2)
        self.assertEqual(self.session.receive_count, 1)

    async def test_missing_configuration_never_opens_google(self):
        self.assertFalse(gemini_bridge.configured('key', 'https://untrusted.example/model'))
        with self.assertRaises(ValueError):
            await gemini_bridge.run(self.phone, 'test', '', '', 'Aoede', '',
                                    self.events.append, session_factory=self.factory)
        self.assertIsNone(self.config)

    async def test_route_uses_gemini_without_openai_or_elevenlabs(self):
        settings = Settings(admin_key='a' * 40, stream_key='s' * 40,
                            voice_provider='gemini', gemini_key='fake-google-key',
                            gemini_model='test-live-model')
        client = TestClient(TestServer(create_app(settings)))
        await client.start_server()
        try:
            status = await client.get('/api/status', headers={'Authorization': 'Bearer ' + 'a' * 40})
            body = await status.json()
            self.assertTrue(body['voice_configured'])
            self.assertTrue(body['gemini_configured'])
            self.assertFalse(body['openai_configured'])
            self.assertNotIn('fake-google-key', json.dumps(body))
            with patch('server.gemini_bridge.run') as run, patch('server.eleven_bridge.run') as eleven:
                ws = await client.ws_connect('/voice/stream', headers={'Authorization': 'Bearer ' + 's' * 40})
                await ws.send_json({'event': 'start', 'start': {'streamSid': 'test',
                    'mediaFormat': {'encoding': 'audio/x-mulaw', 'sampleRate': 8000}}})
                await asyncio.wait_for(ws.receive(), 2)
                run.assert_awaited_once()
                eleven.assert_not_called()
            settings.gemini_key = ''
            with self.assertRaises(WSServerHandshakeError):
                await client.ws_connect('/voice/stream', headers={'Authorization': 'Bearer ' + 's' * 40})
        finally:
            await client.close()


if __name__ == '__main__':
    unittest.main()
