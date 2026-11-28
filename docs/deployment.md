# Phase 2 Deployment

This document describes the implemented Kubernetes/cloud deployment path for
`DECISION-26`. Local Compose remains the loopback-only development path and is
unchanged except for additive digest metadata used by promotion tooling.

## Artifacts

| Path | Purpose |
|---|---|
| `deploy/image-digests.yaml` | Canonical CPU/CUDA digest pins. CI replaces placeholders only after signature, SBOM, and vulnerability gates pass. |
| `Dockerfile` | Digest-pinned CPU image built from `environment-linux-64.lock`. |
| `Dockerfile.cuda` | Digest-pinned CUDA image built from `environment-cuda-64.lock`. |
| `helm/local-llm/` | Provider-neutral Kubernetes chart. |
| `terraform/aws/` | AWS EKS reference module: VPC, private EKS endpoint, CPU/GPU node groups, S3 weights bucket, IRSA, Gateway release, and NetworkPolicy. |
| `deploy/supply-chain.yaml` | Cosign/Syft/Grype promotion gates. |
| `scripts/verify-phase2.sh` | Verification entry point. |

`helm/local-llm/files/local_llm.yaml` is a byte-for-byte copy of the repository
`local_llm.yaml`. The ConfigMap is rendered from that file, so runtime settings
have one human-edited source of truth.

## Images

Build only inside Docker:

```bash
docker compose build api tests dev
docker build -f Dockerfile.cuda -t local-llm-api-cuda:build .
```

Neither image copies model weights, cache entries, `private.txt`, or any secret
value. `.dockerignore` excludes those paths. The CUDA image is the only enabled
CUDA flavor; Apple Metal remains documentation-only and has no image or CI
verification claim.

Promote signed digests into `deploy/image-digests.yaml` after the gates in
`deploy/supply-chain.yaml` pass. Compose, Helm values, and Terraform defaults
must reference the same digest for the selected flavor.

## Local Compose

```bash
docker compose --profile serve config --no-interpolate
docker compose --profile serve up -d api
```

Compose publishes only `127.0.0.1:8000`. It still builds from source and keeps
the optional `private.txt` `env_file` wiring. It does not enable ingress or
object-storage sync.

## Kubernetes

```bash
helm lint helm/local-llm
helm template phase2 helm/local-llm --set gateway.enabled=true \
  --set image.flavor=cpu
helm install phase2 helm/local-llm --namespace local-llm --create-namespace \
  --set gateway.enabled=true \
  --set gateway.hostname=local-llm.example.com \
  --set gateway.certificateSecretRef=local-llm-tls \
  --set gateway.apiKeySecretRef=local-llm-api-key
```

Public ingress is rendered only when `gateway.enabled=true`. The Gateway
terminates TLS 1.2+, redirects HTTP to HTTPS, and delegates API-key validation
to the `APIKeyPolicy` extension before forwarding. Only `/health` and
`/v1/models/{alias}/chat/completions` are public; `/v1/models` and `/admin/*`
are not.

The application pod is non-root, uses a read-only root filesystem, disables
privilege escalation, drops all capabilities, and uses the RuntimeDefault
seccomp profile. Writable mounts are limited to `/app/models`,
`/app/.cache/responses`, and `/tmp`. A NetworkPolicy permits ingress only from
the configured gateway namespace, so direct pod access is denied.

Secrets are Kubernetes Secret references only:

- `local-llm-hf` key `token` optionally supplies `HF_TOKEN`.
- `local-llm-api-key` is read by the gateway, never by the application.
- `model-supply-credentials` is read only by the object-storage sync init
  container.
- TLS material comes from `gateway.certificateSecretRef`.

No secret value belongs in `local_llm.yaml`, an image layer, a CLI argument, a
manifest, or application logs.

## AWS reference

```bash
terraform -chdir=terraform/aws init
terraform -chdir=terraform/aws plan
terraform -chdir=terraform/aws apply
```

Provider and module versions are pinned. The private EKS endpoint, CPU and GPU
node groups, S3 weights bucket, IRSA role, Gateway release, and NetworkPolicy
are declared in the module. GPU node groups are tainted and require an explicit
CUDA image flavor; there is no silent CUDA-to-CPU fallback.

## Verification

```bash
bash scripts/verify-phase2.sh
```

The Docker test gate always runs. `helm` and `terraform` checks run only when
those CLIs are installed on the operator host; the Python artifact tests always
verify the committed files and digest parity.

Verification performed for this change: `docker compose run --rm tests` passed
174 tests; `docker compose --profile serve config --no-interpolate` succeeded;
`docker build -f Dockerfile.cuda -t local-llm-api-cuda:build .` succeeded from
the digest-pinned base and committed lock, and importing `llama_cpp` in that
image printed `0.3.37`; `helm lint helm/local-llm`, `helm template`, and
client-only `helm install --dry-run=client --disable-openapi-validation`
rendered 11 resources from the chart; and `terraform init -backend=false`,
`terraform validate`, and `terraform fmt -check` succeeded in Docker using
Terraform 1.9.8 with the pinned AWS provider 5.61.0. The Helm client-only dry
run used a local fake discovery endpoint and disabled OpenAPI schema validation
because no cluster schema was available. A real cluster smoke still requires a Gateway API
controller that implements the documented `APIKeyPolicy` extension and, for
CUDA, the NVIDIA device plugin. `terraform plan`, repeated-plan idempotency,
live Gateway/TLS enforcement, and supply-chain tool execution remain platform
gates when AWS credentials and a target cluster are available.

## Deferred

Application auth, rate limiting, cache encryption, shared cache, local Compose
non-root, multi-architecture locks, content-hash GGUF fingerprints, and Apple
Metal implementation/verification remain out of scope. `ASSUMPTION-17`,
`ASSUMPTION-18`, `ASSUMPTION-19`, `ASSUMPTION-21`, and `ASSUMPTION-23` are not
closed by this deployment path.
