# Model assignment plan

Status: **applied**. Agent `model:` lines were updated to the selectors below.

## Scope and constraints

- Use only models already listed in `opencode.jsonc`: `big-pickle`, `tidepool-alpha-free`, `iceman-free`, `deepseek-v4.1-flash-free`, `beam-free`, `flint-40b-free`, `hy4-flash-free`, `mimo-v2.6-flash-free`, `nemotron-3.5-lightning-free`, and `nemotron-3.5-super-vl-free`.
- Selector format is `opencode/<model-id>` with an optional `#<reasoning-effort>` variant.
- A variant is assigned only when the model catalog reports a matching effort value. Models without a published effort list get no variant.
- No `llai/...` models are introduced.
- Agent files remain the only place where model assignments are stored. `opencode.jsonc` is unchanged.

## Catalog facts used

| Model | Published reasoning efforts | Notable fit |
| --- | --- | --- |
| `big-pickle` | none published | Deliberate analysis and multi-step problem solving |
| `tidepool-alpha-free` | `minimal`, `low`, `medium`, `high`, `xhigh`, `max` | Stealth reasoning with adjustable depth |
| `iceman-free` | none published | Long text output, large context |
| `deepseek-v4.1-flash-free` | `low`, `high`, `max` | Reasoning, coding, agentic tasks, image understanding |
| `beam-free` | `low`, `medium`, `high`, `xhigh`, `max` | Sparse MoE reasoning for coding and agents |
| `flint-40b-free` | `low`, `medium`, `high`, `xhigh`, `max` | Coding agent model |
| `hy4-flash-free` | `none`, `high` | Everyday agent and productivity tasks |
| `mimo-v2.6-flash-free` | none published | Multimodal coding and long-context automation |
| `nemotron-3.5-lightning-free` | none published | Fast reliable agentic tasks |
| `nemotron-3.5-super-vl-free` | `none`, `minimal`, `low`, `medium`, `high`, `xhigh` | Vision-language reasoning and agentic tool use |

## Proposed assignments

| Agent | Proposed selector | Depth | Rationale |
| --- | --- | --- | --- |
| `product-lead` | `opencode/tidepool-alpha-free#high` | high | Deliberate product framing and testable acceptance criteria |
| `ui-ux-designer` | `opencode/nemotron-3.5-super-vl-free#high` | high | Vision-language fit for realistic mock-ups and visual review |
| `developer` | `opencode/beam-free#high` | high | Coding and agentic implementation with large context |
| `data-engineer` | `opencode/deepseek-v4.1-flash-free#high` | high | Coding, pipelines, and agentic tool use |
| `delivery-manager` | `opencode/nemotron-3.5-lightning-free` | default | Fast planning, tracking, and status work |
| `documentation` | `opencode/hy4-flash-free#high` | high | Productivity writing with long context |
| `explore` | `opencode/mimo-v2.6-flash-free` | default | Multimodal repository discovery and long-context automation |
| `acceptance-reviewer` | `opencode/big-pickle` | default | Deliberate evidence-based gatekeeping |
| `tech-lead` | `opencode/beam-free#xhigh` | xhigh | Architecture boundaries and simplification need deeper reasoning |
| `engineering-reviewer` | `opencode/tidepool-alpha-free#xhigh` | xhigh | Independent correctness and architecture review |
| `security-engineer` | `opencode/beam-free#max` | max | Threat modeling and reachable attack-path analysis |
| `database-architect` | `opencode/flint-40b-free#high` | high | Schema, migration, query, and integrity review |
| `quality-engineer` | `opencode/flint-40b-free#high` | high | Risk-based test design and failure-mode coverage |

## Application procedure

1. Edit the `model:` line in each `.opencode/agents/<agent>.md` to the proposed selector.
2. Reload configuration with `/reload`; configuration changes apply without restarting the service.
3. Run one smoke task per agent and confirm no unknown-variant or model-unavailable errors.
4. Record exact commands and outcomes in the delivery log.
5. If a free model is unavailable, fall back to the same role tier from the free list and record the substitution.

## Risks and open questions

- Free models are limited-time offerings; availability may change without notice.
- Stealth models publish no stable effort list, so no reasoning variant is proposed for `big-pickle`, `iceman-free`, or `mimo-v2.6-flash-free`.
- `nemotron-3.5-lightning-free` and `nemotron-3.5-super-vl-free` are NVIDIA trial endpoints; do not send personal or confidential data to them.
- Model availability requires an OpenCode Console account with free models enabled.
- Variant names are catalog metadata, not arbitrary labels; unknown variants fail model resolution.
