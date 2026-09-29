#!/usr/bin/env python3
"""Supervise an isolated Redis and two API instances; Ctrl-C stops all children."""

import json, os, secrets, shutil, signal, socket, subprocess, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
LOCAL = ROOT / ".local"
LOCAL.mkdir(exist_ok=True, mode=0o700)
config = LOCAL / "tenants.json"
if not config.exists():
    fd = os.open(config, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(
            [
                {
                    "id": tenant,
                    "token": secrets.token_hex(24),
                    "capacity": 10,
                    "refill_per_second": 2,
                }
                for tenant in ["tenant-a", "tenant-b"]
            ],
            f,
            indent=2,
        )
redis_bin = (
    os.environ.get("REDIS_SERVER")
    or shutil.which("redis-server")
    or str(LOCAL / "bin/redis-server")
)
redis_port = int(os.environ.get("LOCAL_REDIS_PORT", "6380"))
ports = [int(os.environ.get("API_PORT_A", "8081")), int(os.environ.get("API_PORT_B", "8082"))]
# Reject occupied ports before starting anything; never reuse unrelated services.
if len(set([redis_port] + ports)) != 3:
    raise SystemExit("All three ports must be distinct")
for port in [redis_port] + ports:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            raise SystemExit(
                f"Port {port} unavailable; stop the existing service or choose another port"
            )
children = []


def shutdown(*_):
    raise KeyboardInterrupt


signal.signal(signal.SIGTERM, shutdown)
try:
    children.append(
        subprocess.Popen(
            [
                redis_bin,
                "--bind",
                "127.0.0.1",
                "--port",
                str(redis_port),
                "--protected-mode",
                "yes",
                "--appendonly",
                "yes",
                "--appendfsync",
                "everysec",
                "--save",
                "",
                "--dir",
                str(LOCAL),
                "--logfile",
                str(LOCAL / "redis.log"),
            ]
        )
    )
    for _ in range(100):
        if children[0].poll() is not None:
            raise RuntimeError("Redis failed; inspect .local/redis.log")
        try:
            with socket.create_connection(("127.0.0.1", redis_port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError("Redis startup timeout")
    for i, port in enumerate(ports):
        env = dict(
            os.environ,
            REDIS_HOST="127.0.0.1",
            REDIS_PORT=str(redis_port),
            PORT=str(port),
            TENANTS_FILE=str(config),
            INSTANCE_ID=f"api-{i+1}",
            KEY_PREFIX="rl-demo",
            BIND_HOST="127.0.0.1",
        )
        children.append(subprocess.Popen([str(ROOT / "build/rate-limiter")], env=env))
    for port in ports:
        for _ in range(100):
            if any(p.poll() is not None for p in children):
                raise RuntimeError("A service exited during startup")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=2) as r:
                    if r.status == 200:
                        break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("API readiness timeout")
    print(
        f"Ready: APIs on {ports}; Redis on {redis_port}. Run make demo. Ctrl-C stops all three.",
        flush=True,
    )
    while True:
        time.sleep(0.5)
        if any(p.poll() is not None for p in children):
            raise RuntimeError("A service exited unexpectedly")
except KeyboardInterrupt:
    print("\nStopping local stack.", flush=True)
finally:
    for p in reversed(children):
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
