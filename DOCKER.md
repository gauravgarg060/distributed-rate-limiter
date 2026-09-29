# Run the service in local Docker containers

Docker runs each part of the service in its own container. Docker Compose is the configuration and command-line tool that starts those containers together.

## Start and use it

Install and open Docker Desktop first. Wait for its engine to start. Then run these commands from this project directory:

```sh
make docker-up
make docker-ps
make demo
make docker-test
```

Stop the native stack first if it already occupies ports 8081/8082. Alternatively, run the containers on other host ports:

```sh
API_PORT_A=8091 API_PORT_B=8092 make docker-up
API_PORT_A=8091 API_PORT_B=8092 make demo
API_PORT_A=8091 API_PORT_B=8092 make docker-test
```

`docker-up` prepares the configuration, builds the Linux image, starts all containers in the background, and waits for health checks. The first build needs internet access to download base images and build packages. Later builds reuse cached layers.

| Command | Purpose |
|---|---|
| `make docker-up` | Build and start the stack; safe to repeat after changes |
| `make docker-ps` | See container state and health |
| `make docker-logs` | Follow recent logs; Ctrl-C stops following, not the containers |
| `make docker-test` | Check both APIs and race them for a single shared allowance |
| `make docker-down` | Stop/remove containers and network while preserving Redis's named volume |

`make test` remains the full native test suite and requires a local compiler and Redis executable. `make docker-test` is an additional running-container smoke test, not a replacement for that full suite. Run the demo and smoke test sequentially: both spend the demo tenant's quota.

## Networking: host ports versus container ports

```text
Your Mac:8081 → API container 1:8081 ─┐
                                      ├→ redis:6379
Your Mac:8082 → API container 2:8081 ─┘
```

Each API listens on port 8081 inside its own container, so there is no conflict. Docker publishes them on different Mac ports. Both published ports bind only to `127.0.0.1`, so they are for local use.

`REDIS_HOST=redis` uses Compose's service-name lookup. `127.0.0.1` inside an API container would point back to that API container, not Redis. Redis has no published host port and is reached through the Compose network.

The API binds `0.0.0.0` inside its container so connections from that network can reach it. This does not change the localhost-only publication on your Mac.

## Credentials and permissions

Both containers mount the same `.local/tenants.json` read-only at `/config/tenants.json`. Tokens are not copied into the image. If the file already exists, its tokens are preserved; otherwise the preparation script generates random ones.

`make docker-up` writes your regular user's numeric UID and GID to `.local/docker.env`. Compose runs the API containers as that non-root user so they can read the private mounted file. Without Compose, the image defaults to user 10001. Run the Make target as your regular user, not with sudo.

The `.local` directory is excluded from Git and the Docker build context. The API container filesystem is read-only, extra Linux capabilities are dropped, and privilege escalation is disabled.

This setup uses local bearer tokens and an internal, passwordless Redis. It is a local development setup, not a remote security configuration. It does not add TLS or a full identity provider.

## Persistence and independence from the native service

Redis stores its append-only log in the named `redis-data` volume, using every-second syncing and a no-eviction memory policy. Recreating containers with `make docker-down` followed by `make docker-up` preserves that volume. Crash durability still has the limitations described in ARCHITECTURE.md.

Docker Redis has its own data, separate from native Redis on port 6380. Its key prefix is `rl-docker`; the native supervisor uses `rl-demo`. They share the credentials file but not quota state.

Do not add `--volumes` to the down command unless you deliberately want to delete saved Redis data. Normal down preserves it.

## What was added

- `compose.yaml`: Redis plus two API instances, network, storage, and health checks.
- `scripts/prepare-docker.py`: private credentials and local user mapping.
- `scripts/test-docker.py`: live-container smoke test.
- Make targets for starting, stopping, inspecting, and testing containers.
- `curl` in the runtime image for API readiness checks.

No token-bucket algorithm or HTTP endpoint behavior changed.

If Docker Desktop is installed without system-wide command links, the Make targets automatically use `/Applications/Docker.app/Contents/Resources/bin/docker`. The Makefile also adds Docker’s bundled tools to the PATH used by its commands, so credential helpers can be found. You do not need to change your shell PATH.

## Verified on this Mac

Docker Desktop is installed in `/Applications/Docker.app`. The Linux image built successfully, all three Compose services passed their health checks, and the shared-quota smoke test and demo passed. A temporary Redis record survived removing and recreating the containers with the named volume retained; the record was then removed. This checks normal volume persistence, not crash-proof or zero-loss durability. GitHub CI also passed: see the [successful GitHub Actions run](https://github.com/gauravgarg060/distributed-rate-limiter/actions/runs/36593584252).
