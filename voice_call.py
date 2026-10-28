"""Supervised AI outbound calls. Secrets are read locally and never logged.

The legacy /api/calls endpoint calls a human agent. This client instead uses
Click-to-Call Support, whose existing key routes this DID to OpenAI bot 2622.
Run only after recipient-specific authorization, with --number, --request-id,
--ledger-dir and --confirmed. Reuse that request ID for inspection, never create
a new ID to retry an uncertain attempt. Accepted does not mean answered.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.request

BASE = 'https://ohmsakthi-smartflo-integration.onrender.com'
PROVIDER = 'https://api-smartflo.tatateleservices.com/v1/click_to_call_support'
CALLER_ID = '917969219980'


def credentials(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            name, value = line.split('=', 1)
            values[name.strip()] = value.strip().strip("\"'")
    required = ('ADMIN_API_KEY', 'SMARTFLO_API_TOKEN', 'SMARTFLO_SUPPORT_API_KEY')
    if not all(values.get(name) for name in required):
        raise ValueError('Required private credentials are unavailable')
    return values


def customer_number(number):
    number = re.sub(r'[\s+()-]', '', number)
    if number.startswith('91') and len(number) == 12:
        number = number[2:]
    if not re.fullmatch(r'[6-9][0-9]{9}', number):
        raise ValueError('Supply an Indian mobile number with ten digits')
    return number


def request_json(url, token, payload=None):
    auth = token if token.lower().startswith('bearer ') else 'Bearer ' + token
    req = urllib.request.Request(url,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={'Authorization': auth, 'Content-Type': 'application/json'})
    # urllib does not retry failed requests. Never add retries to call creation.
    with urllib.request.urlopen(req, timeout=30) as result:
        return json.load(result)


def initiate(number, request_id, ledger_dir, secrets, confirmed=False, transport=request_json, call_timeout=180):
    if confirmed is not True:
        raise ValueError('Specific recipient and call purpose must be authorized first')
    if type(call_timeout) is not int or not 10 <= call_timeout <= 600:
        raise ValueError('Call timeout must be 10-600 seconds')
    number = customer_number(number)
    if not re.fullmatch(r'[A-Za-z0-9]{16,80}', request_id):
        raise ValueError('Request ID must contain 16-80 letters or digits')
    folder = Path(ledger_dir)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    ledger = folder / (request_id + '.json')
    if ledger.exists():
        return {'state': 'existing_request', 'redialed': False}
    status = transport(BASE + '/api/status', secrets['ADMIN_API_KEY'])
    if not status.get('openai_configured') or not status.get('voice_auth_configured'):
        return {'state': 'voice_not_ready', 'dialed': False}
    # Require an idle bridge to give this supervised test exclusive capacity.
    if status.get('active_calls') != 0:
        return {'state': 'voice_busy_or_unknown', 'dialed': False}
    state = {'state': 'request_pending', 'request_id': request_id,
             'customer_ring_timeout': 30, 'call_timeout': call_timeout,
             'recipient_hash': hashlib.sha256(number.encode()).hexdigest(),
             'success': None, 'ref_id': None, 'verified_outcome': 'not_verified'}
    try:
        fd = os.open(ledger, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return {'state': 'existing_request', 'redialed': False}
    with os.fdopen(fd, 'w') as handle:
        def save():
            handle.seek(0)
            handle.truncate()
            json.dump(state, handle)
            handle.flush()
            os.fsync(handle.fileno())
        save()  # The ledger must exist durably BEFORE the single POST.
        payload = {'customer_number': number,
                   'api_key': secrets['SMARTFLO_SUPPORT_API_KEY'],
                   'caller_id': CALLER_ID, 'async': 1,
                   'customer_ring_timeout': 30, 'call_timeout': call_timeout,
                   'custom_identifier': {'testid': request_id}}
        try:
            result = transport(PROVIDER, secrets['SMARTFLO_API_TOKEN'], payload)
            accepted = isinstance(result, dict) and result.get('success') is True
            ref = result.get('ref_id') if isinstance(result, dict) else None
            state.update(state='accepted' if accepted else 'provider_rejected', success=accepted,
                         ref_id=ref if isinstance(ref, str) and re.fullmatch(r'[A-Za-z0-9-]{1,120}', ref) else None)
        except Exception:
            # Even HTTP errors can leave uncertain provider state. Never retry.
            state.update(state='outcome_unknown', success=None)
        save()
    return {key: state[key] for key in ('state', 'success', 'ref_id', 'verified_outcome')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--number', required=True)
    parser.add_argument('--request-id', required=True)
    parser.add_argument('--ledger-dir', required=True)
    parser.add_argument('--call-timeout', type=int, default=180)
    parser.add_argument('--credentials', default=str(Path(__file__).with_name('.env.local')))
    parser.add_argument('--confirmed', action='store_true', help='Use only after explicit call authorization')
    args = parser.parse_args()
    try:
        result = initiate(args.number, args.request_id, args.ledger_dir,
                          credentials(args.credentials), args.confirmed, call_timeout=args.call_timeout)
    except Exception as error:
        # A failure persisting the final ledger may occur AFTER the POST.
        # Never infer that no call was made from a generic exception.
        result = {'state': 'stopped_check_records_before_retry', 'error_type': type(error).__name__}
    print(json.dumps(result))


if __name__ == '__main__':
    main()
