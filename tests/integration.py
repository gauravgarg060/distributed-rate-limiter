#!/usr/bin/env python3
"""No Python packages needed. Exercises real Redis, Lua, and multiple API processes."""

import concurrent.futures, json, os, shutil, signal, socket, subprocess, tempfile, time
import urllib.request, urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
A = "test-a-secret-token-1234567890"
B = "test-b-secret-token-1234567890"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Redis:
    def __init__(self, port):
        self.port = port

    def cmd(self, *args):
        args = [str(a).encode() for a in args]
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as sock:
            sock.sendall(
                b"*"
                + str(len(args)).encode()
                + b"\r\n"
                + b"".join(b"$" + str(len(a)).encode() + b"\r\n" + a + b"\r\n" for a in args)
            )
            f = sock.makefile("rb")

            def read():
                line = f.readline()
                kind = line[:1]
                value = line[1:-2]
                if kind == b"+":
                    return value.decode()
                if kind == b"-":
                    raise RuntimeError(value.decode())
                if kind == b":":
                    return int(value)
                if kind == b"$":
                    n = int(value)
                    if n < 0:
                        return None
                    data = f.read(n)
                    assert f.read(2) == b"\r\n"
                    return data.decode()
                if kind == b"*":
                    return [read() for _ in range(int(value))]
                raise RuntimeError("Invalid RESP")

            return read()


def call(port, path, token=A, body=None, raw=None):
    data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    try:
        r = urllib.request.urlopen(req, timeout=8)
    except urllib.error.HTTPError as e:
        r = e
    with r:
        text = r.read().decode()
        return r.status, (
            json.loads(text)
            if r.headers.get("Content-Type", "").startswith("application/json")
            else text
        )


def stop(p):
    if p.poll() is None:
        p.send_signal(signal.SIGTERM)
        try:
            assert p.wait(timeout=10) == 0
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            raise


def wait_ready(p, port):
    for _ in range(100):
        if p.poll() is not None:
            raise RuntimeError("Process exited during startup")
        try:
            if call(port, "/readyz")[0] == 200:
                return
        except OSError:
            pass
        time.sleep(0.05)
    raise RuntimeError("Readiness timeout")


with tempfile.TemporaryDirectory() as folder:
    folder = Path(folder)
    redis_port = free_port()
    ports = [free_port(), free_port()]
    assert len(set([redis_port] + ports)) == 3
    config = folder / "tenants.json"
    config.write_text(
        json.dumps(
            [
                {"id": t, "token": token, "capacity": 10, "refill_per_second": 0.001}
                for t, token in [("a", A), ("b", B)]
            ]
        )
    )
    redis_bin = (
        os.environ.get("REDIS_SERVER")
        or shutil.which("redis-server")
        or str(ROOT / ".local/bin/redis-server")
    )
    redis_args = [
        redis_bin,
        "--bind",
        "127.0.0.1",
        "--port",
        str(redis_port),
        "--save",
        "",
        "--appendonly",
        "no",
        "--dir",
        str(folder),
    ]
    processes = []

    def start_redis():
        p = subprocess.Popen(redis_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(p)
        for _ in range(100):
            if p.poll() is not None:
                raise RuntimeError("Redis startup failed")
            try:
                if Redis(redis_port).cmd("PING") == "PONG":
                    return p
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("Redis timeout")

    def start_api(i, config_path=config):
        env = dict(
            os.environ,
            PORT=str(ports[i]),
            BIND_HOST="127.0.0.1",
            REDIS_HOST="127.0.0.1",
            REDIS_PORT=str(redis_port),
            REDIS_PASSWORD="",
            TENANTS_FILE=str(config_path),
            KEY_PREFIX="test",
            INSTANCE_ID=f"test-{i}",
        )
        p = subprocess.Popen([str(ROOT / "build/rate-limiter")], env=env, stdout=subprocess.DEVNULL)
        processes.append(p)
        wait_ready(p, ports[i])
        return p

    try:
        redis_process = start_redis()
        r = Redis(redis_port)
        script = (ROOT / "src/bucket.lua").read_text()
        # Only the Redis TIME acquisition is replaced. The production algorithm is unchanged.
        marker = "local now_parts = redis.call('TIME')"
        assert script.count(marker) == 1
        controlled = script.replace(marker, "local now_parts = {ARGV[5], 0}")

        def eval_at(now, cost=1, mode="consume", key="clock", capacity=10, rate=2):
            return r.cmd("EVAL", controlled, 1, key, capacity, rate, cost, mode, now)

        assert eval_at(100, cost=10) == [1, 0, 0, 5000]
        assert eval_at(100) == [0, 0, 500, 5000]
        assert eval_at(101, mode="inspect") == [1, 2, 0, 4000]
        assert eval_at(101, cost=2) == [1, 0, 0, 5000]
        assert eval_at(100) == [0, 0, 1500, 6000]  # backwards clock grants no refill
        assert eval_at(102) == [1, 1, 0, 4500]
        assert eval_at(200, mode="inspect") == [1, 10, 0, 0]
        assert eval_at(200, cost=10) == [1, 0, 0, 5000]
        assert 0 < r.cmd("PTTL", "clock") <= 5000
        assert r.cmd("EVAL", controlled, 1, "inspection-only", 10, 2, 1, "inspect", 100) == [
            1,
            10,
            0,
            0,
        ]
        assert r.cmd("EXISTS", "inspection-only") == 0
        eval_at(100, cost=10, key="expiry", rate=1000)
        time.sleep(0.03)
        assert r.cmd("EXISTS", "expiry") == 0
        print(
            "PASS deterministic refill, capacity cap, retry delay, backward clock, safe expiry, nonmutating inspection"
        )
        # HTTP validation: clients cannot spoof identity or submit invalid costs.
        apis = [start_api(0), start_api(1)]
        assert call(ports[0], "/v1/quota", token="wrong")[0] == 401
        for body in [
            {"cost": 0},
            {"cost": 11},
            {"cost": 1.2},
            {"cost": True},
            {"tenant_id": "b"},
            [],
            {"cost": 2**64},
        ]:
            assert call(ports[0], "/v1/evaluate", body=body)[0] == 400, body
        assert call(ports[0], "/v1/evaluate", raw=b"{")[0] == 400
        assert call(ports[0], "/v1/evaluate", raw=b"x" * 5000)[0] == 413
        assert call(ports[0], "/v1/quota?tenant_id=b")[0] == 400
        assert call(ports[0], "/v1/quota")[1]["remaining"] == 10
        # Race real requests across two processes, not merely threads in one API.
        start = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
            results = list(
                pool.map(lambda i: call(ports[i % 2], "/v1/evaluate", body={}), range(80))
            )
        assert time.monotonic() - start < 1000  # refill interval = 1000s, so no extra whole token
        assert all(code == 200 for code, _ in results)
        assert sum(body["allowed"] for _, body in results) == 10
        assert len({body["request_id"] for _, body in results}) == 80
        assert call(ports[1], "/v1/quota")[1]["remaining"] == 0
        # Exhausting tenant A must leave tenant B with its independent allowance.
        assert call(ports[1], "/v1/quota", token=B)[1]["remaining"] == 10
        assert call(ports[1], "/v1/evaluate", token=B, body={"cost": 10})[1]["allowed"]
        assert not call(ports[0], "/v1/evaluate", token=B, body={})[1]["allowed"]
        assert "rate_limiter_decisions_total" in call(ports[0], "/metrics")[1]
        assert call(ports[0], "/metrics", token="wrong")[0] == 401
        # API memory is disposable: restarting it must not reset Redis quota.
        stop(apis[0])
        apis[0] = start_api(0)
        assert not call(ports[0], "/v1/evaluate", body={})[1]["allowed"]
        print(
            "PASS two-instance contention: exactly 10/80 allowed, authentication, input validation, weighted cost, tenant isolation, metrics, API restart"
        )
        # A replica with inconsistent policy must reject existing state, not grant more.
        other = folder / "mismatch.json"
        data = json.loads(config.read_text())
        data[0]["capacity"] = 20
        other.write_text(json.dumps(data))
        stop(apis[1])
        apis[1] = start_api(1, other)
        assert call(ports[1], "/v1/evaluate", body={})[0] == 503
        stop(apis[1])
        apis[1] = start_api(1)
        # With Redis down, liveness remains healthy but no decision is granted.
        stop(redis_process)
        for port in ports:
            assert call(port, "/healthz")[0] == 200
            assert call(port, "/readyz")[0] == 503
            assert call(port, "/v1/evaluate", body={})[0] == 503
        redis_process = start_redis()
        # First request may discover a stale connection; later requests must reconnect.
        for _ in range(5):
            if call(ports[0], "/readyz")[0] == 200:
                break
        else:
            raise AssertionError("No reconnect")
        assert call(ports[0], "/v1/evaluate", body={})[0] == 200
        print(
            "PASS policy mismatch, Redis outage fails closed, liveness/readiness distinction, recovery"
        )
    finally:
        for p in reversed(processes):
            stop(p)
print("ALL TESTS PASSED")
