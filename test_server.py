import asyncio
import base64
import json
import unittest
from unittest.mock import patch
from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer
from server import Bridge, Settings, create_app

ADMIN = 'a' * 40
STREAM = 's' * 40


class FakeAI:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = asyncio.Queue()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def send(self, text):
        await self.sent.put(json.loads(text))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return json.dumps(await self.incoming.get())


class Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ai = FakeAI()
        self.opened = 0
        def connector(*args, **kwargs):
            self.opened += 1
            return self.ai
        self.settings = Settings(admin_key=ADMIN, stream_key=STREAM, openai_key='test-only', smartflo_token='test-only')
        self.client = TestClient(TestServer(create_app(self.settings, connector)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_public_health_and_private_status(self):
        result = await self.client.get('/healthz')
        self.assertEqual(result.status, 200)
        self.assertNotIn('openai_configured', await result.json())
        self.assertEqual((await self.client.get('/api/status')).status, 401)
        result = await self.client.get('/api/status', headers={'Authorization': 'Bearer ' + ADMIN})
        self.assertFalse((await result.json())['outbound_calls_enabled'])

    async def test_unconfigured_auth_fails_closed(self):
        client = TestClient(TestServer(create_app(Settings())))
        await client.start_server()
        try:
            self.assertEqual((await client.get('/api/status')).status, 401)
            with self.assertRaises(WSServerHandshakeError):
                await client.ws_connect('/voice/stream')
        finally:
            await client.close()

    async def test_unknown_websocket_rejected(self):
        with self.assertRaises(WSServerHandshakeError):
            await self.client.ws_connect('/unknown')

    async def test_bad_auth_does_not_contact_openai(self):
        with self.assertRaises(WSServerHandshakeError):
            await self.client.ws_connect('/voice/stream', headers={'Authorization':'Bearer wrong'})
        self.assertEqual(self.opened, 0)

    async def test_invalid_audio_format_does_not_contact_openai(self):
        socket = await self.client.ws_connect('/voice/stream', headers={'Authorization':'Bearer ' + STREAM})
        await socket.send_json({'event':'start','start':{'streamSid':'test','mediaFormat':{'encoding':'pcm','sampleRate':16000}}})
        await asyncio.wait_for(socket.receive(), 2)
        self.assertEqual(self.opened, 0)

    async def test_bidirectional_audio_framing_and_interruption(self):
        self.settings.instructions = 'தமிழில் ஆனந்தா இன் AI உதவியாளராகப் பேசவும்.'
        socket = await self.client.ws_connect('/voice/stream', headers={'Authorization':'Bearer ' + STREAM})
        await socket.send_json({'event':'connected'})
        await socket.send_json({'event':'start','start':{'streamSid':'test','mediaFormat':{'encoding':'audio/x-mulaw','sampleRate':8000}}})
        config = await asyncio.wait_for(self.ai.sent.get(), 2)
        self.assertEqual(config['session']['audio']['input']['format']['type'], 'audio/pcmu')
        self.assertEqual(config['session']['instructions'], self.settings.instructions)
        audio = base64.b64encode(b'\xff' * 800).decode()
        await socket.send_json({'event':'media','streamSid':'test','media':{'payload':audio}})
        await self.ai.incoming.put({'type':'session.updated'})
        greeting = await asyncio.wait_for(self.ai.sent.get(), 2)
        self.assertEqual(greeting, {'type': 'response.create'})
        self.assertEqual((await asyncio.wait_for(self.ai.sent.get(), 2))['audio'],audio)
        await self.ai.incoming.put({'type':'response.output_audio.delta','item_id':'item1','delta':base64.b64encode(b'x' * 161).decode()})
        media = await asyncio.wait_for(socket.receive_json(), 2)
        self.assertEqual(len(base64.b64decode(media['media']['payload'])),160)
        mark = await asyncio.wait_for(socket.receive_json(), 2)
        await socket.send_json(mark)
        # Ensure acknowledgement processed before caller starts speaking.
        await socket.send_json({'event':'media','media':{'payload':audio}})
        await asyncio.wait_for(self.ai.sent.get(), 2)
        await self.ai.incoming.put({'type':'input_audio_buffer.speech_started'})
        self.assertEqual((await asyncio.wait_for(socket.receive_json(), 2))['event'],'clear')
        truncate = await asyncio.wait_for(self.ai.sent.get(), 2)
        self.assertEqual(truncate['type'],'conversation.item.truncate')
        self.assertEqual(truncate['audio_end_ms'],20)
        await socket.send_json({'event':'stop'})
        await socket.close()

    async def test_outbound_disabled_by_default(self):
        result = await self.client.post('/api/calls',json={},headers={'Authorization':'Bearer ' + ADMIN})
        self.assertEqual(result.status,503)

    async def test_outbound_retry_does_not_redial(self):
        self.settings.outbound_enabled = True
        headers = {'Authorization':'Bearer ' + ADMIN,'Idempotency-Key':'unique-test-key-12345'}
        data = {'agent_number':'919876543210','destination_number':'919876543211','confirmed':True}
        with patch('server.provider_request', return_value={'success':True,'ref_id':'test-reference'}) as provider:
            first = await self.client.post('/api/calls',json=data,headers=headers)
            second = await self.client.post('/api/calls',json=data,headers=headers)
            self.assertEqual(first.status,202)
            self.assertEqual(await first.json(),await second.json())
            self.assertEqual(provider.call_count,1)
            data['destination_number']='919876543212'
            self.assertEqual((await self.client.post('/api/calls',json=data,headers=headers)).status,409)

    async def test_call_filters_and_redaction(self):
        headers = {'Authorization':'Bearer ' + ADMIN}
        self.assertEqual((await self.client.get('/api/call-records',headers=headers)).status,400)
        params={'from_date':'2026-10-27 00:00:00','to_date':'2026-10-27 23:59:59'}
        with patch('server.provider_request',return_value={'count':1,'page':1,'results':[{'call_id':'one','recording_url':'secret-url','client_number':'private-number'}]}):
            result = await self.client.get('/api/call-records',params=params,headers=headers)
            self.assertEqual(result.status,200)
            body = await result.text()
            self.assertNotIn('secret-url',body)
            self.assertNotIn('private-number',body)
        params['limit']='9999'
        self.assertEqual((await self.client.get('/api/call-records',params=params,headers=headers)).status,400)


if __name__ == '__main__':
    unittest.main()
