import asyncio
import base64
import json
import unittest
from unittest.mock import patch
from aiohttp.test_utils import TestClient, TestServer
from server import Settings, create_app
from test_server import FakeAI, ADMIN, STREAM
from eleven_bridge import Codec


class CodecTests(unittest.TestCase):
    def test_mulaw_passthrough(self):
        codec = Codec('ulaw_8000', 'ulaw_8000')
        audio = bytes(range(256))
        self.assertEqual(codec.to_agent(audio), audio)
        self.assertEqual(codec.to_phone(audio), audio)

    def test_pcm_conversion_preserves_duration(self):
        codec = Codec('pcm_16000', 'pcm_16000')
        pcm = codec.to_agent(b'\xff' * 8000)
        self.assertAlmostEqual(len(pcm), 32000, delta=2)
        phone = codec.to_phone(pcm)
        self.assertAlmostEqual(len(phone), 8000, delta=1)

    def test_unsupported_format_rejected(self):
        with self.assertRaises(ValueError):
            Codec('mp3_44100', 'pcm_16000')


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_way_audio_and_interruptions(self):
        ai = FakeAI()
        settings = Settings(admin_key=ADMIN, stream_key=STREAM, voice_provider='elevenlabs',
                            elevenlabs_key='test-key', elevenlabs_agent='test-agent')
        client = TestClient(TestServer(create_app(settings, lambda *a, **k: ai)))
        await client.start_server()
        try:
            with patch('eleven_bridge.signed_url', return_value='wss://api.elevenlabs.io/test'):
                ws = await client.ws_connect('/voice/stream', headers={'Authorization': 'Bearer ' + STREAM})
                await ws.send_json({'event': 'start', 'start': {'streamSid': 'test-stream',
                                   'mediaFormat': {'encoding': 'audio/x-mulaw', 'sampleRate': 8000}}})
                self.assertEqual((await asyncio.wait_for(ai.sent.get(), 2))['type'], 'conversation_initiation_client_data')
                await ai.incoming.put({'type': 'conversation_initiation_metadata',
                    'conversation_initiation_metadata_event': {'user_input_audio_format': 'ulaw_8000',
                                                              'agent_output_audio_format': 'ulaw_8000'}})
                payload = base64.b64encode(b'\xff' * 320).decode()
                await ws.send_json({'event': 'media', 'streamSid': 'test-stream', 'media': {'payload': payload}})
                self.assertEqual((await asyncio.wait_for(ai.sent.get(), 2))['user_audio_chunk'], payload)
                await ai.incoming.put({'type': 'ping', 'ping_event': {'event_id': 5}})
                self.assertEqual(await asyncio.wait_for(ai.sent.get(), 2), {'type': 'pong', 'event_id': 5})
                await ai.incoming.put({'type': 'audio', 'audio_event': {'event_id': 1, 'audio_base_64': payload}})
                self.assertEqual((await asyncio.wait_for(ws.receive_json(), 2))['media']['payload'], payload)
                await ai.incoming.put({'type': 'interruption', 'interruption_event': {'event_id': 1}})
                self.assertEqual((await asyncio.wait_for(ws.receive_json(), 2))['event'], 'clear')
                await ws.send_json({'event': 'stop'})
                await ws.close()
                status = await client.get('/api/status', headers={'Authorization': 'Bearer ' + ADMIN})
                self.assertEqual((await status.json())['voice_provider'], 'elevenlabs')
        finally:
            await client.close()

    async def test_missing_elevenlabs_credentials_fail_closed(self):
        from aiohttp import WSServerHandshakeError
        client = TestClient(TestServer(create_app(Settings(stream_key=STREAM, voice_provider='elevenlabs', openai_key='unused'))))
        await client.start_server()
        try:
            with self.assertRaises(WSServerHandshakeError):
                await client.ws_connect('/voice/stream', headers={'Authorization': 'Bearer ' + STREAM})
        finally:
            await client.close()

    async def test_probe_generates_audio_without_dialling(self):
        import eleven_bridge
        ai = FakeAI()
        await ai.incoming.put({'type': 'conversation_initiation_metadata',
            'conversation_initiation_metadata_event': {'user_input_audio_format': 'ulaw_8000',
                                                      'agent_output_audio_format': 'ulaw_8000'}})
        await ai.incoming.put({'type': 'audio', 'audio_event': {'event_id': 1,
            'audio_base_64': base64.b64encode(b'\xff' * 320).decode()}})
        with patch('eleven_bridge.signed_url', return_value='wss://api.elevenlabs.io/test'):
            self.assertTrue(await eleven_bridge.probe(Settings(elevenlabs_key='test', elevenlabs_agent='test'), lambda *a, **k: ai))


if __name__ == '__main__':
    unittest.main()
