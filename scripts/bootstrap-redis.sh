#!/bin/sh
# Optional local Redis build; uses only project-local files, no sudo.
set -eu
cd "$(dirname "$0")/.."
mkdir -p .local/bootstrap .local/bin
curl --fail --location --max-time 120 https://download.redis.io/releases/redis-7.2.7.tar.gz -o .local/bootstrap/redis.tar.gz
python3 - <<'PY'
import hashlib
from pathlib import Path
p=Path('.local/bootstrap/redis.tar.gz')
assert hashlib.sha256(p.read_bytes()).hexdigest()=='72c081e3b8cfae7144273d26d76736f08319000af46c01515cad5d29765cead5', 'Redis archive checksum mismatch'
PY
tar -xzf .local/bootstrap/redis.tar.gz -C .local/bootstrap
make -C .local/bootstrap/redis-7.2.7 -j4 MALLOC=libc BUILD_TLS=no redis-server redis-cli
cp .local/bootstrap/redis-7.2.7/src/redis-server .local/bin/
cp .local/bootstrap/redis-7.2.7/src/redis-cli .local/bin/
