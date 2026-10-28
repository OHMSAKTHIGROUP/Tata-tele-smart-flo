import json
import os
from pathlib import Path
import tempfile
import unittest
from voice_call import initiate, PROVIDER


class OutboundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.secrets = dict(ADMIN_API_KEY='admin-test-secret',
                            SMARTFLO_API_TOKEN='token-test-secret',
                            SMARTFLO_SUPPORT_API_KEY='support-test-secret')
        self.posts = []

    def transport(self, url, token, payload=None):
        if payload is None:
            return dict(active_calls=0, openai_configured=True, voice_auth_configured=True)
        self.posts.append((url, token, payload))
        ledger = Path(self.temp.name) / 'ApprovedTestRequest123.json'
        self.assertTrue(ledger.exists())
        self.assertEqual(os.stat(ledger).st_mode & 0o777, 0o600)
        return {'success': True, 'ref_id': 'provider-reference'}

    def call(self, transport=None, confirmed=True):
        return initiate('+91 99999 99999', 'ApprovedTestRequest123', self.temp.name,
                        self.secrets, confirmed, transport or self.transport)

    def test_routes_to_bot_and_never_redials_same_request(self):
        result = self.call()
        self.assertEqual(result['state'], 'accepted')
        self.assertEqual(result['verified_outcome'], 'not_verified')
        self.assertEqual(self.call()['state'], 'existing_request')
        self.assertEqual(len(self.posts), 1)
        url, token, payload = self.posts[0]
        self.assertEqual(url, PROVIDER)
        self.assertEqual(token, self.secrets['SMARTFLO_API_TOKEN'])
        self.assertEqual(payload['api_key'], self.secrets['SMARTFLO_SUPPORT_API_KEY'])
        self.assertEqual(payload['customer_number'], '9999999999')
        self.assertEqual(payload['caller_id'], '917969219980')
        self.assertEqual(payload['customer_ring_timeout'], 30)
        self.assertEqual(payload['call_timeout'], 180)
        self.assertNotIn('agent_number', payload)
        saved = next(Path(self.temp.name).glob('*.json')).read_text()
        for secret in self.secrets.values():
            self.assertNotIn(secret, saved)
        self.assertNotIn('9999999999', saved)

    def test_timeout_never_retried_or_exposes_exception(self):
        def uncertain(url, token, payload=None):
            if payload is None:
                return self.transport(url, token)
            self.posts.append(payload)
            raise TimeoutError('private credentials must not escape')
        result = self.call(uncertain)
        self.assertEqual(result['state'], 'outcome_unknown')
        self.assertEqual(self.call()['state'], 'existing_request')
        self.assertEqual(len(self.posts), 1)
        self.assertNotIn('private credentials', json.dumps(result))

    def test_ten_minute_call_limit_and_invalid_limits(self):
        for invalid in (0, 601, True, '600'):
            with self.assertRaises(ValueError):
                initiate('9999999999', 'ApprovedTestRequest123', self.temp.name,
                         self.secrets, True, self.transport, call_timeout=invalid)
        result = initiate('9999999999', 'ApprovedTestRequest123', self.temp.name,
                          self.secrets, True, self.transport, call_timeout=600)
        self.assertEqual(result['state'], 'accepted')
        self.assertEqual(self.posts[0][2]['call_timeout'], 600)
        self.assertEqual(self.posts[0][2]['customer_ring_timeout'], 30)

    def test_busy_bridge_and_missing_authorization_do_not_dial(self):
        with self.assertRaises(ValueError):
            self.call(confirmed=False)
        result = self.call(lambda *args: dict(active_calls=1, openai_configured=True, voice_auth_configured=True))
        self.assertEqual(result['state'], 'voice_busy_or_unknown')
        self.assertEqual(self.posts, [])
        self.assertEqual(list(Path(self.temp.name).glob('*.json')), [])


if __name__ == '__main__':
    unittest.main()
