#!/usr/bin/env python3
"""Prepare local secrets and run the container as the host user to read mode-0600 config."""
import json, os, secrets
from pathlib import Path
root=Path(__file__).resolve().parents[1]
local=root/'.local'
local.mkdir(exist_ok=True,mode=0o700)
config=local/'tenants.json'
if not config.exists():
    with os.fdopen(os.open(config,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:
        json.dump([{'id':t,'token':secrets.token_hex(24),'capacity':10,'refill_per_second':2} for t in ['tenant-a','tenant-b']],f,indent=2)
if os.getuid()==0:
    raise SystemExit('Run as your regular user so the API containers do not run as root.')
(local/'docker.env').write_text(f'LOCAL_UID={os.getuid()}\nLOCAL_GID={os.getgid()}\n')
print('Docker configuration ready; existing tenant credentials preserved.')
