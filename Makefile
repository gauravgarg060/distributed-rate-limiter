# Docker invokes credential helpers by name, so its bundled tools must be on PATH.
# This only affects Make recipes; it does not modify the user's shell settings.
ifneq ($(wildcard /Applications/Docker.app/Contents/Resources/bin/docker),)
export PATH := /Applications/Docker.app/Contents/Resources/bin:$(PATH)
endif
DOCKER ?= $(shell command -v docker 2>/dev/null || printf /Applications/Docker.app/Contents/Resources/bin/docker)
CXX = c++
CXXFLAGS = -std=c++17 -O2 -Wall -Wextra -Wpedantic -pthread
.PHONY: all test run demo clean
all: build/rate-limiter
build/bucket_script.hpp: src/bucket.lua
	mkdir -p build
	python3 scripts/embed.py
vendor/hiredis/libhiredis.a:
	$(MAKE) -C vendor/hiredis libhiredis.a
build/rate-limiter: src/main.cpp src/redis_store.hpp build/bucket_script.hpp vendor/hiredis/libhiredis.a vendor/httplib.h vendor/json.hpp
	$(CXX) $(CXXFLAGS) -Ivendor -Ivendor/hiredis -Ibuild src/main.cpp vendor/hiredis/libhiredis.a -o $@
test: all
	python3 tests/integration.py
run: all
	python3 scripts/start-local.py
demo:
	python3 scripts/demo.py
clean:
	rm -rf build
	$(MAKE) -C vendor/hiredis clean

.PHONY: docker-up docker-down docker-logs docker-ps
docker-up:
	python3 scripts/prepare-docker.py
	$(DOCKER) compose --env-file .local/docker.env up --build -d --wait
docker-down:
	$(DOCKER) compose --env-file .local/docker.env down
docker-logs:
	$(DOCKER) compose --env-file .local/docker.env logs --tail=100 -f
docker-ps:
	$(DOCKER) compose --env-file .local/docker.env ps
.PHONY: docker-test
docker-test:
	python3 scripts/test-docker.py
