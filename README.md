# Distributed rate limiter in C++

This service answers one question: **“May this tenant perform this operation right now?”**

A tenant is a customer or account with its own allowance. In the demo, Tenant A and Tenant B each start with 10 tokens and regain 2 tokens per second. A normal request costs one token. When there are not enough tokens, the service denies the request and estimates how long to wait.

Two copies of the C++ API run locally. Both use the same Redis server, so sending a request to the other copy does not give a tenant extra allowance. This is the distributed part of the exercise.

## How to read this project

| Document | What you will learn |
|---|---|
| This README | Run the service, call its APIs, understand results, and troubleshoot |
| [Architecture explained](ARCHITECTURE.md) | How the algorithm works, why Redis is needed, and what happens during failures |
| [Local Docker guide](DOCKER.md) | Run Redis and both APIs in containers |
| [Interview walkthrough](INTERVIEW.md) | Demonstrate the project and explain its decisions with examples |

Start here, run the demo, and then read the architecture guide. The native build/tests and local Docker build, three-container startup, shared-quota smoke test, and demo have been verified. The Ubuntu build, integration suite, and Docker smoke test also passed in the [successful GitHub Actions run](https://github.com/gauravgarg060/distributed-rate-limiter/actions/runs/36593584252).

## 1. Run the project locally

Open a terminal in the `rate-limiter` project directory:

```sh
make
make run
```

`make` compiles the C++ executable. `make run` starts a Python supervisor: a small program that launches and monitors three processes.

| Process | Address | Purpose |
|---|---|---|
| API instance 1 | `http://127.0.0.1:8081` | Accept HTTP requests |
| API instance 2 | `http://127.0.0.1:8082` | Accept requests using the same tenant allowance |
| Redis | `127.0.0.1:6380` | Hold the shared bucket state and execute decisions |

The supervisor waits until both APIs can reach Redis, then prints `Ready`. Stop it with Ctrl-C in the terminal where you started it.

A local Redis executable is already available on the preparation Mac at `.local/bin/redis-server`. First startup generates private demo tokens in `.local/tenants.json`. Later starts reuse that file and Redis's saved data. The earlier KMS exercise on port 8080 is separate.

If the service is already running, skip `make run` and use it. Starting another copy on the same ports produces a port-unavailable error.

## 2. See the shared limit in action

In a second terminal in this directory:

```sh
make demo
```

The demo waits for Tenant A's bucket to refill if necessary, then alternates requests between the two APIs. With requests arriving quickly, you should see roughly this sequence:

```text
API 1: allowed=True, remaining=9
API 2: allowed=True, remaining=8
...
API 2: allowed=True, remaining=0
API 1: allowed=False, retry≈500ms
API 2: allowed=False, retry≈500ms
```

The shared allowance is 10 total, not 10 per API. Tenant B still has its own allowance. After a short wait, Tenant A can make another request because tokens have replenished.

The exact demo output depends on elapsed time: if execution is slow, extra tokens can refill between calls. The automated contention test controls this issue by using a much slower refill rate.

## 3. Call the APIs yourself

Load Tenant A's generated token into a shell variable:

```sh
TOKEN=$(python3 -c 'import json; print(json.load(open(".local/tenants.json"))[0]["token"])')
```

This bearer token is a credential: whoever presents it acts as Tenant A. The service finds the tenant from the token; callers cannot choose another tenant by adding `tenant_id` to the body.

Evaluate one request:

```sh
curl -sS http://127.0.0.1:8081/v1/evaluate \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"cost":1}'
```

An empty object `{}` also means cost 1. A cost of 3 requires three available tokens and consumes all three only if the request is allowed. Cost must be an integer from 1 through that tenant's capacity.

Example response, starting with a full bucket:

```json
{
  "allowed": true,
  "tenant_id": "tenant-a",
  "capacity": 10,
  "refill_per_second": 2,
  "remaining": 9,
  "retry_after_ms": 0,
  "full_after_ms": 500,
  "request_id": "api-1-boot-id-1"
}
```

| Field | Meaning in this example |
|---|---|
| `allowed` | The caller may proceed with the requested operation |
| `capacity` | At most 10 tokens can accumulate |
| `refill_per_second` | Two tokens replenish each second |
| `remaining` | Nine whole tokens remain after this decision |
| `retry_after_ms` | No waiting is needed because this request was allowed |
| `full_after_ms` | With no more spending, the bucket becomes full in 500 ms |
| `request_id` | An identifier for discussing or correlating this particular request |

Redis retains fractional tokens. If 0.8 tokens remain, the API displays `remaining: 0`, but that fraction still counts toward the next refill.

A denied decision has `allowed: false`. For example, with zero tokens and a refill rate of two per second, a one-token request gets a retry estimate of about 500 ms. This is an estimate, not a reservation: another request may spend the replenished token first.

### Why a denied decision still returns HTTP 200

This service answers whether an operation is allowed; it does not execute or proxy the business operation.

```text
Caller asks the rate limiter: “May I proceed?”
Rate limiter answers successfully: HTTP 200, allowed=false
Caller or gateway rejects the business request: HTTP 429
```

If Redis is unavailable, the limiter returns 503 because it could not safely answer. The caller must treat that as no permission to proceed. The service cannot enforce a limit on callers that ignore its decisions.

### Inspect quota without spending it

```sh
curl -sS http://127.0.0.1:8082/v1/quota \
  -H "Authorization: Bearer $TOKEN"
```

This returns the caller's current bucket snapshot. It omits `allowed` and `retry_after_ms`, consumes no tokens, and does not update Redis state or extend its expiry. Asking twice does not spend two tokens. The snapshot can change immediately as other requests arrive.

### Check health and metrics

```sh
curl http://127.0.0.1:8081/healthz
curl http://127.0.0.1:8082/readyz
curl -sS http://127.0.0.1:8081/metrics \
  -H "Authorization: Bearer $TOKEN"
```

| Endpoint | Question it answers | Authentication |
|---|---|---|
| `GET /healthz` | Is the API process responding? | None |
| `GET /readyz` | Can it successfully PING Redis? | None |
| `GET /metrics` | How many requests did this API allow, deny, or fail due to store errors? | Bearer token |

When Redis is down, liveness can be 200 while readiness is 503. The API process is alive, but cannot make decisions. A successful readiness check only verifies Redis connectivity; it does not validate every tenant's stored state or policy.

Metrics belong to each API process. If API 1 reports six allowed decisions and API 2 reports four, the total is ten. Monitor both instances. Counters reset on process restart; Redis quota does not reset just because an API restarts.

Metrics use Prometheus text format, which monitoring tools can collect. They avoid a separate label for every tenant, which would create many distinct time series. For this demo any valid tenant can read aggregate metrics; a production system should reserve that access for operators. Latency histograms and distributed tracing are not implemented.

### Error responses

| Status | Example cause | What the caller should understand |
|---|---|---|
| 400 | Invalid cost, malformed JSON, unexpected body fields or query parameters | Fix the request |
| 401 | Missing or invalid bearer token | Valid credentials are required |
| 413 | Body exceeds 4096 bytes | Send a smaller body |
| 415 | Evaluate request is not `application/json` | Set the correct content type |
| 503 | Redis failure, invalid stored state, or policy mismatch | No permission was granted; consumption may still have occurred if the reply was lost |

Evaluate and quota responses handled by the application include `X-Request-ID` and `X-Instance-ID`. A request ID helps identify a call; it does not make retries free of duplicate consumption. See the lost-response example in [ARCHITECTURE.md](ARCHITECTURE.md).

## 4. Run and understand the tests

```sh
make test
```

The test starts its own Redis and two API processes on temporary ports. It uses disposable data, so it does not alter the running demo.

| Test | What it demonstrates |
|---|---|
| Exhaust a full bucket | Requests cannot consume more than available capacity |
| Advance controlled time | Refill, fractions, retry estimates, and the capacity cap work |
| Move the clock backward | The same time interval is not counted twice |
| Inspect and expire state | Inspection spends nothing; state disappears only after safe refill |
| Send 80 concurrent requests to two APIs | Both processes share a single allowance of 10 |
| Use another tenant | Tenant B does not spend Tenant A's tokens |
| Restart an API | Allowance remains in Redis |
| Start an API with conflicting policy | Existing stored policy produces a 503 mismatch |
| Stop and restart Redis | Requests fail closed during outage and connections recover afterward |

For the 80-request test, the refill rate is 0.001 token/second: one whole token every 1000 seconds. The test verifies it completes before that interval. Otherwise, expecting exactly ten successes could be wrong because more tokens might refill during the test.

For precise time tests, only the Lua script's time-read expression is replaced with a controlled test clock. The algorithm stays the same. Production offers no caller-supplied clock override.

Test Redis disables persistence. Its restart test proves recovery after a store outage, not preservation of quotas after Redis data loss. Demo Redis enables persistence, but crash durability has limits explained in the architecture guide. Input validation, weighted costs, metric authentication, and unique request IDs are also covered.

## 5. Build on a fresh machine

| Requirement | Used for |
|---|---|
| C/C++17 compiler and Make | Build the API and its Redis client library |
| Python 3 | Startup scripts, embedding Lua, demo, and tests |
| Redis 7.x | Shared quota storage and atomic evaluation |
| curl | Optional manual API calls and Redis bootstrap download |

The HTTP, JSON, and Redis client libraries are included in `vendor/`; building the API needs no downloads. Redis itself must be installed or built separately.

Inside an Ubuntu interview container, work under `/workspaces` as HR requested. With permission to install packages:

```sh
sudo apt-get update
sudo apt-get install -y build-essential python3 redis-server
make
make test
make run
```

On macOS, Xcode command-line tools provide the compiler and Make. If Redis is missing:

```sh
./scripts/bootstrap-redis.sh
make run
```

The script downloads Redis 7.2.7, checks a fixed archive checksum, and builds it within `.local` without sudo. The version is pinned for repeatable practice; it is not a recommendation for a public production deployment. To use another executable, set `REDIS_SERVER=/absolute/path/redis-server` for the startup script or tests.

### GitHub Actions CI

CI means checking changes automatically after a push or pull request. The supplied workflow:

1. Checks out the repository on Ubuntu.
2. Installs build tools and Redis.
3. Compiles the C++ service.
4. Runs the same tests, including the two-instance test.
5. Builds the Docker image, starts the Compose stack, and runs the container smoke test.

Place this project's contents at the repository root so GitHub finds `.github/workflows/ci.yml`. It needs no deployment credentials. It checks the build and tests; it does not deploy the service. The project is published at https://github.com/gauravgarg060/distributed-rate-limiter and has a [successful GitHub Actions run](https://github.com/gauravgarg060/distributed-rate-limiter/actions/runs/36593584252).

### Run with Docker Compose

Docker Desktop provides the local container engine. Compose starts Redis and both API containers together:

```sh
make docker-up
make demo
make docker-test
make docker-down
```

Use this instead of `make run` when you want containers. The default API ports are the same, so stop the native stack first or choose alternate ports. See [DOCKER.md](DOCKER.md) for networking, credential permissions, persistent storage, logs, and exact commands.

## 6. Configuration and troubleshooting

The local supervisor sets up both instances for you. When launching `build/rate-limiter` directly, these variables control it:

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | 8081 | This API's listening port |
| `BIND_HOST` | 127.0.0.1 | Which network interfaces accept API connections |
| `TENANTS_FILE` | .local/tenants.json | File mapping tokens to tenant policies |
| `REDIS_HOST` | 127.0.0.1 | Shared Redis host |
| `REDIS_PORT` | 6379 | Redis port; supervisor uses 6380 instead |
| `REDIS_PASSWORD` | empty | Optional password for an already-protected Redis server |
| `KEY_PREFIX` | rl-demo | Prefix for Redis keys; replicas must agree |
| `INSTANCE_ID` | api-1 | A distinct label for each API instance |

Setting `REDIS_PASSWORD` on the API does not configure a password on Redis. The local supervisor starts a loopback-only Redis without a password, so leave this variable unset for that setup.

Tenant IDs, prefixes, and instance IDs accept 1–64 alphanumeric, hyphen, or underscore characters. Tokens must be unique and 24–256 bytes long. Capacity accepts 1–1,000,000; refill rate accepts 0.001–1,000,000 tokens/second. Up to 1000 tenant policies are supported. Tokens and policy contents are not logged.

To choose other local ports:

```sh
LOCAL_REDIS_PORT=6381 API_PORT_A=8091 API_PORT_B=8092 make run
# In the second terminal:
API_PORT_A=8091 API_PORT_B=8092 make demo
```

| Symptom | Check |
|---|---|
| Port unavailable | An existing stack may already be running; reuse it, stop it, or select other ports |
| Redis executable missing | Install Redis or run the bootstrap script |
| Startup failed | Validate the policy file, required fields, unique tokens/IDs, and port configuration |
| 401 | Load the token from the same tenant file used by the running API |
| 503 | Check `/readyz`, Redis logs at `.local/redis.log`, and policy agreement |
| Fewer tokens than expected | Another request may have consumed them; API instances share the same bucket |
| Different allowance on each API | Verify Redis host, port, key prefix, tenant identity, and policies match |

## 7. Find your way through the code

| File | Responsibility |
|---|---|
| `src/main.cpp` | HTTP routing, authentication, validation, configuration, metrics, shutdown |
| `src/redis_store.hpp` | Manage Redis connections and replies; invoke the script |
| `src/bucket.lua` | Calculate refill, allow/deny, consume tokens, and set expiry atomically |
| `scripts/embed.py` | Put the Lua source into a generated C++ header during the build |
| `scripts/start-local.py` | Start, monitor, and stop Redis and both APIs |
| `scripts/demo.py` | Show shared limits, independent tenants, and refill |
| `tests/integration.py` | Controlled-time tests and real multi-process API tests |
| `.github/workflows/ci.yml` | Automated build, tests, and container build |

The executable contains the Lua script, so it does not need to read `src/bucket.lua` at runtime. Editing that file and running `make` embeds the new version.

## 8. Scope and next steps

The working core covers shared limits, tenant identity, quota visibility, contention tests, CI configuration, and documentation. Before production use, the biggest work is identity/TLS, Redis availability and durability, safe retries, live policy changes, and load testing. [ARCHITECTURE.md](ARCHITECTURE.md) explains each gap with a failure example.

Dependencies: cpp-httplib 0.26.0 and nlohmann/json 3.12.0 use the included MIT licenses; hiredis 1.2.0 uses `vendor/hiredis/COPYING` (BSD).

AI assistance: Codex generated the implementation and documentation and ran the native build/tests. This was not authored in Cursor. For interview preparation, review the code and practice explaining or modifying it in Cursor yourself.
