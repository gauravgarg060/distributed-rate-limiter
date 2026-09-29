# Explain and demonstrate the rate limiter

Use this guide to practice speaking about the service. Aim to explain the behavior first, then point to the code that implements it. You do not need to memorize the wording.

## 1. Explain the project in one minute

> “I built a service that decides whether a tenant can perform an operation. Each tenant has a bucket of permission tokens. In the demo the bucket holds ten tokens and replenishes two per second.
>
> There are two C++ API instances, but both use the same Redis bucket for a tenant. A Redis Lua script calculates refill, checks the cost, and consumes tokens in one atomic operation. This prevents both instances from spending the same last token.
>
> I tested concurrent requests across both instances, tenant isolation, refill, API restarts, and Redis outages. If Redis cannot provide a decision, the API returns 503. The README explains remaining production work, including safe retries, policy updates, and Redis durability.”

Do not describe this as a gateway: it returns a decision, and the caller is responsible for enforcing it. Do not claim it is production-hardened; explain the working behavior and its limits.

## 2. Establish the requirements before choosing an algorithm

Ask the interviewer to clarify the meaning of quota. These questions affect correctness:

| Question | Why it matters |
|---|---|
| Is the quota per tenant, or per tenant and endpoint? | Determines the key used to share state |
| Are immediate bursts allowed? | Helps decide whether token bucket is appropriate |
| Does “ten per five seconds” mean a strict rolling interval? | Token bucket does not implement that exact contract |
| Do expensive operations cost more than one unit? | Determines whether weighted consumption is needed |
| Should a store outage allow requests or reject them? | Defines fail-open versus fail-closed behavior |
| Must retries count as the same logical operation? | Determines whether idempotency is required |

Our implementation assumes per-tenant token buckets, allowed bursts, positive integer costs, and fail-closed behavior. Retrying a request is a new evaluation; duplicate charging is possible after a lost reply.

## 3. Give a five-minute demo

From the project directory, start the stack if it is not already running:

```sh
make run
```

In a second terminal:

```sh
curl http://127.0.0.1:8081/healthz
curl http://127.0.0.1:8082/healthz
make demo
make test
```

| Show | Explain |
|---|---|
| Two health responses with different instance IDs | “These are two separate API processes.” |
| Alternating calls consume one allowance | “The total allowance is shared, not ten per process.” |
| Tenant B still has quota | “Each tenant has a separate Redis key.” |
| A request succeeds after waiting | “Refill allows new work without resetting the whole bucket.” |
| Two-instance concurrency test passes | “This verifies the race between separate processes.” |
| Architecture and CI files | “These describe the design and automate verification.” |

The interactive demo refills while it runs, so do not promise an exact count under arbitrary delays. The automated test uses very slow refill to make its exact ten-success assertion meaningful.

## 4. Walk through one request in the code

Start in `src/main.cpp` and follow this path:

1. **Authenticate:** match the bearer token to a server-configured tenant. Explain why the caller cannot supply a different tenant ID.
2. **Validate:** accept `{}` or a valid integer cost. Reject unexpected fields and costs above capacity.
3. **Call Redis:** pass the tenant key, configured capacity/rate, cost, and consume mode to `RedisStore`.
4. **Execute Lua:** open `src/bucket.lua`; explain refill, decision, state update, and expiry.
5. **Return:** show `allowed`, remaining tokens, and wait estimates. Explain 200 versus 503.

Then open `src/redis_store.hpp` to explain worker-owned connections and resource cleanup. If asked about tests, show the 80 concurrent requests distributed across two API ports in `tests/integration.py`.

## 5. Practice the main design questions with examples

### Why does this need Redis rather than an in-memory map?

An in-memory map in API 1 is separate from the map in API 2. If each stores ten tokens, the tenant can spend twenty by contacting both. Redis provides one shared state for both APIs.

A local mutex protects only threads inside its own process. It cannot coordinate another process's independent map.

### Why not read and write Redis using separate commands?

With one token remaining, both API instances could read one before either writes zero. Both would allow a request. The individual commands may be atomic, but the whole decision is not.

The Lua script makes the read, refill, check, and consumption one non-interleaved operation. One request consumes the last token; the other then sees the updated state.

### What exactly does capacity ten and rate two mean?

A full bucket allows ten immediately. Once empty, it replenishes one token every half second. After a long idle period it holds only ten, because capacity caps accumulation.

This supports a burst plus sustained rate. It does not enforce at most ten requests in every rolling five-second interval. Say this explicitly rather than treating the two contracts as interchangeable.

### Is refill performed by a background thread?

No. The next request calculates how many tokens should have accumulated since the previous timestamp.

For example, two tokens left plus three seconds at two per second gives eight tokens. A cost-one request leaves seven. This avoids running a timer for every tenant.

### Why can a denied request still write state?

Suppose zero tokens were stored and 0.2 seconds pass. Now there are 0.4 tokens. A cost-one request is denied, but storing 0.4 and the new timestamp correctly records refill. The denial did not subtract anything.

### Why does expiry depend on time until full?

A missing Redis entry is treated as a full bucket. If two tokens remain and refill is two per second, four seconds are needed to reach ten. Deleting after one second would replace the correct four-token state with ten tokens. Deleting after four seconds is safe because both states mean ten tokens.

If asked about clock delay, add: when the clock is two seconds behind the stored timestamp, we include that catch-up delay before the four seconds of refill.

### Why use Redis time?

Both API instances use the same clock source for decisions. If they independently used their own clocks, a machine running ahead could refill earlier.

Redis's clock can still move. We clamp backward movement against the last update so we do not count the same interval twice. Forward jumps remain a limitation and are documented.

### What does a quota read guarantee?

It returns a snapshot without spending tokens. If it shows three available, another concurrent request can spend them immediately afterward. Reading quota is not reserving it.

### Why return 200 when a decision denies a request?

The evaluation itself succeeded: the answer is no. A gateway would translate that into 429 for the protected business request. A 503 means the service could not safely obtain a decision, which is different from a computed denial.

### What happens if Redis goes down?

We fail closed: return 503 and do not grant permission. That protects the quota at the cost of availability. The caller must also honor that response.

Liveness can remain 200 because the API process is still responding. Readiness becomes 503 because its Redis PING fails.

### Why not retry Redis automatically after a timeout?

Redis may have consumed a token and then lost the reply. Retrying could consume another token for the same intended operation. We report failure without replaying the uncertain consumption.

A request ID in the response only identifies the request. A true idempotency mechanism would require a stable client operation ID and an atomically stored previous decision, with defined retention. That extension is not implemented.

### What survives an API restart? What about a Redis restart?

An API restart leaves the shared Redis quota intact, though that API's metrics reset. Redis data loss can make depleted buckets appear new and full.

The local demo uses an append-only log with every-second syncing, which reduces restart loss but does not guarantee zero loss during a crash. The automated Redis recovery test disables persistence and proves reconnection, not quota durability.

### Does atomic Lua make failover perfectly consistent?

No. Atomicity prevents two operations from interleaving on the current primary. It does not ensure every write reaches a replica or disk before failure. A replacement Redis primary could lack recent consumption and grant extra allowance.

Separate the claims: concurrency correctness is tested; zero-loss failover is not implemented or demonstrated.

### What if replicas use different policies?

Existing state stores capacity and rate, so a replica with different values gets 503. But after the entry expires, those stored values disappear. The next replica could create a bucket with its own policy.

Therefore, all replicas must use matching policies and namespace. Live policy updates need shared versioned configuration and rules for adjusting existing tokens.

### Why does each worker own a Redis connection?

A hiredis context is not safe for concurrent use by multiple threads. Each worker reuses its own connection. This avoids sharing a context or forcing all workers through a single client-side lock. Redis still serializes the atomic script execution.

RAII ownership cleans up connections and replies, even when errors throw exceptions.

### What fails first as traffic grows?

The answer needs measurement. Likely constraints include Redis script throughput, the eight API workers, network latency, and the bounded queue. Adding API replicas does not remove the single Redis bottleneck.

First measure throughput and tail latency. Then consider connection/command optimizations or distributing tenants across Redis servers. One very busy tenant can still be a concentrated source of contention.

### How did you prove distributed correctness?

The test starts real Redis and two independent API processes, then sends 80 concurrent requests across both. Capacity is ten; refill is one token per 1000 seconds. It asserts completion before that refill interval and exactly ten allowed decisions.

That is stronger evidence than testing a single process with a mutex, and the timing condition explains why the exact expected count is valid.

## 6. Fit the explanation into thirty minutes

| Time | Focus |
|---|---|
| 0–5 minutes | Demo, requirement assumptions, and what works |
| 5–12 minutes | Shared state, token bucket example, and the atomic script |
| 12–18 minutes | Contention tests, expiry, clocks, and C++ ownership |
| 18–25 minutes | Redis failures, lost replies, policy mismatch, and production gaps |
| 25–30 minutes | Questions and which improvement you would prioritize next |

If the interviewer asks a question early, follow their interest. Use this as a preparation outline, not a script that must be completed in order.

## 7. Use Cursor in reviewable steps

Give it a bounded task, inspect its changes, and test the behavior before asking for more.

| Stage | Example prompt |
|---|---|
| Plan | “Propose the smallest C++ API with a shared Redis token bucket. State assumptions and identify the atomic operation.” |
| Implement | “Implement refill/check/consume in one Lua script. Preserve fractions, validate before writing, and expire only when full.” |
| Test | “Test two independent API processes racing for one bucket. Explain why refill during execution cannot invalidate the expected count.” |
| Review | “Review for tenant spoofing, unsafe shared Redis connections, duplicate charging on retries, clock errors, and premature expiry.” |
| Document | “Explain what was verified, what the API promises, and where it falls short. Do not claim production guarantees we have not tested.” |

Be ready to explain a generated decision you reviewed or corrected. The useful evidence is your reasoning and tests, not the size of the AI-generated change.

## 8. Submit a complete, honest result

HR's sample instructions say to work inside `/workspaces` in the interview container, push code plus the architecture diagram before time expires, and log out of personal accounts afterward.

Before the deadline:

- Put the project at the repository root so GitHub discovers `.github/workflows/ci.yml`.
- Include README, architecture diagrams, source, tests, and dependency licenses.
- Keep `.local` tokens, Redis data, and native build outputs out of Git using `.gitignore`.
- Push a working version early and update it as you finish.
- Check the actual CI result; a workflow file alone does not prove CI passed.
- Document any incomplete work or unverified deployment path.
- Log out of GitHub, Cursor, and browser accounts before returning the interview machine.

For this prepared project, native build/tests, the Docker image build, container health, shared-quota smoke test, and demo were verified. GitHub CI also passed: see the [successful GitHub Actions run](https://github.com/gauravgarg060/distributed-rate-limiter/actions/runs/36593584252). Local deployment meets the clarification you received from the team.
