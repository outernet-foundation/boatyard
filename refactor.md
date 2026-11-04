# Refactor: assembly interpolation and validation (in-progress ADR)

State: in progress, spanning sessions. "Locked" entries are decisions; "Open questions"
is the remaining space, ordered cheapest-first. This file is the resume point: read it,
then `git log --oneline refactor`.

## Context: the problem being decided

Bundles are rendered with named interpolation holes (`${ALPINE_IMAGE:?err}`) instead of
baked digests, so the consuming machine can pick platform manifests at `up` time. Hole
names are global family vocabulary. One env file (per-run, synthesized from the
consumer's lock plus any path-override producer locks) fills every hole in the assembly.

Consequences of the shared vocabulary:

- Uniformity is structural: two bundles cannot run different digests of the same
  third-party image. For remote bundles this is not a warning — it is unrepresentable.
- `write_assembly_env_file`'s collision fence refuses any key filled by two locks.
  Because every compose-scoring repo must carry `ALPINE_IMAGE` in its lock
  (score_compose refuses generation without it), any consumer with local compose
  entries can never path-override a sibling bundle. Cross-repo dogfood override —
  confirmed a first-class workflow, not a hack — is structurally forbidden, and the
  fence's only remedy is "don't do that".

## Decision (locked): namespaced holes + carried pins

Direction locked this session ("B-with-carried-pins"):

1. Producer score render leaves holes namespaced per entry
   (`{QUALIFIED_NAME}_ALPINE_IMAGE`, spelling per the `bundle_lock_key` law).
2. The published bundle artifact carries its pin requirements (consumed hole names with
   index digest + platform manifests) — transitively digest-pinned with the artifact.
3. `boatyard lock` records carried pins into the consumer's lock when resolving the
   bundle digest. `up` reads only the lock; never the network, for pin facts.
4. One precedence rule everywhere (remote bundles and path overrides alike):
   explicit consumer declaration wins; carried/producer pins fill undeclared names.
5. The collision fence is deleted. Divergence (consumer pin differs from carried pin)
   warns instead of refusing.

What this preserves by construction: one-bump CVE response (relock the consumer, the
fan-out writer copies the new digest into every selected entry), centralized pinning
authority, platform selection, the mirror/lock security model.

Accepted costs (walked and accepted this session):

- One-time family-wide artifact break: old bare-hole digests become unfillable; plain
  refusal for stragglers, no bridge (fresh-repo culture). Ruled irrelevant.
- The per-run env file becomes a synthesis (fan-out over selection and overrides), not a
  projection of the lock. "What fills this hole" gains a rule instead of a grep.
- Divergence becomes possible on request (override/undeclared) and silent; the warn in
  point 5 is the announced-ness. Bundles-only consumers stop hand-pinning `.env` lines;
  the fix is declaring `base_images` (pins move from rotting `.env` into the CI-refreshed
  lock — a net improvement).

## Locked: validation analysis outcomes

From the independent reachability audit of the four `render_compose` validations:

- Profile validation: correctly placed and shaped. Profile names live in bundle
  artifacts, selection in per-machine `.env`; they coexist only at `up`. Compose
  silently no-ops unknown profiles; the check converts that into a loud refusal.
  Closed: no change.
- Route/port validation stories (all real): group-route x option mismatch (a group's
  routes ride with whichever option is selected, so group routes must hold for every
  option's bundle); remote bundle drift across relock; typo; override-in-development;
  profile-hidden upstreams (verified live: `config --services` is profile-sensitive —
  the refusal text conflates "no such service" with "hidden behind an unselected
  profile"; text ruled accurate enough, no change).
- The obviously-correct earlier tier: score-time validation of every local entry's
  routes/ports against its own rendered service set, at full group-aware scope
  (each option's rendering validated against `option.routes + group.routes`, the fold
  `Config.resolve_services` performs). Makes up-time route/port failure for local
  entries structurally impossible behind the `--check` freshness gate. NOT yet
  implemented — awaiting authorization (Q2).

## Landed this session (branch `refactor`, ahead of origin)

- `Collapse assembly render: model_dump port stanzas, drop identity copies` — audit
  round 1. Includes the pyright lesson: the two "identity copy" comprehensions are
  inference anchors (bidirectional typing to `list[Yaml]`/`dict[str, Yaml]` under list/
  dict invariance); kept as comprehensions with one-line why-comments. Do not
  "simplify" them again.
- `Fold the three compose validation refusals into one helper` — `_refuse_unknown`
  with named byte-stable refusal-template constants (RUF027 rejects inline braced
  template literals; constants are the sanctioned shape and make the phrasing law
  greppable).

## Open questions, cheapest-first

Ordering rationale: each question closes strictly more space per unit of reader effort
than the one after it; earlier answers shrink or absorb later questions. Answering Q1
with "accept all" closes five forks in one word.

- **Q1 (batch): accept sub-decision recommendations 1-5?** These are walked with
  rationale in the session record; each has a recommendation and none requires org
  knowledge the session lacked:
  1. Vehicle: manifest annotations, attached post-`docker compose publish` via
     `crane mutate` (the pipeline already post-processes with `crane tag`; read side is
     `crane manifest`, no layer pull). Alternative rejected: companion layer
     (`crane append`) — heavier both directions.
  2. Carried scope: consumed-holes-only (the render knows every hole it emits); not the
     producer's whole image lock (phantom authority).
  3. Recording: structured lock `bundles:` section `{ref, pins: {NAME: {index,
     platforms}}}`; annotations read only by `lock`; `up` stays lock-pure.
  4. Precedence: consumer-declaration-wins; **no** per-entry `.env` escape hatch in v1
     (the `--env-file .env --env-file <tmp>` ordering already makes hand keys lose to
     machine keys; divergence is expressible by not-declaring or path-overriding).
  5. Conflict signal: warn (never refuse — refuse would recreate the fence post-CVE,
     when every carried pin "differs" from the fresh declaration), at both `lock`
     (CI-visible in `--check`) and `up` (divergence live now).
  Amending one item reopens only that item.
- **Q2 (authorization): implement score-time group-aware route/port validation now?**
  Independent of B; implementable immediately; also de-fangs Q4.
- **Q3 (scope add, cheap only before the vehicle exists): carry service names in the
  same annotations?** If yes, `lock` can validate route upstreams and port owners for
  remote bundles against the recorded vocabulary — absorbing the lock-time-drift
  judgment call (formerly JC2) for near-zero marginal cost. Decide before building the
  vehicle, not after.
- **Q4 (schema fork, mostly de-fanged by Q2): groups keep routes?** Alternative is
  routes-only-on-entries (each option restates its surface; cross-option contract
  disappears). With Q2 landing, group x option mismatch is CI-caught, so keeping groups
  costs little. Lean: keep.
- **Q5 (housekeeping batch, propose close-all as no-change):** route/port refusal text
  mentioning profiles (text accurate; byte-stable law); dedup of identical
  producer/consumer lock paths (unreachable in practice); `--profile` flags into the
  validation call (verified behaviorally redundant — env/`.env` carriage already
  activates profiles during `config --services`).
- **Q6 (session residue, independent of the ADR): extract `render_caddyfile`, and
  decide the rule-text line it exposes.** First-audit finding never resolved: the
  17-line Caddyfile render block inside `render_compose` is the clearest extraction,
  but AGENTS-SHARED's "inline single-call helpers" rule as written forbids it, while
  compose.py already carries single-call helpers owning coherent units
  (`write_assembly_env_file`, `namespaced_route`). One deciding line either way:
  extract + soften the rule text to target trivial pass-throughs, or keep inline.
  Cheap, zero dependencies, pure readability.

## Implementation surface

Once Q2 is authorized (independent of B): the score render loop iterates
`local_service_entries()`, which drops group context — the group-aware walk (the fold
`resolve_services` performs, applied to every option rather than a selection) must be
plumbed into the loop; validate each option's rendering against its leaf routes/ports;
`compose.py`'s up-time teeth remain for remote drift and path overrides.

Once Q1 locks (B):

- `config.py`: the one namespaced-hole join (mirrors `bundle_lock_key` precedent).
- `score_compose.py`: namespaced holes including the barrier; emit the carried-pin set
  (consumed holes, producer lock digests + recorded platforms).
- `verbs/publish.py`: `crane mutate --annotation` between publish and tag/digest record.
- `lock.py` + `config_lock.py`: read annotations at bundle resolve; structured
  `bundles:`; non-digest law extends to carried pins.
- `compose.py`: fan-out writer (consumer lock keys x selected entries, minus
  path-overridden entries whose producer lock contributes namespaced keys); delete the
  `filled_by` fence; pin-conflict warning.
- `up`-time divergence warning (one line when a live assembly runs a carried pin that
  differs from a consumer declaration).
- Boundary: `major_minor` bump; producers republish, then consumers relock; old
  artifacts get the plain unfilled-hole refusal.

Verify before build (engineering risk, not user questions):

- `crane mutate --annotation` against compose-bundle media types (docker compose
  `publish` constructs the manifest; mutation is post-hoc) — and registry annotation
  size limits against a per-bundle mini image-lock.

Ratification path: when the ADR closes, hardened facts move into the relevant AGENTS.md
sections (compose assembly, lock, publish); this file is then deleted — it is an
in-progress record, not a permanent doc.
