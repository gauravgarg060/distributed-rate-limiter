# Review against the distributed rate limiter brief

## Overall assessment

This is a strong interview implementation of a **horizontally replicated decision API with
shared quota state**. The important concurrency claim is implemented correctly: every replica
runs the complete token-bucket read, refill, decision, and update in one atomic Redis Lua script.
The integration suite proves that two independent API processes cannot spend the same tokens.

It is not yet a production-ready 1M-RPS, highly available, multi-region rate limiter. The API
tier can scale horizontally, but the supplied deployment has one Redis primary, static local
policy files, no Redis Cluster/Sentinel client, and no load-test evidence. Present those as
deliberate scope limits rather than implying that atomic Lua alone solves distributed failover.

## Findings

### High: the data tier does not scale horizontally or fail over

All decisions go to one configured Redis host. The client does not discover cluster nodes,
follow `MOVED` responses, or discover a promoted replica, and Compose starts one Redis process.
Adding API instances therefore cannot meet the reference target of 1M requests/second or keep
serving decisions through a Redis-node failure.

**Interview answer:** partition bucket keys by tenant across a Redis Cluster, keep all keys used
by one script in the same hash slot, and give each primary replicas with automated failover.
Benchmark script throughput and tail latency to determine shard count; do not infer capacity
from a nominal Redis operations-per-second number.

### High: the attached HTTP rejection contract is not implemented by this service

The supplied reference says excess HTTP requests receive 429 plus limit, remaining, reset, and
`Retry-After` headers. This API instead returns HTTP 200 with `allowed: false`, expecting a
gateway or application to translate the decision into 429. That is a coherent decision-service
contract, but it is not the same contract.

**Choose explicitly in the interview:**

- If the requested component is a decision service, keep 200 and document the caller's mandatory
  429 translation. This cleanly separates “decision computed and denied” from “decision could
  not be computed” (503).
- If the evaluator expects this process to enforce HTTP traffic, add a gateway/proxy endpoint or
  change denial responses to 429 with `Retry-After` and standard `RateLimit-*` headers.

### Medium: policies are static and can diverge after bucket expiry

Each process loads a local tenant file once. Existing bucket state detects a capacity/rate
mismatch, but after that state expires a differently configured replica can create a bucket
under its own policy. There is no live rule update, versioned policy, endpoint-specific rule, or
global rule.

**Production direction:** use a versioned configuration service, cache policies in each API,
push or poll updates, and define how existing tokens change when capacity or refill rate changes.

### Medium: identity supports one mechanism and is demo-grade

The service maps bearer tokens to tenants with a linear scan. It does not derive identities from
validated user claims, trusted API keys, or proxy-normalized client IPs as described in the
reference design. The code clearly labels this as a demo authentication mechanism.

**Production direction:** authenticate at the gateway, construct a trusted subject such as
`tenant:user:endpoint`, and never trust a raw client-supplied tenant or forwarded-IP header
unless the trusted proxy chain has normalized it.

### Medium: the non-functional targets are not measured

The bounded worker pool, persistent per-thread Redis connections, and atomic single-round-trip
decision are sensible latency choices. However, there is no benchmark for p50/p95/p99 latency,
maximum throughput, queue saturation, Redis CPU, memory per active tenant, or hot-key behavior.
The test suite establishes correctness, not the stated `<10 ms` or `1M RPS` goals.

### Low: observability is useful but incomplete

`/v1/quota`, health/readiness, request IDs, and low-cardinality decision/error counters are good
foundations. Metrics are process-local and omit latency, queue pressure, Redis command duration,
connection failures, and policy version. Any valid tenant can read them in the demo.

## Requirement scorecard

| Requirement | Status | Evidence or gap |
|---|---|---|
| Per-tenant quotas | Met | Tenant identity selects an independent Redis bucket. |
| Consistent across API replicas | Met on one Redis primary | One Lua script atomically performs read-modify-write. |
| Evaluate-request API | Met | `POST /v1/evaluate` supports unit and weighted costs. |
| Quota observability API | Met | `GET /v1/quota` is a non-mutating snapshot. |
| Contention handling | Met and tested | 80 concurrent calls across two processes grant exactly 10 tokens. |
| Flow diagram | Met | `ARCHITECTURE.md` contains deployment and request-flow Mermaid diagrams. |
| Test coverage | Strong for core behavior | Refill, expiry, clock rollback, contention, isolation, restart, mismatch, and outage are covered. |
| CI | Met | GitHub Actions builds, runs integration tests, and smoke-tests Compose. |
| Setup and design docs | Met | README, architecture guide, Docker guide, and interview walkthrough are present. |
| Horizontal API scaling | Met | API processes are stateless with respect to quota. |
| Horizontal data scaling | Not implemented | One Redis endpoint; no cluster-aware client. |
| High availability | Not implemented | Fail-closed behavior is tested, but Redis failover is not provided. |
| 429 and rate-limit headers | Not implemented here | Expected to be performed by the caller. |
| Dynamic rules | Not implemented | Policies are loaded from local files at startup. |
| `<10 ms` and `1M RPS` | Unverified | No load or latency benchmark. |

## Interview notes

### One-minute explanation

> I built a per-tenant token-bucket decision service in C++. API replicas are stateless for quota
> purposes and share bucket state in Redis. Each request runs one Lua script that uses Redis time
> to refill, checks the weighted cost, conditionally consumes tokens, and sets a safe expiry. Redis
> executes that complete read-modify-write atomically, so separate API replicas cannot spend the
> same last token. A real two-process contention test sends 80 concurrent requests against a
> capacity of 10 and proves exactly 10 are allowed. The service fails closed when Redis cannot
> decide. The main production gaps are Redis sharding and failover, centralized live policy,
> idempotency after lost replies, production identity/TLS, and measured latency and throughput.

### Lead with these decisions

1. Clarify whether bursts are allowed and whether this is a decision service or an enforcing
   gateway. Those answers determine token bucket suitability and the 200-versus-429 contract.
2. Explain why process-local counters and process-local mutexes fail across replicas.
3. Show the race caused by separate Redis reads and writes, then show how Lua expands the atomic
   boundary over the full state transition.
4. Separate three guarantees: atomicity on one primary, durability across crashes, and
   consistency across failover. This project proves only the first.
5. Explain fail-closed as a product tradeoff: it protects downstream systems but reduces
   availability when the quota store is unavailable.

### Likely follow-up questions

**Why token bucket?** It stores constant-size state, permits a configured burst, supports weighted
costs, and enforces a sustained refill rate. It is not an exact rolling-window limit.

**Why use Redis time?** Every API replica uses one time source. Backward jumps are clamped to the
stored update time, though forward jumps remain a documented limitation.

**Why does expiry equal time until full?** Missing state means a full bucket. Earlier deletion
would recreate more tokens than the tenant had earned.

**What if Redis consumes a token but its reply is lost?** The API returns 503 and does not retry
because the outcome is unknown. Production idempotency needs a client operation ID and an atomic
record of the prior decision.

**How would this reach 1M RPS?** Benchmark one shard, shard by tenant with Redis Cluster hash
slots, provision replicas, keep gateways and Redis region-local, and monitor p99 latency and hot
tenants. Cross-region quotas are approximate unless the latency cost of global coordination is
accepted.

**How would rules change live?** Store versioned rules centrally, distribute updates by watch or
short polling, retain last-known-good configuration, and define token migration when a policy
changes.

### Demo sequence

1. Show two `/healthz` responses with distinct instance IDs.
2. Run `make demo` to show one shared allowance and tenant isolation.
3. Run `make test` and point out the two-process 80-request contention check.
4. Open the Lua script at the read-refill-check-consume-write sequence.
5. Open the architecture diagram and finish with the single-Redis and failover limitations.

## Submission checklist

- Work under `/workspaces` in the actual interview container; this review session is on macOS and
  `/workspaces` was not mounted.
- Keep the repository root layout so GitHub discovers `.github/workflows/ci.yml`.
- Push source, tests, docs, dependency licenses, and the Mermaid architecture source early.
- Confirm the remote CI run, not just the local test result.
- Keep `.local` credentials, Redis data, and generated build outputs out of the commit.
- Log out of GitHub, Cursor, and browser sessions before returning the interview machine.

## Verification performed for this review

`make test` passed on 2026-09-29, including deterministic Lua behavior, two-process contention,
authentication and validation, tenant isolation, API restart, policy mismatch, Redis outage,
readiness behavior, and recovery. PRISM was not run because this repository has no
`.prism/prompts` directory.