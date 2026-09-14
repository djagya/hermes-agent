#!/usr/bin/env bash
# Measure a loaded Hermes image against image-budget.json.
# Bind-mount leftovers are root-owned; wipe them from inside the
# image before the host rm (GHA failed with Operation not permitted).
set -euo pipefail

IMAGE="${IMAGE:?IMAGE is required}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export IMAGE_BUDGET="${IMAGE_BUDGET:-$ROOT/docker/sera-toolbox/image-budget.json}"

scratch=""
shut_home=""
gw=""

wipe_bind() {
  local dir="${1:-}"
  [ -n "$dir" ] && [ -d "$dir" ] || return 0
  docker run --rm --user 0 --network none -v "$dir:/wipe" \
    --entrypoint sh "$IMAGE" -c 'find /wipe -mindepth 1 -delete' >/dev/null 2>&1 || true
  rm -rf "$dir" 2>/dev/null || sudo rm -rf "$dir"
}

cleanup() {
  if [ -n "${gw:-}" ]; then
    docker rm -f "$gw" >/dev/null 2>&1 || true
    gw=""
  fi
  wipe_bind "$scratch"
  wipe_bind "$shut_home"
  scratch=""
  shut_home=""
}
trap cleanup EXIT

bytes="$(docker image inspect --format '{{.Size}}' "$IMAGE")"
echo "image_bytes=$bytes"
echo "image_mib=$((bytes / 1024 / 1024))"
docker history --format '{{.Size}}\t{{.CreatedBy}}' "$IMAGE" | head -40

largest="$(docker history --format '{{.Size}}' "$IMAGE" | python3 -c '
import sys
def parse(s):
    s = s.strip()
    if not s or s in ("0B", "0"):
        return 0
    units = (("TB", 10**12), ("GB", 10**9), ("MB", 10**6), ("kB", 1000), ("KB", 1000), ("B", 1))
    for unit, mul in units:
        if s.endswith(unit):
            return int(float(s[: -len(unit)]) * mul)
    return 0
print(max(parse(line) for line in sys.stdin))
')"

time_ms() {
  local start end
  start="$(date +%s%N)"
  "$@" >/dev/null
  end="$(date +%s%N)"
  echo $(( (end - start) / 1000000 ))
}

cold_ms="$(time_ms docker run --rm --network none --entrypoint /opt/hermes/.venv/bin/hermes "$IMAGE" --help)"
warm_ms="$(time_ms docker run --rm --network none --entrypoint /opt/hermes/.venv/bin/hermes "$IMAGE" --help)"
cid="$(docker run -d --network none --entrypoint sleep "$IMAGE" 20)"
sleep 2
rss_raw="$(docker stats --no-stream --format '{{.MemUsage}}' "$cid" | awk '{print $1}')"
docker rm -f "$cid" >/dev/null
rss_kb="$(RSS_RAW="$rss_raw" python3 -c '
import os
s=os.environ["RSS_RAW"].strip()
units=(("GiB",1024*1024),("MiB",1024),("KiB",1),("GB",1000000),("MB",1000),("kB",1),("B",1/1024))
for u,m in units:
    if s.endswith(u):
        print(int(float(s[:-len(u)])*m)); break
else:
    raise SystemExit("unparsed rss "+s)
')"

scratch="$(mktemp -d)"
docker run --rm --network none -v "$scratch:/opt/data" \
  --entrypoint hermes-image-doctor "$IMAGE" --check >/dev/null || true
cache_bytes="$(du -sb "$scratch" | awk '{print $1}')"
wipe_bind "$scratch"
scratch=""

shut_home="$(mktemp -d)"
gw="$(docker run -d --network none \
  -e HERMES_REQUIRE_DATA_MOUNT=1 \
  -v "$shut_home:/opt/data" \
  "$IMAGE" gateway run)"
for _ in $(seq 1 90); do
  if docker exec "$gw" /command/s6-svstat /run/service/gateway-default 2>/dev/null | grep -q up; then
    break
  fi
  sleep 1
done
docker exec -d "$gw" sleep 15 || true
shut_start="$(date +%s%N)"
docker stop -t 90 "$gw" >/dev/null
shut_end="$(date +%s%N)"
shutdown_ms=$(( (shut_end - shut_start) / 1000000 ))
docker rm -f "$gw" >/dev/null
gw=""
wipe_bind "$shut_home"
shut_home=""

echo "largest_layer_bytes=$largest cold_ms=$cold_ms warm_ms=$warm_ms rss_kb=$rss_kb cache_bytes=$cache_bytes shutdown_ms=$shutdown_ms"
IMAGE_BYTES="$bytes" LARGEST_LAYER_BYTES="$largest" \
  COLD_HELP_MS="$cold_ms" WARM_HELP_MS="$warm_ms" \
  IDLE_RSS_KB="$rss_kb" CACHE_WRITE_BYTES="$cache_bytes" \
  SHUTDOWN_MS="$shutdown_ms" \
  python3 "$ROOT/docker/sera-toolbox/check-image-budget.py"
