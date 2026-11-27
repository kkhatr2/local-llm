# Security report and mitigation plan

Status: **living draft — not approved as final**. `ASSUMPTION-17` remains open by product decision on 2026-11-27: the loopback-only trust boundary is treated as sufficient for a trusted single-user host, but the report must not silently close auth/rate limiting, non-root container, or cache encryption.

Last reviewed: **2026-11-27**

Maintainer: **Product Lead**

Contributing leads: **Tech Lead, Security Engineer, Delivery Manager**

Review inputs incorporated: independent security assessment, engineering review, tech-lead review, and delivery review for the idle-retention change.

Authoritative scope: `docs/requirements.md` and `docs/architecture.md`. Runtime settings: `local_llm.yaml`. Operating rules: `AGENTS.md`.

## 1. Product framing

### User request, in the client's words

> "could you create and maintain a security report with cybersecurity mitigations for the codebase. include what is implemented and what needs to be implemented, severity, implementation effort and other suggestions from your and teams the team's memory."

### Outcome

A living report that lets an operator or reviewer answer four questions quickly:

1. What security controls are implemented and where is the evidence?
2. What is still open, how severe is it, and how much effort would closure take?
3. What is deferred or rejected by existing decisions and must not be reopened silently?
4. When must this report be reviewed or updated?

### Constraints

- Preserve loopback-only host exposure: `127.0.0.1:8000` is the only host publish.
- Preserve the approved 180-second idle retention with `server.idle_unload_seconds` validated `> 0` and `<= 86400`.
- Keep `server.lock_timeout_seconds: 600` and `503` on contention.
- Keep OpenCode `96`-token caps and supported V2 fields.
- All execution, tests, deployment, and verification remain Docker-only.
- Do not add auth, rate limiting, non-root container, cache encryption, content-hash GGUF fingerprints, admin-alias gating, or concurrent generation without a new product decision.
- Never commit `private.txt`, model weights, or `.cache/responses/`.

### Smallest coherent MVP for this change

Create this report and link it from `README.md`. Do **not** change runtime behavior. Contract-preserving small fixes listed below are recommendations for a future approved change, not work performed by this documentation task.

### Deferred from this report

Actual implementation of any mitigation, replacement of the loopback trust model, embeddings/tool-calling APIs, GPU inference, model-management UI, and committed dependency lock generation.

### Rejected by existing decisions

7B parameter enforcement, committing weights, non-GGUF formats, gated/private model support beyond public repos, loading all models at startup, using loaded instances as the response cache, host-local Python/conda/uvicorn execution, and unsupported OpenCode key spellings.

## 2. Trust boundary and assumptions

| ID | Assumption or decision | Owner | Status | Required decision |
| --- | --- | --- | --- | --- |
| `ASSUMPTION-17` | Loopback-only exposure, token hygiene, cache-retention guidance, and Docker-only execution are sufficient for a trusted single-user host. | Product Lead + Security Engineer | **Open** — user chose “keep open” on 2026-11-27 | Confirm whether any other local user/process can reach `127.0.0.1:8000`. If yes, open scope for auth, rate limiting, non-root container, and cache encryption. |
| `ASSUMPTION-2` | Inside Docker the app binds `0.0.0.0:8000`; Compose publishes only to host `127.0.0.1:8000`. | Tech Lead | Closed | None. Any change to host `0.0.0.0` is rejected. |
| `ASSUMPTION-5` | Admin endpoints require no auth because host publish is loopback-only. | Tech Lead + Security Engineer | Closed, bounded by `ASSUMPTION-17` | Reopen only if host trust changes. |
| `ASSUMPTION-7` | Generation is serialized by one process-wide lock; idle unload uses the same lock. | Tech Lead | Closed | None. Do not weaken the lock for idle unload. |
| `ASSUMPTION-8` | Cache is written after full response assembly; errors unload immediately. With idle retention, success schedules/reset the timer instead of unloading. | Tech Lead | Closed | None. |
| `ASSUMPTION-12` | Cache limits are 30 days and 1024 MB. | Tech Lead | Closed | Shorter TTL/encryption deferred under `ASSUMPTION-17`. |
| `ASSUMPTION-16` | Miniconda base image plus `environment.yaml` is sufficient until a committed lock file is generated inside Docker. | Tech Lead + Delivery Manager | Closed; lock file deferred | Revisit only as explicit scope expansion. |

**Blocking question for final approval:** Is the host a trusted, single-user environment, or can other local users/processes reach `127.0.0.1:8000`? The user has chosen to keep this open, so this report remains a living draft.

## 3. Severity and effort scale

| Severity | Meaning |
| --- | --- |
| Critical | Directly exposes secrets, external access, or unauthenticated remote compromise. None identified under the current loopback-only boundary. |
| High | Service-wide denial of service, arbitrary admin action, or meaningful unsafe behavior reachable by a local caller. |
| Medium | Sensitive-data exposure, privilege/resource misuse, or defense-in-depth gap requiring local access or operational mistake. |
| Low | Narrow race, stale data, conditional log leakage, or misconfiguration with limited impact. |
| Info | Verified control or accepted residual with no action required. |

| Effort | Meaning |
| --- | --- |
| S | One module or documentation-only; existing tests can cover it; roughly hours. |
| M | Two or more modules, new tests, and a compatibility check; roughly a day. |
| L | New component, client/config contract change, or platform verification; multiple days. |

## 4. Implemented controls

| ID | Control | Evidence | Status |
| --- | --- | --- | --- |
| `SEC-I-01` | Host publish is loopback-only. | `docker-compose.yml:12-13`; verified by `docker compose --profile serve config --no-interpolate`. | Implemented, verified |
| `SEC-I-02` | `HF_TOKEN` comes from untracked `private.txt` through Compose `env_file`; it is never passed as a CLI argument or baked into the image. | `docker-compose.yml:14-16`, `downloader.py`, `.gitignore`, `.dockerignore`; `AC-54`. | Implemented, verified |
| `SEC-I-03` | Resource preflight fails closed for disk/RAM and returns stable `503`. | `local_llm/preflight.py`, `local_llm.yaml:47-52`, preflight tests; `AC-51`. | Implemented, verified |
| `SEC-I-04` | Completed responses are cached with full-request keys, 30-day TTL, 1 GiB cap, and no bypass. Prompt text participates in the SHA-256 key but is not stored verbatim. | `local_llm/cache.py`, `local_llm.yaml:34-40`; `AC-49`. | Implemented, verified |
| `SEC-I-05` | Weights and cache files are ignored; config is mounted read-only. | `.gitignore`, `.dockerignore`, `docker-compose.yml:21-24`; `git check-ignore -v`. | Implemented, verified |
| `SEC-I-06` | Error envelopes sanitize exception text; no prompts, paths, stack traces, or token values are returned. | `local_llm/error_mapper.py`, `local_llm/composition.py`, error-mapping tests. | Implemented, verified |
| `SEC-I-07` | One process lock serializes generation, admin lifecycle, and idle unload; contention returns `503`. | `local_llm/composition.py`, `local_llm/locking.py`, `chat_routes.py`, `runtime.py`; `AC-52`, `AC-60`. | Implemented, verified |
| `SEC-I-08` | Idle retention is bounded to `0 < idle_unload_seconds <= 86400`, default `180`. | `local_llm/config.py`, `local_llm/runtime.py`, `local_llm.yaml:8-10`; config/runtime tests; `AC-57`. | Implemented, verified |
| `SEC-I-09` | Idle expiry callback marks registry state while the lifecycle lock is held, preventing stale callback/runtime drift. | `local_llm/runtime.py:219-245`; runtime tests. | Implemented, verified |
| `SEC-I-10` | Errors and cache-put failures unload immediately and cancel the idle timer. | `chat_routes.py`; cache-put/runtime-error tests; `AC-60`. | Implemented, verified |
| `SEC-I-11` | Cache hit refreshes the idle timer without load or inference; preflight is skipped only while the same alias is resident. | `chat_routes.py`; three-prompt and cache-hit tests; `AC-58`. | Implemented, verified |
| `SEC-I-12` | Public-only Hugging Face download is enforced before invoking `hf`. | `local_llm/downloader.py`, `local_llm/config.py`; downloader tests. | Implemented, verified |
| `SEC-I-13` | OpenCode local providers use supported V2 fields with `96`-token caps. | `opencode.jsonc`, `tests/test_opencode_config.py`. | Implemented, verified |
| `SEC-I-14` | All development, test, deployment, and verification commands run inside Docker. | `AGENTS.md`, `Dockerfile`, `docker-compose.yml`; `AC-53`. | Implemented, verified |
| `SEC-I-15` | Aliases, public repository IDs, and GGUF filenames are validated; path separators and non-`.gguf` filenames are rejected. | `local_llm/config.py` alias/repo patterns and filename validation; config tests. | Implemented, verified |
| `SEC-I-16` | Cache writes are atomic; corrupt or schema-mismatched entries are treated as misses and removed; TTL and LRU eviction are enforced. | `local_llm/cache.py` `_atomic_write`, `_read_entry`, `get`, and `evict`; cache tests. | Implemented, verified |
| `SEC-I-17` | An existing GGUF is not re-downloaded. | `local_llm/downloader.py` existence check before CLI invocation; downloader tests. | Implemented, verified |
| `SEC-I-18` | Application logging does not emit `HF_TOKEN`, prompts, paths, or stack traces; normal download logs contain only alias/repository/filename metadata. | Static review of `local_llm/`, `local_llm/composition.py` logging setup, container-environment tests, and prior real-model smoke. Residual `hf` subprocess output is tracked in `SEC-G-17`. | Implemented, verified for application code |
| `SEC-I-19` | Settings are validated and frozen after load; the configured download CLI defaults to `hf`. | `local_llm/config.py`, `local_llm.yaml`, config tests. | Implemented, verified |

## 5. Open and needed mitigations

The report distinguishes accepted controls, open recommendations, and deferred scope. Contract-preserving small fixes are not approved work under this documentation-only MVP because `ASSUMPTION-17` is open and the user requested a report.

| ID | Finding | Severity | Effort | Owner module | Recommendation | Current status |
| --- | --- | --- | --- | --- | --- | --- |
| `SEC-G-01` | Slow `hf` download runs while the process-wide lock is held in `_resolve_path`, so one cold download can block every chat/admin route until `lock_timeout_seconds` and return `503`. Idle unload blocks on the same lock. | High | S | C7 ChatRoutes | Move download outside the lock, then re-acquire the lock and re-check existence before registry mutation. Keep fail-closed download preflight. | Open; F-01 remains open; tech lead approves as safe with recheck |
| `SEC-G-02` | `.cache/responses/` is created with default directory permissions; files use `mkstemp` `0o600` but the directory itself is not explicitly restricted. | Medium | S | C9 ResponseCache | Create directory with `mode=0o700`; optionally `chmod` existing directory. Document that Windows bind mounts govern host-side ACLs. | Open |
| `SEC-G-03` | `DELETE /admin/models/{alias}` unregisters the alias but leaves cache entries containing that alias and response text. | Medium | S | C8 AdminRoutes + C9 ResponseCache | Add `remove_alias(alias)` to C9 and call it from admin `remove` under the process lock. Weights remain a separate operational concern. | Open |
| `SEC-G-04` | No server-side maximum generation or request body cap; OpenCode’s `96`-token cap is client-side only. A local caller can request a large generation and hold the lock. | Medium | S–M | C7 ChatRoutes | Validate request body size and cap `max_tokens` server-side, with stable `422`; keep OpenCode caps unchanged. | Open; identified by tech lead and security |
| `SEC-G-05` | Plain `docker compose config` resolves and prints `HF_TOKEN`; operational sharing can leak it. | Medium | S | C11 DockerRuntime + docs | Keep requiring `--no-interpolate` for shared output; never paste interpolated config into issues or chat. | Guidance implemented; residual operational risk |
| `SEC-G-06` | Idle retention keeps decoded GGUF bytes in process RAM after response for up to 24 hours by configuration. | Low–Medium | Info | C6 LlamaRuntimeAdapter | Accepted trade-off from `DECISION-21`. Keep bounded `<= 86400`, admin unload, and error unload. Monitor RAM on constrained hosts. | Accepted; no code change |
| `SEC-G-07` | `_expire_idle` uses a blocking lifecycle-lock acquire. | Info | S (docs) | C6 LlamaRuntimeAdapter | Document that blocking is required for safe unload; do not substitute a timed skip, which could unload mid-generation or miss the idle deadline. | Accepted; documented here |
| `SEC-G-08` | Cache fingerprint is `size:mtime_ns`; a same-size, same-mtime GGUF swap can replay stale cached output. | Low–Medium | M | C9 ResponseCache + C6 runtime | Streamed SHA-256 fingerprint cached per alias/path; accept one-time cache-key invalidation. | Deferred by existing decision |
| `SEC-G-09` | Container runs as root. | Medium on shared host | M | C11 DockerRuntime | Add non-root `USER` plus Windows bind-mount ownership verification or UID/GID-matched runtime user. | Deferred under `ASSUMPTION-17` |
| `SEC-G-10` | No auth or rate limiting; any local host process can call chat and admin routes. Admin routes can trigger downloads, unload models, or unregister aliases. | High for admin abuse; Medium for chat; High on shared/remote host | M–L | New auth module + C7/C8 + C12 OpenCode headers | Add only after host-trust decision. Key storage/rotation and OpenCode header wiring required. | Deferred under `ASSUMPTION-17` |
| `SEC-G-11` | Cache encryption and shorter TTL are not implemented. | Medium on shared host | M–L | C9 ResponseCache + C2 config | Add encryption-at-rest and configurable shorter TTL only after host-trust decision. | Deferred under `ASSUMPTION-17` |
| `SEC-G-12` | Synchronous public-model download has no approved-alias gate, async queue, or cancellation endpoint. | Low–Medium | M–L | C8 AdminRoutes + C4 downloader | Prefer startup-configured aliases; add gating/queue/cancellation only as explicit scope expansion. | Deferred |
| `SEC-G-13` | No cache administration endpoint or per-request bypass; operators rely on TTL, LRU eviction, or manual file deletion. | Low | M | C8 AdminRoutes + C9 ResponseCache | Add scoped cache purge/bypass only after product decision. | Deferred |
| `SEC-G-14` | Arbitrary public GGUF bytes are loaded by llama.cpp; public-only enforcement is a supply-chain boundary, not a safety guarantee. | Low–Medium | S (docs) | C4 downloader + C14 runtime boundary | Use trusted public repos and filenames; user verifies model size before load. Keep GGUF-only and fail-closed preflight. | Accepted and documented |
| `SEC-G-15` | Lock contention returns `503` under sustained load. | Low | Info | C7 ChatRoutes | Document `server.lock_timeout_seconds: 600`; do not add concurrency in MVP. | Accepted |
| `SEC-G-16` | TOCTOU between `preflight.check_download` and `downloader.download`: disk space can be consumed after the check passes. | Low | S | C5 ResourcePreflight + C4 downloader | Re-check free space immediately before writing, or tolerate `ENOSPC` cleanly; accepted for MVP while downloads are synchronous and loopback-only. | Accepted residual; F-05 partial |
| `SEC-G-17` | `hf` subprocess output inherits container stdout/stderr. The application logs no token, but a future CLI could print a credential-bearing URL. | Low | S | C4 downloader + C11 logging | Capture or redact CLI diagnostics if the tool changes; continue never passing the token as a CLI argument and never pasting interpolated Compose output. | Conditional residual |

## 6. Review provenance / team memory

| Source | Recorded conclusion | Current status in this report |
| --- | --- | --- |
| Independent Security Engineer assessment, 2026-11-27 | Verified 15 implemented controls, including loopback publish, token hygiene, input validation, fail-closed preflight, public-repo-only download, sanitized errors, bounded idle retention, atomic cache writes, and no application token/prompt logging. Open gaps G-1–G-9 and prior findings F-01–F-06 were rechecked. | Implemented controls → `SEC-I-01`–`SEC-I-19`; gaps → `SEC-G-01`–`SEC-G-17`. No code-change blocker; fresh Docker verification and `ASSUMPTION-17` remain gates to final sign-off. |
| Security team memory F-01 | Process lock held during synchronous download; idle worker blocking acquire remains open. | `SEC-G-01` (High, open). |
| Security team memory F-02 | Longer model residency is now bounded by `idle_unload_seconds <= 86400`. | `SEC-I-08` and `SEC-G-06` (accepted residual). |
| Security team memory F-03 | Registry/idle callback race was fixed by capturing the callback under the idle-state lock and rechecking before/after lifecycle-lock acquisition. | `SEC-I-09` (fixed). |
| Security team memory F-04 | Cache TTL and size cap exist; directory permissions and alias-scoped purge remain open. | `SEC-I-04`, `SEC-G-02`, `SEC-G-03`. |
| Security team memory F-05 | Upper bound for load preflight and idle retention is implemented; download TOCTOU remains. | `SEC-I-03`, `SEC-I-08`, `SEC-G-16`. |
| Security team memory F-06 | No token or prompt logging was found in application code, CLI arguments, or normal download logs. | `SEC-I-18`; subprocess-output residual in `SEC-G-17`. |
| Independent Engineering review | No blocking defects; lock ordering consistent; stale timers safe; registry consistency maintained; low-severity admin `remove` note. | No blockers; admin `remove` note → `SEC-G-03`. |
| Tech Lead final review | Ready to commit; blocking lock acquire required; callback under lock confirmed; admin `remove` acknowledged and deferred. | `SEC-G-07` documented; `SEC-G-01`/`SEC-G-03` kept as recommendations. |
| Delivery Manager review | Items 1–3 and 5–7 verified by `AC-49`–`AC-56`; item 4 by `AC-57`–`AC-61`; `ASSUMPTION-17` must stay open. | Reflected in approval status and maintenance triggers. |

## 7. Acceptance criteria for the security posture

Each criterion is one observable behavior. `SEC-*` criteria define what must be true for this report or a future mitigation to be accepted; they are not runtime changes by themselves.

| ID | Observable behavior | Owner module | Status |
| --- | --- | --- | --- |
| `SEC-1` | `docker compose --profile serve config --no-interpolate` shows `host_ip: 127.0.0.1` and no token value. | C11 DockerRuntime | Implemented |
| `SEC-2` | `private.txt`, `models/`, and `.cache/responses/` are reported ignored by `git check-ignore -v`. | C11 + Git config | Implemented |
| `SEC-3` | A failed disk or RAM preflight returns `503` and writes no model bytes. | C5 ResourcePreflight + C7 ChatRoutes | Implemented |
| `SEC-4` | A completed-response cache entry is written only after full response assembly and is replayed without inference on a hit. | C9 ResponseCache | Implemented |
| `SEC-5` | A same-alias prompt or cache hit refreshes the idle deadline; a different-alias prompt, admin unload, or error unloads immediately. | C6 runtime + C7 chat routes | Implemented |
| `SEC-6` | `idle_unload_seconds` outside `0 < value <= 86400` is rejected by both config and runtime validation with the field name in the error. | C2 ConfigLoader + C6 runtime | Implemented |
| `SEC-7` | Downloading a missing GGUF does not hold the process-wide lock; the lock still guards the existence re-check and registry mutation. | C7 ChatRoutes | Planned (`SEC-G-01`) |
| `SEC-8` | `.cache/responses/` has mode `0o700` when created by the container. | C9 ResponseCache | Planned (`SEC-G-02`) |
| `SEC-9` | `DELETE /admin/models/{alias}` removes cache entries for that alias and leaves unrelated entries intact. | C8 AdminRoutes + C9 ResponseCache | Planned (`SEC-G-03`) |
| `SEC-10` | A request exceeding the server-side generation or body cap returns stable `422` without acquiring the model lock. | C7 ChatRoutes | Planned (`SEC-G-04`) |
| `SEC-11` | This report records the current answer to `ASSUMPTION-17` and does not mark it closed without a product decision. | Product Lead | Open by user choice |
| `SEC-12` | Auth/rate limiting, non-root container, and cache encryption remain deferred until `ASSUMPTION-17` is decided. | Product Lead + C11/C9 | Deferred |
| `SEC-13` | Content-hash GGUF fingerprints are introduced only with a documented cache-key invalidation note and tests. | C9 ResponseCache | Deferred |
| `SEC-14` | Every deployment prep, PR merge, monthly cache audit, and host-trust change records a review entry in this report. | Documentation owner | Maintenance rule |
| `SEC-15` | Invalid aliases, non-public repository IDs, path-separator filenames, and non-`.gguf` filenames are rejected before download or registration. | C2 ConfigLoader | Implemented |
| `SEC-16` | A corrupt or schema-mismatched cache entry is removed and treated as a miss; a successful write is atomic. | C9 ResponseCache | Implemented |
| `SEC-17` | An already-present GGUF is skipped rather than re-downloaded. | C4 HfCliDownloader | Implemented |
| `SEC-18` | Application logs contain no token, prompt, path, or stack trace; normal download logs contain only alias/repository/filename metadata. | C10 error mapping + C4 downloader | Implemented; `SEC-G-17` residual |
| `SEC-19` | A download preflight TOCTOU review is repeated if downloads become concurrent or move outside the lifecycle lock. | C5 ResourcePreflight + C4 downloader | Deferred accepted residual |

## 8. Maintenance and review cadence

| Trigger | Action | Record |
| --- | --- | --- |
| Every deployment prep | Run `docker compose --profile serve config --no-interpolate`; verify loopback publish, `env_file`, no token output. | Update “Last reviewed” and evidence line. |
| Every PR merge | Run `docker compose run --rm tests`; all tests must pass, including `AC-57`–`AC-61`. | Add a brief verification note if security-relevant behavior changed. |
| Monthly, or when cache config changes | Audit `.cache/responses/` size and TTL behavior; confirm no secrets in prompts. | Update `SEC-G-02`/`SEC-G-03`/`SEC-I-04` if needed. |
| Any Compose/Dockerfile/config change | Re-check host publish, mounts, token injection, root-user status, and CLI output handling. | Update risk register and `SEC-1`/`SEC-2`/`SEC-12`/`SEC-G-17`. |
| Host usage change | Reassess `ASSUMPTION-17`. If shared or remotely reachable, open product scope for `SEC-G-09`–`SEC-G-11` and `SEC-G-10`. | Do not silently close the assumption. |
| Quarterly | Review open gaps, severity, effort, and deferred decisions. | Update status table and review provenance. |

## 9. Verification evidence for this report

- Report baseline: commit `524844a Add 180-second idle retention for loaded models`.
- Docker test gate: re-run for this report; `docker compose run --rm tests` passed all 110 tests (`Ran 110 tests in 2.834s ... OK`), including idle-retention, validation, cache-integrity, error-envelope, and container-environment tests.
- Compose gate: re-run for this report; `docker compose --profile serve config --no-interpolate` passed without printing `HF_TOKEN`. Re-check at every deployment prep and confirm the published host address remains `127.0.0.1:8000`.
- Ignore gate: re-run for this report; `git check-ignore -v private.txt models/ .cache/responses/` reported all paths ignored through `.gitignore`. Re-check before every commit.
- This report is documentation-only; no runtime behavior was changed by adding it. The security assessment was read-only, so a future mitigation must run its own acceptance tests before changing this status table.

## 10. Approval status

**Not approved as a final security sign-off.** The report is publishable as a living draft because scope, criteria, evidence, and exclusions are explicit, but `ASSUMPTION-17` remains open by user decision. Final approval requires a product decision on host trust; no runtime mitigation should be implemented solely because it appears in the open-mitigation table.
