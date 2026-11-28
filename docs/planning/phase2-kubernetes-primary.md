# Phase 2: Kubernetes Primary Cloud Target — Architecture & Feasibility Recommendation

Status: **proposed; supporting document only**. The authoritative Phase 2 scope is `docs/requirements.md` under `DECISION-26` and `AC-81`–`AC-167`. This record captures the tech-lead feasibility review; no production code, `local_llm.yaml` key, image, Helm chart, or Terraform module is implemented by it.

## Scope

Primary cloud target: provider-neutral Kubernetes, with AWS EKS as the first concrete Terraform reference. Compute backends: NVIDIA CUDA for local and cloud, explicit CPU selection available everywhere with no silent fallback, and Apple Metal local support recorded as documentation-only in Phase 2. Model supply: Hugging Face CLI download into a volume, plus optional object-storage sync. Per-instance local cache and model residency, with no shared cache across pods. Secrets come from Kubernetes Secret primitives/workload identity; cloud pods use non-root users and read-only root filesystems. One digest-promoted OCI artifact per compute flavor (CPU and CUDA) is shared by Compose and Helm for the selected flavor. Local Compose deployment continues to work unchanged on `127.0.0.1:8000`.

## 1. Configuration additions to `local_llm.yaml`

Preserve every current key (`server`, `paths`, `llama_defaults`, `generation_defaults`, `max_n_ctx`, `cache`, `download`, `resources`, `models`); additions are additive new top-level sections. OpenCode V2 behavior is unchanged (`limit` singular, `body.options` only; no `limits` or top-level `options`). These fields are planned only and are not present in the repository config.

```yaml
# --- existing keys unchanged ---

deployment:
  primary_target: kubernetes        # kubernetes | compose
  local_mode: true                 # true preserves Compose / loopback behavior
  terraform_reference: aws         # AWS EKS first; other providers are later adapters
  ingress:
    enabled: false                 # true under Helm; false under Compose
    gateway_policy: api_key        # enforced by the gateway, not by the app
    api_key_secret_ref: local-llm-api-key
    publish: loopback              # Compose: loopback; Helm: public via ingress

compute:
  default_backend: cpu
  active_backends: [cpu, cuda]     # Metal is documentation-only in Phase 2
  documented_backends: [metal]
  device_placement: auto           # auto | manual
  fallback: fail_closed            # CPU fallback requires explicit opt-in
  backend_defaults:
    cpu:
      n_gpu_layers: 0
    cuda:
      n_gpu_layers: 33
    metal:
      n_gpu_layers: 1              # documented reference only

registry:
  supply: hf                       # hf | object_storage | volume
  object_storage:
    provider: s3
    bucket: local-llm-models
    prefix: gguf/
    region: us-east-1
    secret_ref: model-supply-credentials
  sync:
    enabled: false                 # initContainer sync before the app starts
    immutable_weights: true
    concurrency: 4

cache:
  scope: per_instance              # not shared across pods
  dir: .cache/responses            # unchanged
  ttl_days: 30                     # unchanged
  max_size_mb: 1024                # unchanged

security:
  non_root: true                   # cloud target; local Compose deferred
  read_only_root: true             # cloud target; models/cache are writable mounts
  secrets_backend: kubernetes_secret
```

New config-model fields are owned by `ConfigLoader` (C2). Validation rules: `primary_target` in `{kubernetes, compose}`; `gateway_policy` must be `api_key`; `active_backends` is a non-empty subset of `{cpu, cuda}`; `metal` is accepted only in `documented_backends`; `fallback` is `fail_closed` in Phase 2; `supply` in `{hf, object_storage, volume}`; `secret_ref` non-empty when `supply != hf` or ingress is enabled; `cache.scope` is `per_instance`; `secrets_backend` is `kubernetes_secret`. Startup failures name the offending field. Effective llama options keep the existing merge order with backend defaults inserted once: `llama_defaults` → `compute.backend_defaults[selected_backend]` → model `options` → request `options`.

## 2. Module boundaries / ownership

| Module | Public surface | Owns | Injected collaborators | Edge |
|---|---|---|---|---|
| `compute/backend_selection.py` (new) | `BackendSelector.select(settings, probes) -> BackendDecision` | Backend preference and ordered fallback chain; one decision per call | Fake backend probes | No direct I/O |
| `compute/device_placement.py` (new) | `DevicePlacement.validate(backend, resources) -> Placement` | GPU-memory estimate, `n_gpu_layers` mapping, placement legality | Resource thresholds | Memory/disk probes injected |
| `runtime/adapter.py` (extended) | `LlamaRuntimeAdapter` gains a backend decision | Backend-specific llama options; one resident instance per process | llama-cpp runtime injected | GGUF file I/O at edge |
| `sync/object_storage.py` (new) | `ObjectStorageSync.sync(alias, record) -> Path` | Idempotent bucket → volume sync outside the lifecycle lock | HTTP client + credentials injected | Bucket GET, volume write |
| `downloader.py` (extended) | RegistryDownloader chooses hf / object_storage / volume-verified | Download-or-sync dispatch; hf path unchanged | CLI runner injected | subprocess CLI at edge |
| `cache.py` (unchanged surface) | `get`, `put`, `replay_sse`, `evict` | Per-instance cache dir via injected `instance_id` | Clock, filesystem paths injected | Disk I/O at edge |
| `composition.py` (extended) | `build_app()` | Wires new modules; acyclic graph; single concrete constructor | All collaborators | App bootstrap only |

Rules preserved: routes depend only on contracts; `runtime` depends on `compute` contracts; `sync` and `downloader` never hold or touch the process-wide lifecycle lock; composition root is the only constructor of concrete implementations; no cycles, no sibling-internal access, no shared mutable state, side effects at edges only. Independent per-module tests use fakes inside Docker. The service team owns application modules; platform owns Helm, Terraform, ingress API-key policy, K8s Secrets, and GPU device access.

## 3. Container image strategy (CPU / CUDA / Metal)

- Weights are never baked: `models/` and `.cache/responses/` are volumes in every flavor.
- `Dockerfile.cpu`: existing digest-pinned linux/amd64 Miniconda base plus the committed `environment-linux-64.lock`; unchanged artifact reused by Compose and Helm.
- `Dockerfile.cuda`: digest-pinned base plus a separately generated CUDA lock built inside Docker (for example `environment-cuda-64.lock`). The committed linux-64 lock is never modified, and no unpinned `pip install --upgrade pip` is introduced.
- Metal: documentation-only in Phase 2. Apple Silicon local support is described as a future native path; there is no Metal container image, no cloud Metal target, and no Docker/CI Metal verification claim.
- Every implemented image targets a pinned platform/base digest; lock regeneration happens inside Docker only; `environment.yaml` remains the human-edited dependency source.

## 4. One OCI artifact shared by Compose and Helm / IaC

Build each flavor once, promote each by digest. Compose and Helm both reference the selected flavor digest, for example `repository/local-llm-api@sha256:<cpu-digest>` or `repository/local-llm-api-cuda@sha256:<cuda-digest>`. Compose keeps loopback-only publish (`127.0.0.1:8000`) and optional `private.txt` → `HF_TOKEN`. Helm adds: a TLS Ingress with gateway API-key policy, per-instance volumes for `models/` and `.cache/responses/`, an initContainer for object-storage sync, a ConfigMap rendered from `local_llm.yaml`, and a cloud `securityContext` (non-root, read-only root, dropped capabilities, RuntimeDefault seccomp). Terraform's AWS reference module provisions EKS CPU/GPU node groups, an S3 weights bucket, K8s Secrets/secret references, ingress, and network policies. No config duplication: `local_llm.yaml` is the single source of truth mounted into both Compose and Helm.

## 5. Testability with fakes inside Docker + acceptance criteria

All tests run via `docker compose run --rm tests` with injected fakes: fake backend probes, fake object-storage HTTP, fake per-backend llama runtime, fake clock, and injected paths. Draft criteria in `docs/requirements.md` are `AC-81`–`AC-167`; the major behaviors are backend fail-closed placement, per-instance cache isolation, object-storage sync failures, same-digest Compose/Helm deployment, read-only-root startup, K8s Secret-only credentials, gateway API-key rejection, and no secrets-manager calls from application code.

## 6. Boundaries, risks, and deferred items

Boundaries: app contract unchanged — one resident instance per process, serialized generation, one process-wide lock, loopback-only local exposure. Auth/API keys belong to the gateway; secrets are infra-injected; object-storage sync is infra-side; weights live outside the image. Admin routes are internal-only. Rate limiting is explicitly deferred with residual risk accepted under `ASSUMPTION-21`, and `ASSUMPTION-17` stays open.

Severity-ordered findings:

1. **Critical** — Per-instance residency and cache do not span replicas; every pod cold-loads and cache hits are local only. Sticky alias affinity or per-pod warm-up is a separate product decision under `ASSUMPTION-18`.
2. **High** — Synced weights can diverge from HF originals; the `size:mtime` fingerprint is unreliable across copies. Treat synced weights as immutable in Phase 2 or adopt content-hash fingerprints/versioned aliases later under `ASSUMPTION-19`.
3. **High** — API-key enforcement lives in the gateway; a misconfigured ingress exposes the unauthenticated app. Admin paths must be excluded and NetworkPolicy must block direct pod access.
4. **Medium** — CUDA dependency resolution is outside the committed linux-64 lock; the CUDA flavor needs its own byte-for-byte-verified lock generated inside Docker.
5. **Medium** — Read-only root and non-root conflict with Windows bind-mount ownership locally; Phase 2 applies them only to cloud pods under `DECISION-26`.
6. **Low** — Config drift between `local_llm.yaml` and the Helm ConfigMap; mitigated by single-source-of-truth plus a config parity check.

Deferred (non-Phase-2): application auth and rate limiting, cache encryption, local Compose non-root, Metal implementation/verification, AMD ROCm, cloud-managed inference, concurrent generation, multi-model residency, async download queue, admin alias gating, shared cache, gated HF repos, embeddings, tools, model-management UI, multi-architecture locks, and content-hash fingerprints.

## Feasibility verdict

**Feasible — bounded.** Phase 2 is additive: new config sections plus new `compute` and `sync` modules behind injected contracts; no existing MVP contract, route, or `local_llm.yaml` key changes. CPU and CUDA cloud targets are feasible with one digest-pinned OCI artifact per flavor shared by Compose and Helm/IaC, and local Compose keeps working unchanged. Metal is documentation-only in Phase 2. Conditions: the gateway owns API keys and TLS; object-storage sync treats weights as immutable; per-instance residency and deferred rate limiting are documented as accepted residuals; `ASSUMPTION-17` and the new Phase 2 assumptions are not silently closed.
