#!/usr/bin/env python3
"""Trace each real HTTP call without printing bearer tokens."""
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

root = Path(__file__).resolve().parents[1]
config = json.loads((root / '.local/tenants.json').read_text())
ports = [int(os.environ.get('API_PORT_A', '8081')),
         int(os.environ.get('API_PORT_B', '8082'))]
started = time.monotonic()
call_number = 0


def request(index, tenant, path, body=None):
    global call_number
    call_number += 1
    method = 'GET' if body is None else 'POST'
    url = f'http://127.0.0.1:{ports[index]}{path}'
    print(f'\n[{call_number:02d} | +{time.monotonic() - started:.3f}s] SEND {method} {url}', flush=True)
    print(f'  Tenant: {config[tenant]["id"]} | API instance: {index + 1} | Authorization: Bearer <hidden>', flush=True)
    if body is not None:
        print(f'  Request body: {json.dumps(body)}', flush=True)
    else:
        print('  Read only: no tokens consumed.', flush=True)
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, method=method, data=data, headers={
        'Authorization': 'Bearer ' + config[tenant]['token'],
        'Content-Type': 'application/json',
    })
    sent = time.monotonic()
    try:
        response = urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    except urllib.error.URLError as error:
        raise SystemExit(f'  Connection failed: {error.reason}. Start the service first.')
    with response:
        result = json.load(response)
        print(f'  RECEIVE HTTP {response.status} | {(time.monotonic() - sent) * 1000:.1f} ms', flush=True)
        print('  Response: ' + json.dumps(result, sort_keys=True), flush=True)
        if response.status != 200:
            raise SystemExit('Demo stopped because the API returned an error.')
        return result


print('This script makes real HTTP requests to the running service.')
print('Calls are sequential: send one request, receive its response, then send the next.')
print('POST /v1/evaluate asks permission and consumes tokens when allowed.')
print('GET /v1/quota only reads quota. The demo does not perform a business operation.')
print('\nSTEP 1: Inspect Tenant A and wait for a full starting bucket if needed.')
q = request(0, 0, '/v1/quota')
delay = q['full_after_ms'] / 1000 + .05
if delay > .05:
    print(f'\nWAIT {delay:.2f}s for the bucket to fill. No API calls during this wait.', flush=True)
    time.sleep(delay)
print('\nSTEP 2: Send 12 cost-one requests, alternating between API 1 and API 2.')
for i in range(12):
    request(i % 2, 0, '/v1/evaluate', {'cost': 1})
print('\nSTEP 3: Read Tenant B’s independent quota through API 2.')
request(1, 1, '/v1/quota')
print('\nSTEP 4: WAIT 0.6s for refill. No API calls during this wait.', flush=True)
time.sleep(.6)
print('Now send one more Tenant A evaluation through API 1.')
request(0, 0, '/v1/evaluate', {'cost': 1})
print('\nSTEP 5: Read Tenant A’s remaining quota through API 2 without spending it.')
request(1, 0, '/v1/quota')
print(f'\nDone: {call_number} real HTTP calls completed. No background requests are made by this script.')
print('Docker health checks are separate and may continue calling /readyz.')
