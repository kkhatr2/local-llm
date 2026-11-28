#!/usr/bin/env bash
# Phase 2 verification. All project commands execute inside Docker.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== MVP + Phase 2 Docker test gate =="
docker compose run --rm tests

echo "== Compose configuration (secrets not interpolated) =="
docker compose --profile serve config --no-interpolate >/dev/null

echo "== Git hygiene =="
docker compose run --rm dev git -C /workspace diff --check
docker compose run --rm dev git -C /workspace check-ignore -v \
  private.txt models/ .cache/responses/

if command -v helm >/dev/null 2>&1; then
  echo "== Helm =="
  helm lint helm/local-llm
  helm template phase2 helm/local-llm \
    --set gateway.enabled=true \
    --set image.flavor=cpu \
    --set modelSupply.syncEnabled=false >/dev/null
  helm install phase2-dry helm/local-llm --dry-run=client \
    --disable-openapi-validation \
    --set gateway.enabled=true >/dev/null
else
  echo "helm not found; static chart checks still ran in the Docker test gate"
fi

if command -v terraform >/dev/null 2>&1; then
  echo "== Terraform =="
  terraform -chdir=terraform/aws init -backend=false -input=false
  terraform -chdir=terraform/aws validate
else
  echo "terraform not found; static module checks still ran in the Docker test gate"
fi
