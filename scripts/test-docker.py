#!/usr/bin/env python3
"""Smoke-check the running Compose APIs. Uses the demo tenant's allowance."""
import concurrent.futures, json, os, time, urllib.request, urllib.error
from pathlib import Path
root=Path(__file__).resolve().parents[1]
config=json.loads((root/'.local/tenants.json').read_text())
ports=[int(os.environ.get('API_PORT_A','8081')),int(os.environ.get('API_PORT_B','8082'))]
def call(i,path,body=None,token=None):
    request=urllib.request.Request(f'http://127.0.0.1:{ports[i]}{path}',data=None if body is None else json.dumps(body).encode(),headers={'Authorization':'Bearer '+(config[0]['token'] if token is None else token),'Content-Type':'application/json'})
    try: response=urllib.request.urlopen(request,timeout=5)
    except urllib.error.HTTPError as e:response=e
    with response:return response.status,json.load(response)
for i in range(2):
    code,health=call(i,'/healthz');assert code==200 and health['instance']==f'docker-api-{i+1}',health
    assert call(i,'/readyz')[0]==200
    assert call(i,'/v1/quota',token='invalid')[0]==401
quota=call(0,'/v1/quota')[1]
time.sleep(quota['full_after_ms']/1000+.05)
# Two independent processes race to spend the entire shared capacity.
start=time.monotonic()
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    result=list(pool.map(lambda i:call(i,'/v1/evaluate',{'cost':quota['capacity']}),[0,1]))
assert time.monotonic()-start<quota['capacity']/quota['refill_per_second'], 'Test took a full refill interval; retry it'
assert all(status==200 for status,_ in result),result
assert sum(body['allowed'] for _,body in result)==1,result
assert call(1,'/v1/quota',token=config[1]['token'])[1]['tenant_id']==config[1]['id']
print('PASS: both containers healthy, authentication enforced, one shared bucket across containers, second tenant accessible independently')
