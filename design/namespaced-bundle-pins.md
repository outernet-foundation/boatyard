# Namespaced bundle pins (carried-pin design)

Parked pre-implementation design. Build only when a trigger holds:

- Stage A trigger: the lock-collision fence in `compose.py` (`write_assembly_env_file`)
  blocks real dogfood work — a consumer repo with its own image lock path-overriding a
  sibling bundle, which the fence always refuses.
- Stage B trigger: the first real need for two remote bundles in one assembly to run
  different digests of the same third-party image — unrepresentable today.

Direction was converged and walked with the consumer in the originating session; all
sub-decisions below carry recommendations, none formally locked. Blast radius was ruled
irrelevant to the decision; the punt was a scope call.

## Problem

Bundles render with named interpolation holes (`${ALPINE_IMAGE:?err}`) rather than baked
digests, so the consuming machine picks platform manifests at `up`. Hole names are
global family vocabulary; one per-run env file fills every hole. Consequences:

- Uniformity is structural: two bundles cannot run different digests of the same image.
  For remote bundles this is unrepresentable, not merely unannounced.
- The fence refuses any key filled by two locks. Every compose-scoring repo must carry
  `ALPINE_IMAGE` in its lock (score refuses generation without it), so any consumer with
  local compose entries can never path-override a sibling bundle. Dogfood override —
  confirmed first-class — is structurally forbidden.

## Design

1. Producer score render leaves holes namespaced per entry (`{QUALIFIED_NAME}_NAME`,
   spelling per the `bundle_lock_key` law; barrier included; k8s untouched — no env file).
2. The published artifact carries its pin requirements (consumed hole names with index
   digest + platform manifests), transitively digest-pinned with the artifact.
3. `boatyard lock` records carried pins into the consumer's lock at bundle-digest
   resolve; `up` reads only the lock, never the network, for pin facts.
4. One precedence rule everywhere (remote and path-override alike): explicit consumer
   declaration wins; carried/producer pins fill undeclared names. Uniformity is
   expressed by declaring; divergence by not declaring or by path-override.
5. The fence is deleted; consumer/carried disagreement warns (lock: CI `--check`
   visible; up: divergence live in this assembly). Never refuse — post-CVE relock makes
   every carried pin "differ", and refusing would recreate the fence one level up.

Preserved by construction: one-bump CVE response (relock the consumer; the fan-out
writer copies the fresh digest into every selected entry), centralized pin authority,
platform selection, the mirror/lock security model.

Accepted costs: one-time family-wide artifact break (old bare-hole digests unfillable,
plain refusal, no bridge); the per-run env file becomes a synthesis (fan-out over
selection and overrides) rather than a projection of the lock; divergence possible on
request and silent-but-warned.

## Sub-decision recommendations (walked, not locked)

1. Vehicle: manifest annotations, stamped post-`docker compose publish` via
   `crane mutate` (before the existing `crane tag`/digest record; mutation changes the
   digest). Read via `crane manifest`, no layer pull. Companion layer (`crane append`)
   rejected: pull-to-read plus a foreign layer in a compose-owned artifact.
2. Carried scope: consumed-holes-only, not the producer's whole image lock — the render
   knows every hole it emits; whole-lock carrying grants phantom authority and can fire
   phantom conflict warnings.
3. Recording: structured lock `bundles:` section `{ref, pins: {NAME: {index,
   platforms}}}`; non-digest-pinned law extends to carried pins; annotations read only
   by `lock`.
4. Precedence: consumer-declaration-wins; no per-entry `.env` escape hatch in v1 —
   `compose_command` passes `--env-file .env --env-file <tmp>` (later wins), so hand
   namespaced keys would be silently ignored without a broader precedence change.
5. Conflict signal: warn at both `lock` and `up` (see design point 5).

Rider (cheap only before the vehicle exists): carry service names in the same
annotations; `lock` then validates route upstreams and port owners for remote bundles
against the recorded vocabulary — lock-time drift signal for near-zero marginal cost.

## Staging

- Stage A — no artifact break, no family rollout: annotations + carried pins +
  record-at-lock + precedence-with-warning, bare hole names kept. Fixes both confirmed
  pains (dogfood override works, warned; bundles-only consumers stop hand-pinning
  `.env` — declaring `base_images` moves pins into the CI-refreshed lock). Complete
  useful stop.
- Stage B — the break: hole rename + fan-out writer + fence deletion + family-wide
  producer republish / consumer relock at a `major_minor` boundary. Buys remote-bundle
  divergence, the Stage A-inexpressible case.

## Implementation surface (Stage B, superset)

- `config.py`: the one namespaced-hole join (mirrors `bundle_lock_key`).
- `score_compose.py`: namespaced holes (every emitter, the barrier, the `$`-doubling
  exception lists); emit the carried-pin payload.
- `verbs/publish.py`: `crane mutate --annotation` between publish and tag/digest.
- `lock.py` + `config_lock.py`: annotation read at resolve; structured `bundles:`;
  ripple through `up.py`'s bundle probe skip logic.
- `compose.py`: fan-out writer (consumer keys x selected entries, minus path-overridden
  entries whose producer lock contributes namespaced keys); fence deletion; warnings.

Verify before build: `crane mutate --annotation` against compose-bundle media types;
registry annotation size limits vs a per-bundle mini image-lock.

Size: ~300-400 source lines across five new parts; test churn larger (every override
and lock fixture assumes bare holes and flat bundle locks); Stage B adds the family-wide
rollout, which dominates.

When built: hardened facts fold into the co-located `AGENTS.md` (compose assembly,
lock, publish sections); this file is deleted.
