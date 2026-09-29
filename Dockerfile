FROM debian:bookworm-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends g++ gcc make python3 libc6-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY Makefile ./
COPY vendor ./vendor
COPY src ./src
COPY scripts/embed.py ./scripts/embed.py
RUN make -j2

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 ca-certificates curl && rm -rf /var/lib/apt/lists/* && useradd --uid 10001 --no-create-home app
WORKDIR /app
COPY --from=build /app/build/rate-limiter /app/rate-limiter
USER 10001
ENV BIND_HOST=0.0.0.0 PORT=8081 TENANTS_FILE=/config/tenants.json
EXPOSE 8081
ENTRYPOINT ["/app/rate-limiter"]
