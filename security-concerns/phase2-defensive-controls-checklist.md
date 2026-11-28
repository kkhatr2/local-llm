# Defensive Controls Checklist — Phase 2 Implementation Readiness

**Repository:** `C:\Users\kruna\Documents\R\local-llm` (local-llm API)
**Review type:** Read-only security and defensive-coding review completed before implementation; its “planned” labels describe the original gap analysis rather than the current code state.
**Authoritative inputs:** `docs/requirements.md` (AC-1–AC-167, `DECISION-26`), `docs/security-report.md` (living draft), `docs/architecture.md` (C1–C16), `local_llm.yaml`, `docker-compose.yml`, `Dockerfile`, `local_llm/*.py`, `tests/*.py`.
**Current status:** MVP and Phase 2 Docker-verifiable criteria are implemented and verified by the 174-test `docker compose run --rm tests` gate. `docs/deployment.md` records the implemented artifacts and the remaining live cluster/AWS platform gates. `ASSUMPTION-17` (host trust / loopback sufficiency) remains open and must not be silently closed.

## Severity scale (adopted from docs/security-report.md §3)

| Severity | Meaning |
|----------|---------|
| Critical | Directly exposes secrets, external access, or unauthenticated remote compromise. None identified in the current loopback-only boundary. |
| High | Service-wide denial of service, arbitrary admin action, or meaningful unsafe behavior reachable by a local caller. |
| Medium | Sensitive-data exposure, privilege/resource misuse, or defense-in-depth gap requiring local access or an operational mistake. |
| Low | Narrow race, stale data, conditional log leakage, or misconfiguration with limited impact. |
| Info | Verified control to preserve; no change required. |

**Owner roles:** Tech Lead (TL), Security Engineer (SE), Platform (PLAT), Product Lead (PM), Delivery Manager (DM).
**Checkbox meaning:** `[ ]` = defensive control required (not yet implemented or not verified); `[x]` = verified in the current Docker gate — preserve, do not weaken.

---

## CRITICAL

### [ ] CR-1 — Public exposure without gateway enforcement would be Critical (conditional, not present in MVP)

**Categories:** no secret leakage, TLS/ingress, network policies
**Gap observed:** Nothing in the repo enforces authentication at the edge. `ASSUMPTION-17` treats loopback-only exposure as sufficient for a trusted single-user host; if Phase 2 publishes a public endpoint without gateway API-key enforcement, unauthenticated callers reach chat and admin routes (remote compromise / arbitrary admin action).
**AC IDs:** AC-107–AC-114 (gateway/API-key/path isolation), AC-104–AC-106 (ingress gating), AC-115–AC-116 (TLS), ASSUMPTION-17 open
**Target files:** Helm ingress chart (planned, not in repo), Helm NetworkPolicy (planned), Terraform ingress/NP (planned). Application files stay auth-less: `local_llm/admin_routes.py`, `local_llm/chat_routes.py`, `local_llm/composition.py` must not acquire an API-key or secrets-manager dependency.
**Owner:** PLAT + SE
**Verification:** Phase 2 deployment prep must confirm a missing/invalid API key returns 401 before the app sees the request (AC-107–AC-109), `/admin/models*` is not publicly routable (AC-112–AC-113), and direct pod access is denied (AC-114).

---

## HIGH

### [ ] H-1 — Gateway API-key enforcement + admin-route isolation for Phase 2 public exposure

**Categories:** TLS/ingress, network policies
**Gap observed:** MVP publishes only `127.0.0.1:8000` (verified). Phase 2 moves the trust boundary to a TLS gateway; `/admin/models*` must never be publicly routable and direct pod access must be denied. The application itself must remain auth-less (no API key read, no secrets-manager call).
**AC IDs:** AC-107–AC-114, AC-104–AC-106, AC-137–AC-138
**Target files:** Helm ingress + NetworkPolicy templates (planned), Terraform reference module (planned); app contract files unchanged: `local_llm/admin_routes.py`, `local_llm/chat_routes.py`, `local_llm/composition.py`
**Owner:** PLAT + SE

### [ ] H-2 — Server-side request body size and `max_tokens` cap, enforced before the lifecycle lock

**Categories:** validation, fail-closed GPU behavior (lock exhaustion), safe error mapping
**Gap observed:** OpenCode's 96-token cap (`limit.output` / `body.options.max_tokens`) is client-side only (`opencode.jsonc`). `local_llm/chat_routes.py` `ChatCompletionRequest` has no body-size limit and no server-side `max_tokens` ceiling (`local_llm.yaml` `generation_defaults.max_tokens: 512` is not enforced). A local caller can request an arbitrarily large generation and hold the 600s process-wide lock → service-wide DoS / head-of-line blocking (`SEC-G-04`, `SEC-10` planned).
**AC IDs:** AC-10 (security report, planned SEC-G-04); MVP validation pattern AC-2; Phase 2 field-named validation pattern AC-81–AC-85; OpenCode caps AC-48
**Target files:** `local_llm/chat_routes.py` (`ChatCompletionRequest`, route handler), `local_llm/config.py` (new `server.max_request_body_bytes`, generation/load option ceilings), `local_llm.yaml`, `tests/test_chat_routes.py`
**Owner:** TL
**Smallest mitigation:** Reject oversized bodies and `max_tokens` above a configured ceiling with stable 422 through `local_llm/error_mapper.py` before any lock acquisition, model load, or inference.

### [ ] H-3 — Capture/redact `hf` subprocess output; never let CLI diagnostics into container logs

**Categories:** no secret leakage
**Gap observed:** `local_llm/composition.py:_subprocess_runner` calls `subprocess.run(...)` with default stdout/stderr, so `hf` / `huggingface_hub` diagnostics inherit container stdout/stderr. `HF_TOKEN` is injected into the container environment (optional `env_file`) and is never passed as a CLI argument (verified), but a future CLI printing credential-bearing URLs would leak the token into container logs. `SEC-G-17`, `SEC-18` residual.
**AC IDs:** AC-73–AC-76, AC-129, AC-147; `SEC-G-17`, `SEC-18`
**Target files:** `local_llm/composition.py` (`_subprocess_runner`), `local_llm/downloader.py` (runner contract), `docker-compose.yml` (`env_file`), `tests/test_composition.py`, `tests/test_hf_cli_downloader.py`
**Owner:** SE
**Smallest mitigation:** Capture `hf` stdout/stderr; log only alias/repo_id/filename/exit-code metadata; never log raw CLI output. Keep `HF_TOKEN` out of CLI arguments and out of application logs.

### [ ] H-4 — Cache directory and file permission hardening (`0o700` dir / `0o600` files)

**Categories:** no secret leakage, path traversal and symlink protection
**Gap observed:** `local_llm/cache.py:ResponseCache.put` creates `.cache/responses/` with `mkdir(parents=True, exist_ok=True)` under the process umask (typically `0o755`). `mkstemp` files are `0o600` (`SEC-I-16`), but the directory itself is not explicitly restricted. Cache files contain completed response text and any user content echoed by the model (`README.md` §Response cache). `SEC-G-02`, `SEC-8` planned.
**AC IDs:** AC-49, `SEC-8` (planned), `SEC-G-02`; Phase 2 writable-mount scoping AC-122
**Target files:** `local_llm/cache.py` (`_resolve_dir`, `put` mkdir), `docker-compose.yml` (bind mount/ACL note), Helm pod securityContext (planned), `tests/test_response_cache.py`
**Owner:** TL

### [ ] H-5 — Admin `DELETE` must unload resident instance and purge that alias's cache entries

**Categories:** cleanup, safe error mapping
**Gap observed:** `local_llm/admin_routes.py:remove` unregisters the alias (`204`) but (a) leaves cache entries containing that alias's response text, and (b) does not unload an in-memory instance → residency drift (alias gone from registry while GGUF still occupies the single resident slot). `SEC-G-03`, `SEC-9` planned; README already documents "call unload first" but the code does not enforce it.
**AC IDs:** `SEC-G-03`, `SEC-9` (planned)
**Target files:** `local_llm/admin_routes.py` (`remove`), `local_llm/cache.py` (add alias-scoped removal), `local_llm/runtime.py` (`unload` coordination), `tests/test_admin_routes.py`, `tests/test_response_cache.py`
**Owner:** TL
**Smallest mitigation:** Under the process-wide lifecycle lock: unload the instance if loaded, cancel its idle timer, `registry.remove(alias)`, then delete cache entries keyed by that alias (prefix scan on `alias` metadata inside entries, since file names are key hashes).

---

## MEDIUM

### [ ] M-1 — Validate `paths.model_dir` / `paths.cache_dir` at config load (path-traversal guard)

**Categories:** validation, path traversal and symlink protection
**Gap observed:** `local_llm/config.py:PathSettings` accepts `model_dir`/`cache_dir` with no separator or `..` validation. `cache.py:_resolve_dir` contains `cache.dir` inside the workspace and outside `models/`, but `downloader.py:_destination` joins `model_dir` raw under `root`. A configured `model_dir: ../escape` would write weights outside the workspace. Validation errors must name the offending field (MVP pattern AC-2).
**AC IDs:** MVP AC-2 pattern; Phase 2 config-section validation AC-81–AC-85
**Target files:** `local_llm/config.py` (`PathSettings` field validators), `local_llm/cache.py` (`_resolve_dir` — already partial), `local_llm/downloader.py` (`_destination`), `tests/test_config_loader.py`
**Owner:** TL

### [ ] M-2 — Symlink protection for model and cache paths

**Categories:** path traversal and symlink protection
**Gap observed:** `downloader.py:exists` uses `Path.is_file()` which follows symlinks; a symlinked `models/<alias>/<filename>` could redirect reads/writes outside the intended alias directory. `cache.py:path_for` is hex-keyed (traversal-safe), but directory containment relies on `resolve()` which follows symlinks.
**AC IDs:** AC-94–AC-96 (model-supply integrity, Phase 2)
**Target files:** `local_llm/downloader.py` (`exists`, `_destination`), `local_llm/cache.py` (`path_for`, `_resolve_dir`), `tests/test_hf_cli_downloader.py`, `tests/test_response_cache.py`
**Owner:** SE
**Smallest mitigation:** Reject non-regular files or resolve with `realpath()` and enforce containment under the configured root/model/cache directories before read or write.

### [ ] M-3 — Atomic write + verification for downloaded GGUF weights (no partial-weight reuse)

**Categories:** atomic writes, cleanup
**Gap observed:** `downloader.py:download` writes directly to the final destination path; `exists()` treats any non-empty regular file as present. An interrupted `hf` run can leave a partial/truncated GGUF > 0 bytes that is silently reused on the next startup (skip-download), poisoning the model. `SEC-G-16` partial (accepted residual).
**AC IDs:** AC-95 (Phase 2: sync failure writes no partial weights), `SEC-G-16`
**Target files:** `local_llm/downloader.py` (`download`, `_destination`), `local_llm/composition.py` (`_download_one`), `tests/test_hf_cli_downloader.py`
**Owner:** TL
**Smallest mitigation:** Download to a temp file in the same directory, `fsync`, `os.replace` into place, then re-verify `exists()`; never present partial bytes as a completed model.

### [ ] M-4 — Re-check disk immediately before each parallel download write (download TOCTOU)

**Categories:** atomic writes, lock handling
**Gap observed:** `composition.py:_download_configured_models` runs `preflight.check_download` once per missing model serially, then starts bounded parallel downloads. Disk can be exhausted after the check passes; `ENOSPC` mid-download is not explicitly handled. `SEC-G-16` accepted residual.
**AC IDs:** AC-65, `SEC-G-16`, AC-96
**Target files:** `local_llm/composition.py` (`_download_configured_models`), `local_llm/downloader.py`, `tests/test_composition.py`
**Owner:** TL

### [ ] M-5 — Non-root / read-only-root-filesystem pod compatibility (Phase 2 cloud pods)

**Categories:** non-root/read-only pod compatibility
**Gap observed:** `Dockerfile` has no `USER` directive → container runs as root (`SEC-G-09`). No `readOnlyRootFilesystem`, dropped capabilities, or seccomp in any artifact. The app writes to the cache at runtime and `hf` may write to `~/.cache/huggingface` (`HF_HOME`) outside the declared `models/`/`.cache/` volumes, which breaks read-only rootfs / non-root pods and can leave artifacts outside volumes. Local Compose non-root is deferred under `ASSUMPTION-22` (Windows bind-mount UID/GID unresolved).
**AC IDs:** AC-117–AC-122 (Phase 2, planned), `SEC-G-09`, ASSUMPTION-22
**Target files:** `Dockerfile` (add `USER`, `ENV HF_HOME` to a writable volume path, `PYTHONDONTWRITEBYTECODE=1` already present), `docker-compose.yml` (local unchanged, deferred), Helm pod securityContext (planned), `tests/test_environment.py`
**Owner:** PLAT + TL
**Smallest mitigation for Phase 2 builds:** non-root `USER`, `HF_HOME=/app/.cache/hf` (on the cache volume), no writes outside `models/`, `.cache/`, `/tmp`; cloud pod `securityContext` per DECISION-26.

### [ ] M-6 — Fail-closed GPU backend selection (Phase 2 compute)

**Categories:** fail-closed GPU behavior, validation
**Gap observed:** No backend selection exists in code (CPU only, `n_gpu_layers: 0`). Phase 2 must: reject unknown `compute.active_backends` values with the field name; require `compute.fallback: fail_closed` (no silent CUDA→CPU degradation); reject `metal` outside `documented_backends`; apply `compute.backend_defaults.cuda.n_gpu_layers` only to CUDA placement; GPU-memory preflight must reject a CUDA load when estimated VRAM exceeds the configured limit; CPU receives `n_gpu_layers: 0`; one resident model per process preserved.
**AC IDs:** AC-82, AC-84, AC-86–AC-90, AC-91
**Target files:** `local_llm/config.py` (new `deployment`/`compute`/`registry`/`security` sections with field-named errors), new backend-selector / device-placement module (planned), `local_llm/preflight.py` (VRAM check, AC-90), `local_llm/runtime.py` (`n_gpu_layers` application), `tests/` (new Phase 2 tests)
**Owner:** TL
**Smallest mitigation:** Backend selection returns one explicit backend per load; `fallback != fail_closed` fails startup naming `compute.fallback`; missing CUDA under a CUDA request returns 503 naming the backend, never silent CPU substitution.

### [ ] M-7 — Cache freshness / weight-fingerprint staleness

**Categories:** validation, safe error mapping
**Gap observed:** `cache.py:weight_fingerprint` is `size:mtime_ns`. A same-size GGUF swapped at the same mtime replays stale cached output. Content-hash fingerprints / versioned aliases are deferred (`SEC-G-08`, `SEC-13`, `ASSUMPTION-19` open).
**AC IDs:** `SEC-G-08`, `SEC-13` (deferred — content hash only with documented cache-key invalidation and tests), ASSUMPTION-19
**Target files:** `local_llm/cache.py` (`weight_fingerprint`), `tests/test_response_cache.py`, `docs/security-report.md`
**Owner:** SE

### [ ] M-8 — Request-side llama options can bypass `max_n_ctx` (merge-order escape)

**Categories:** validation
**Gap observed:** `runtime.py:merge_load_options` merges `llama_defaults → model options → request options` without re-checking `n_ctx <= max_n_ctx` for request-supplied options. `config.py` only validates `llama_defaults.n_ctx <= max_n_ctx` at load (`Settings._validate_cross_fields`). A request with `options.n_ctx: 1000000` bypasses the cap → oversized KV cache / RAM abuse.
**AC IDs:** MVP AC-15 (unsupported options rejected); Phase 2 effective-options merge rule (`docs/requirements.md` §Phase 2: backend defaults inserted once between defaults and model/request options)
**Target files:** `local_llm/runtime.py` (`merge_load_options`), `local_llm/chat_routes.py` (merge computation), `local_llm/config.py` (`max_n_ctx`), `tests/test_runtime.py`
**Owner:** TL

### [ ] M-9 — `HF_HOME` / transient tool output outside declared volumes

**Categories:** no secret leakage, non-root/read-only pod compatibility
**Gap observed:** Related to M-5: `huggingface_hub` may write token cache / XET artifacts to `HF_HOME` (default `~/.cache/huggingface`) inside the container image layer or an unwritable home dir under read-only rootfs. This can break non-root/read-only pods and leave credential-related artifacts outside the declared volumes.
**AC IDs:** AC-122 (writable mounts), AC-123–AC-125 (credentials from secrets, not image), AC-127 (no secret in image layer)
**Target files:** `Dockerfile` (`ENV HF_HOME=...` on writable volume), `docker-compose.yml` (environment), Helm (planned)
**Owner:** PLAT

---

## LOW

### [ ] L-1 — `AliasDownloadLocks._locks` map never evicts (alias-cardinality memory leak)

**Categories:** lock handling
**Gap observed:** `local_llm/locking.py:AliasDownloadLocks` retains a lock per alias forever. High-alias cardinality (runtime adds) grows the map without bound. Minor resource misuse; no AC coverage.
**Target files:** `local_llm/locking.py`, `tests/test_locking.py`
**Owner:** TL
**Smallest mitigation:** Evict locks for aliases no longer in the registry, or cap the map.

### [ ] L-2 — Idle worker does blocking `lock.acquire()` with no timeout

**Categories:** lock handling
**Gap observed:** `local_llm/runtime.py:_expire_idle` blocks on `lock.acquire()` (no timeout) before unloading. Under sustained contention the idle unload can be delayed past its deadline. It is a daemon thread (no hard hang) and blocking is intentionally required for safe unload (`SEC-G-07`, documented accepted).
**AC IDs:** `SEC-G-07`; AC-47 context (lock timeout 600s, 503 on contention)
**Target files:** `local_llm/runtime.py` (`_expire_idle`), `docs/security-report.md`
**Owner:** TL

### [ ] L-3 — Unvalidated `stop` and `messages` items reach llama.cpp

**Categories:** validation, safe error mapping
**Gap observed:** `local_llm/chat_routes.py:ChatCompletionRequest` declares `stop: Any = None`; `messages` items are not type-checked. Malformed values reach `llama_cpp` → `RuntimeError` → sanitized 502 instead of a clean 422.
**AC IDs:** AC-22 (422 stable envelope)
**Target files:** `local_llm/chat_routes.py` (`ChatCompletionRequest` validators), `tests/test_chat_routes.py`
**Owner:** TL

### [ ] L-4 — Cache hit rewrites `last_accessed_at` but silently swallows `OSError`

**Categories:** cleanup
**Gap observed:** `local_llm/cache.py:get` rewrites the entry on hit to refresh `last_accessed_at`; `OSError` is caught and `pass`ed, so access metadata can go stale and LRU eviction is slightly off. No security exposure, weakens TTL/LRU accuracy.
**AC IDs:** `SEC-I-16` partial
**Target files:** `local_llm/cache.py` (`get`), `tests/test_response_cache.py`
**Owner:** TL

### [ ] L-5 — ErrorMapper body leaks exception class name (`type` field)

**Categories:** safe error mapping
**Gap observed:** `local_llm/error_mapper.py:map` sets `"type": type(exception).__name__` in every envelope. This is minimal class-name metadata, not secrets, but could aid fingerprinting.
**AC IDs:** AC-23 (sanitized envelopes, paths/stack traces/tokens excluded)
**Target files:** `local_llm/error_mapper.py`, `tests/test_error_mapper.py`
**Owner:** SE

### [ ] L-6 — `_hf_repo_info` swallows network errors as "not public"

**Categories:** no secret leakage
**Gap observed:** `local_llm/composition.py:_hf_repo_info` catches all exceptions and returns `False`, so a transient network failure is reported as "repository is not public" (safe rejection, misleading message). `SEC-G-17` conditional residual.
**AC IDs:** AC-73–AC-76 hygiene context
**Target files:** `local_llm/composition.py` (`_hf_repo_info`), `tests/test_composition.py`
**Owner:** TL

### [ ] L-7 — `resources.fail_closed` flag is decorative

**Categories:** validation
**Gap observed:** `local_llm/config.py:ResourceSettings.fail_closed` defaults `true`, but `preflight.py` always fails closed regardless of the flag. Flipping the flag changes nothing (safe by accident, misleading to operators). Validation errors should name the offending field.
**AC IDs:** AC-51 (MVP fail-closed preflight)
**Target files:** `local_llm/config.py` (`ResourceSettings`), `local_llm/preflight.py`, `local_llm.yaml`, `tests/test_preflight.py`
**Owner:** TL

---

## INFO — Verified Controls to Preserve (No Change Required)

| ID | Control | Evidence | AC IDs | Target files |
|----|---------|----------|--------|--------------|
| [x] I-1 | Host publish loopback-only (`127.0.0.1:8000`); never `0.0.0.0` host publish | `docker compose --profile serve config --no-interpolate`; Compose `ports: "127.0.0.1:8000:8000"` | AC-24, AC-50, AC-105; `SEC-I-01`, SEC-1 | `docker-compose.yml`, `local_llm.yaml` comments, `README.md` |
| [x] I-2 | `HF_TOKEN` optional via untracked `private.txt` `env_file` (`required: false`); never CLI arg, never image, never logged; public downloads work without | Compose config both token states; container-environment tests; real-model smokes | AC-44, AC-73–AC-76; `SEC-I-02`, `SEC-18`; Phase 2 AC-123–AC-130, AC-147 | `docker-compose.yml`, `.gitignore`, `.dockerignore`, `local_llm/downloader.py`, `local_llm/composition.py` (`configure_logging`) |
| [x] I-3 | Fail-closed disk/RAM preflight → 503, no model bytes, no load | `local_llm/preflight.py`, startup smoke | AC-12, AC-13, AC-51; `SEC-I-03`, SEC-3 | `local_llm/preflight.py`, `local_llm/composition.py`, `local_llm.yaml`, `tests/test_preflight.py` |
| [x] I-4 | Atomic cache writes (mkstemp + fsync + os.replace); corrupt/schema-mismatched entries deleted as misses; TTL + LRU eviction | `local_llm/cache.py` `_atomic_write`, `_read_entry`, `evict` | AC-29, AC-33–AC-35; `SEC-I-16`, SEC-16 | `local_llm/cache.py`, `tests/test_response_cache.py` |
| [x] I-5 | Sanitized stable error envelopes (422/502/503/500); no prompts/paths/stack traces/tokens; SSE stream errors without `[DONE]` | `local_llm/error_mapper.py`, `install_error_handlers`, streaming smoke | AC-22, AC-23, AC-70–AC-72; `SEC-I-06`, `SEC-I-21`, `SEC-G-18` | `local_llm/error_mapper.py`, `local_llm/composition.py`, `local_llm/chat_routes.py`, `local_llm/streaming.py`, `tests/test_error_mapper.py`, `tests/test_streaming.py` |
| [x] I-6 | Bounded lifecycle lock (600s wait), 503 on contention; one resident instance; chat/admin/idle serialized | `local_llm/locking.py`, composition wiring | AC-47, AC-59; `SEC-I-07` | `local_llm/locking.py`, `local_llm/composition.py`, `local_llm/chat_routes.py`, `local_llm/runtime.py`, `tests/test_locking.py` |
| [x] I-7 | Idle retention bounds: `0 < idle_unload_seconds <= 86400` validated at config and runtime; resident alias unloads only after idle or immediate conditions; timer shares lock | `local_llm/config.py`, `local_llm/runtime.py`, `local_llm/chat_routes.py` | AC-19, AC-20, AC-57–AC-61; `SEC-I-08`, `SEC-I-09`, `SEC-I-10`, `SEC-I-11` | `local_llm/config.py`, `local_llm/runtime.py`, `local_llm/chat_routes.py`, `tests/test_runtime.py` |
| [x] I-8 | Download I/O outside the lifecycle lock; per-alias download lock; bounded parallel startup downloads (1–16) after preflight; lifecycle lock only for re-checks/registry mutation | `local_llm/composition.py` (`_download_configured_models`, `_download_one`), `local_llm/locking.py`, `chat_routes.py:_resolve_path` | AC-62–AC-66; `SEC-I-20`; `SEC-G-01` mitigated by DECISION-22 | `local_llm/composition.py`, `local_llm/locking.py`, `local_llm/chat_routes.py`, `tests/test_composition.py`, `tests/test_locking.py` |
| [x] I-9 | Streaming lock held through iteration; released in `finally` on success/error/early close; native chunks closed before unload; partial content never cached; cache-hit replay skips load/inference | `local_llm/chat_routes.py` (`_stream_chat`, `_LifecycleStreamingResponse`), `local_llm/streaming.py`, `local_llm/runtime.py` | AC-67–AC-72; `SEC-I-21` | `local_llm/chat_routes.py`, `local_llm/streaming.py`, `local_llm/runtime.py`, `tests/test_streaming.py` |
| [x] I-10 | Supply-chain build: digest-pinned Miniconda base; committed linux-64 explicit lock with SHA-256 fragments; `conda create --file` from lock; no `conda env create -f` or unpinned pip upgrade; `platform: linux/amd64` on all Compose services | `Dockerfile`, `environment-linux-64.lock`, `docker-compose.yml`, byte-for-byte lock regeneration | AC-77–AC-80; `SEC-I-10` (architecture table), `SEC-I-19` | `Dockerfile`, `environment-linux-64.lock`, `docker-compose.yml`, `tests/test_dependency_lock.py` |
| [x] I-11 | Docker-only execution; read-only config mount; `models/` and `.cache/` ignored; no host Python/conda/uvicorn | AGENTS.md, `docker-compose.yml` mounts, `.gitignore`, `.dockerignore` | AC-40–AC-42, AC-53; `SEC-I-14` | `docker-compose.yml`, `.gitignore`, `.dockerignore`, `AGENTS.md`, `README.md` |

---

## Phase 2 Deployment-Only Controls (Not Implemented — Helm / Terraform / Pipeline Artifacts)

These have no application-code equivalent by design (the app stays auth-less; gateway, K8s Secrets, and init-containers own the edges, per `docs/architecture.md` §Phase 2 module boundaries).

| ID | Control | AC IDs | Target artifact (planned) | Owner |
|----|---------|--------|---------------------------|-------|
| [ ] P-1 | NetworkPolicy denies direct pod access; only gateway → pod | AC-114, AC-138 | Helm NetworkPolicy templates, Terraform NP | PLAT |
| [ ] P-2 | Public ingress exposes only `/health` and per-alias chat; `/v1/models` and `/admin/models*` excluded | AC-110–AC-113, AC-104–AC-106 | Helm ingress, Terraform ingress | PLAT |
| [ ] P-3 | TLS 1.2+ on public endpoint; HTTP → HTTPS redirect | AC-115, AC-116 | Ingress certificate / ALB listener, Terraform | PLAT |
| [ ] P-4 | Pod `securityContext`: non-root, read-only rootfs, no privilege escalation, drop ALL capabilities, RuntimeDefault seccomp; writable mounts only at `models`, `.cache`, `/tmp` | AC-117–AC-122 | Helm pod spec, Terraform pod template | PLAT + TL |
| [ ] P-5 | Secrets from K8s Secrets / workload identity only: `HF_TOKEN`, API-key ref, object-store creds; never in `local_llm.yaml`, image layers, CLI args, logs, or shared manifest output | AC-123–AC-130 | Helm Secrets + values, Terraform secrets; app: `local_llm/composition.py` logging stays secret-free | PLAT + SE |
| [ ] P-6 | Supply chain: image references by digest only (Helm + Terraform), image signature verification before promotion, SBOM archived per digest, vulnerability-scan gate per digest | AC-142–AC-146 | Helm values, Terraform, CI/pipeline | PLAT + DM |
| [ ] P-7 | Gateway audit records masked API-key identity, alias, status, request ID; application logs keep no-token/no-prompt/no-path/no-stack-trace contract | AC-147–AC-154 | Gateway audit logging (planned); app logging unchanged | PLAT + SE |
| [ ] P-8 | Documented deferrals: rate-limiting deferral, lock-exhaustion residual accepted in writing, `ASSUMPTION-17`/`ASSUMPTION-18`/`ASSUMPTION-19`/`ASSUMPTION-21` remain open | AC-155–AC-160 | `docs/security-report.md`, `docs/requirements.md` | PM + SE |
| [ ] P-9 | Verification gates: full 142-test MVP suite + new Phase 2 tests in Docker; `git check-ignore` passes; no weights/cache staged; Metal documentation-only, no Docker/CI Metal claim | AC-161–AC-167 | `tests/`, `Dockerfile`, `docs/planning/phase2-kubernetes-primary.md` | TL + DM |

---

## Category Coverage Index (requested defensive areas → checklist items)

| Requested area | Checklist items |
|----------------|-----------------|
| Validation | H-2, M-1, M-6, M-8, L-3, L-7; Info I-7 |
| Fail-closed GPU behavior | M-6; Info I-3 |
| No secret leakage | H-3, H-4, M-5, M-9, L-6; Info I-2; Phase 2 P-5, P-7; CR-1 conditional |
| Path traversal and symlink protection | M-1, M-2; Info I-4 |
| Atomic writes | M-2, M-3, M-4; Info I-4 |
| Cleanup | H-5, L-4; Info I-9 |
| Lock handling | L-1, L-2; Info I-6, I-8, I-9 |
| Non-root / read-only pod compatibility | M-5; Phase 2 P-4 |
| Network policies | P-1; CR-1 conditional |
| TLS / ingress | P-2, P-3; CR-1 conditional; H-1 |
| Supply-chain references | I-10; Phase 2 P-6, AC-99–AC-103 |
| Safe error mapping | H-2, L-5; Info I-5 |

---

## Implementation Notes

1. **Order of work:** address HIGH items before MEDIUM; H-1/H-3/H-5 are the only items that change reachable trust boundaries under `ASSUMPTION-17`. Phase 2 items (P-1…P-9, AC-81–AC-167) were implemented after product approval; their live cluster/AWS gates remain documented in `docs/deployment.md`, and none may be treated as closing `ASSUMPTION-17`, `ASSUMPTION-18`, `ASSUMPTION-19`, `ASSUMPTION-21`, or `ASSUMPTION-23`.
2. **Verification:** every code change must be validated with `docker compose run --rm tests` (must stay green at 142+ tests), `docker compose --profile serve config --no-interpolate` (loopback publish, no `HF_TOKEN` output), and `git check-ignore -v private.txt models/ .cache/responses/`. No model weights or cache files may be staged or committed.
3. **Do not:** add application-level auth/rate limiting, local non-root container, cache encryption, content-hash GGUF fingerprints, admin-alias gating, or concurrent generation without a new product decision (all deferred/rejected per `docs/security-report.md` and `docs/requirements.md`).
4. **No secret values** appear in this document; none were extracted from `private.txt`, image layers, CLI arguments, or logs during this review.
