#!/usr/bin/env bash
# Build the project-user layer (docker/project-user/Dockerfile) on the pinned
# benchmark image, then check the result.
#
# Usage: scripts/build_project_user_image.sh [TAG]
#   TAG defaults to sag-advisor-five-cold-user:20261021.
#
# Refuses to build while any container is running or while the campaign status
# file named by SAG_CAMPAIGN_STATUS (default: the test-phase-reuse campaign in
# the main checkout's output/) says "running": a concurrent build competes with
# the benchmark for CPU, memory and disk.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MAIN="$(cd "$(git -C "$ROOT" rev-parse --git-common-dir)/.." && pwd)"
BASE_TAG="sag-advisor-five-cold:20260926"
BASE_ID="sha256:a2d28950852af85d9b64d0e4ee015d989e88e83d9beb71c8ac5a166c41b8e1cc"
TAG="${1:-sag-advisor-five-cold-user:20261021}"
STATUS="${SAG_CAMPAIGN_STATUS:-$MAIN/output/test-phase-reuse-20261021/status.json}"

if [ -n "$(docker ps -q)" ]; then
    echo "Containers are running; not building while they are up." >&2
    exit 1
fi
if [ -f "$STATUS" ]; then
    state="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("state"))' "$STATUS")"
    if [ "$state" = "running" ]; then
        echo "The campaign in $STATUS is still running; not building." >&2
        exit 1
    fi
fi

actual="$(docker image inspect --format '{{.Id}}' "$BASE_TAG")"
if [ "$actual" != "$BASE_ID" ]; then
    echo "$BASE_TAG is $actual, not the pinned $BASE_ID." >&2
    exit 1
fi

docker build --platform linux/amd64 \
    --build-arg "BASE_IMAGE=$BASE_TAG" \
    -t "$TAG" \
    -f "$ROOT/docker/project-user/Dockerfile" \
    "$ROOT/docker/project-user"

echo "== checks inside $TAG"
docker run --rm --platform linux/amd64 "$TAG" sh -c '
set -e
id sag
test -x /usr/bin/setpriv && echo "setpriv: /usr/bin/setpriv"
/usr/bin/setpriv --reuid=sag --regid=sag --init-groups sudo -n true && echo "sudo -n true as sag: ok"
echo "/opt/sag-maven-observations mode: $(stat -c %a /opt/sag-maven-observations)"
echo "safe.directory: $(git config --system --get-all safe.directory)"
for f in /etc/mavenrc /opt/sag-maven-witness/launch.py /opt/sag-maven-witness/witness.jar /opt/sag-maven-witness/policy.json; do
    /usr/bin/setpriv --reuid=sag --regid=sag --init-groups test -r "$f" && echo "readable by sag: $f"
done
'

echo "== witness policy files (base, then $TAG)"
policies='find /opt /etc -name policy.json -type f -exec sha256sum {} + | sort -k2'
base_hashes="$(docker run --rm --platform linux/amd64 "$BASE_TAG" sh -c "$policies")"
new_hashes="$(docker run --rm --platform linux/amd64 "$TAG" sh -c "$policies")"
echo "$base_hashes"
echo "$new_hashes"
if [ "$base_hashes" != "$new_hashes" ]; then
    echo "The witness policy files differ from the base image." >&2
    exit 1
fi

echo "== image"
docker image inspect --format '{{.Id}}' "$TAG"
docker image inspect --format '{{range .RootFS.Layers}}{{println .}}{{end}}' "$TAG"
