#!/usr/bin/env bash
# Build the layered orion-erpnext image on kvm4 and roll the stack onto it.
# Usage: deploy/deploy.sh <image-tag-suffix>   e.g. deploy/deploy.sh orion1
# Run from the repo root on the workstation.
set -euo pipefail

SUFFIX=${1:?usage: deploy.sh <tag-suffix>}
ERPNEXT_VERSION=v16.30.0
TAG="orion-erpnext:${ERPNEXT_VERSION}-${SUFFIX}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

rsync -a --delete --exclude .git "$ROOT/orion" "$ROOT/deploy/Dockerfile" kvm4:/tmp/orion-build/

ssh kvm4 "
  set -e
  sudo -n docker build -q -t $TAG --build-arg ERPNEXT_VERSION=$ERPNEXT_VERSION /tmp/orion-build
  sudo -n sed -i 's|^ERPNEXT_IMAGE=.*|ERPNEXT_IMAGE=orion-erpnext|; s|^ERPNEXT_VERSION=.*|ERPNEXT_VERSION=${ERPNEXT_VERSION}-${SUFFIX}|' /opt/erpnext/.env
  sudo -n docker compose --project-directory /opt/erpnext up -d 2>&1 | tail -3
  sleep 15
  for s in erp.ratunda.id stg-erp.ratunda.id; do
    sudo -n docker compose --project-directory /opt/erpnext exec -T backend bash -c \
      \"bench --site \$s list-apps | grep -q '^orion' || bench --site \$s install-app orion\"
    sudo -n docker compose --project-directory /opt/erpnext exec -T backend bench --site \$s migrate 2>&1 | tail -2
  done
"
echo "deployed $TAG"
