# compaction.md — full session record for the image-declaration design line

Hydration document for the next session. The plan is `refactor-redux.md` (same directory);
this file holds everything the plan deliberately does not: the full decision history with
reversals, both implementation post-mortems, repo ground truths, the open fork, and the
operator's rulings and working style. Nothing of this line is implemented — two attempts
were made and reverted whole; the working tree is clean at commit `1385057`.

## Current state

- Branch `refactor-redux` in this repo. Commits, in order: `a814f71` (plan added),
  `010f45d` (wrench section dropped), `7c2c3c0` (base-registry/fan amendments folded),
  `1385057` (observed-universe pinning after attempt 1's post-mortem).
- Attempt 1 (unstaged, +600/−217 across 14 files) — reviewed, post-mortemed, reverted.
- Attempt 2 (unstaged, +517/−207 across 13 files) — reviewed, post-mortemed, reverted.
- The design is settled down to a short list of open questions (see "Open questions").
  The next session's job is to drill those, update the plan, and only then re-attempt.
- Operator instruction in force: the plan's acceptance test line ("net negative or
  near-zero in src/") is known false as written (see "The arithmetic"); fix or re-frame
  it when the plan is next touched. Do not implement before the drill.

## How this line started

Orientation request in `_derive` (`src/boatyard/models/config_file.py:471`, pre-refactor
numbering). The operator sensed "logic smeared across multiple files" around images,
workloads, and provisioners. Session findings, in order:

1. Code-level tangle: `variant_axes` computed 3×; `load_workloads` called 3× under two
   different lenses; `select_provisioner_files` 2×; `images`/`consumer_surfaces` stored as
   untyped phantom dict fields on ConfigFile; `_derive`'s per-entry block fused loading,
   selection, collision checks, and surface extraction.
2. Schema-level root cause (the real one): no document anywhere declares its relationship
   to the images it runs. The join exists only as naming conventions plus filesystem
   shape, reconstructed by (a) a directory walk for the build set, (b) string surgery on
   hand-written image refs (`_refuse_unknown_axis_refs`, the line
   `stem = image[:match.start()].rpartition("/")[2].removesuffix("-")`), and (c) ARG
   sniffing plus FROM-chain propagation for the variant fan.
3. The identifier "reconstructor" was spelled five ways joined only by convention
   (directory name, hand-typed ref string, `RECONSTRUCTOR_SHA`, bake/tree-tags vocabulary,
   repository path segment). The repository prefix `ghcr.io/outernet-foundation/placeframe`
   was hand-spelled 17× across placeframe score files plus once in boatyard.yml.
4. Four grammars spelled "what image does this container run":
   - `image: ghcr.io/.../api:${API_SHA}` (workload, built, no fan)
   - `image: ghcr.io/.../reconstructor-${TORCH_DEVICE}:${RECONSTRUCTOR_SHA}` (workload, built, fanned)
   - `image: ghcr.io/.../postgres:{{ env "POSTGRES_SHA" }}` (provisioner manifest, built)
   - `image: ${KEYCLOAK_IMAGE}` / `${LOKI_IMAGE:?err}` (pulled; workload and provisioner-services)
   - plus the pre-existing marker grammar `lock:` / `lock-image:` in chart values and one
     k8s provisioner (`stack/score/k8s/observability.yaml` uses `lock: ALLOY_IMAGE`).
5. First-principles decidable core (not judgment calls): the consumer→image edge can only
   live at the consuming document; the document composes zero characters of the reference;
   one grammar for pulled and built images (the `base_images`+lock+slot pattern was already
   the correct precedent — built images were the anomaly); membership/fan are closures with
   loud refusals. Judgment calls that remained: edge syntax, external-only images, fan home.

## The decisions, with full history (amendments and reversals included)

D1 — Where the image edge is declared. History: (1) ruled "the slot is the declaration"
(`image: ${RECONSTRUCTOR_IMAGE}`) on the keycloak-pattern precedent; (2) operator disliked
the `workloads/<name>/Dockerfile` locator convention, proposed per-container Dockerfile-path
annotations — accepted; (3) operator challenged the redundant second spelling (annotation +
slot) — conceded, ruling became: **`image:` is never authored anywhere; sources are declared
structurally and boatyard injects the composed reference at render.** Final shape: workload
containers declare sources via workload-metadata annotations (score annotations are
workload-level, so the annotation is a container-name→source mapping); provisioner
manifests declare sources via markers on the image field (`dockerfile:` new, `lock:`/
`lock-image:` existing); chart values keep lock markers only. `${X_SHA}` slots survive
solely as data uses (`SERVICE_VERSION: ${RECONSTRUCTOR_SHA}`). Unifying insight recorded
late: provisioners/chart values already refused literal images (`_check_literal_image`) —
workload containers were the only document kind without a structural grammar; the fix is
"do what base_images already does, everywhere."

D2 — Build-set membership. **Strict closure, one-sided error rule — the operator's explicit
ruling.** Build set = declared edges (annotations, provisioner markers, built base entries)
extended through `FROM ${KEY}` edges into built bases. No directory walk, and **no reverse
check**: an unreferenced Dockerfile is silently not built (same regime as an unlisted score
file). Forward failures refuse loudly at load (declared path missing). An intermediate
version of this ruling ("Dockerfile-but-never-consumed refuses") was explicitly retracted
by the operator as "the walk's worldview sneaking back in as a validator."

D3 — Fan (variant-ability) declaration. Three rulings in sequence:
(1) leaf Dockerfile ARG only, kill FROM-propagation — WRONG, reversed when facts showed all
device branching lives in the base (`neural-networks-base`), so a leaf `ARG TORCH_DEVICE`
would be an unused marker misdescribing its own file, and leaf-only safety requires
re-implementing propagation as an error check anyway;
(2) propagation-as-closure (fan = own ARG ∪ FROM ancestry) — correct semantics but then
superseded by;
(3) FINAL: **the fan is declared on the built base entry in boatyard.yml**
(`axis: {TORCH_DEVICE: [cuda, rocm]}`), verified against a matching ARG in that Dockerfile,
inherited one hop through FROM edges. Built bases FROM pulled entries only (built-on-built
chains refuse); a Dockerfile FROMs at most one built entry; a built entry declares at most
one axis; one global check holds equal axis names to equal value sets. The ARG in the
Dockerfile is mechanism (bake passes the variant), not declaration.

D4 — Axis vocabulary home. History: group `axes:` blocks (original schema) → global
`axes:` map + top-level `images:` section → operator collapsed the vocabulary onto the
image entry ("surprised we couldn't just put the variants directly in the image") →
operator then folded the whole `images:` section into `base_images` ("the only time
someone would need to explicitly declare a locally built image is if it is a base image").
FINAL: `base_images` is the single base registry with two entry shapes:
- pulled: `KEY: "host/path:tag"` (lock-resolved, lock-paired, workload-runnable — keycloak proves the case)
- built: `KEY: {dockerfile: <repo-relative path>, axis: {NAME: [values]}}` (closure/tree-resolved,
  excluded from lock pairing, refused if present in the lock, FROM-fodder only)
A built base's published image name = its dockerfile directory stem; the map key is only
the ARG/FROM name — two declared facts, no convention joining them.

D5 — FROM grammar. **One spelling everywhere: `ARG KEY` + `FROM ${KEY}`** (the shape
api/Dockerfile already used). Kills bare-name FROMs, `BARE_IDENTIFIER_PATTERN`,
context-candidate stem matching, and the entire stem-resolution machinery. `FROM ${X}`
naming no base_images key refuses at load. Bonus recorded at the time: this also removed
the last name-convention join in the build graph.

D6 — Group/bundle fan. Group `axes:` blocks die. A fan group (member with group-level
`workloads:`) publishes one rendering per value of the single axis its image closure fans
(core-cuda/core-rocm derive); authored `options:` groups unchanged; closure fanning two
axes refuses; fan group whose closure fans nothing refuses (a non-fanning local set spells
one authored option). Member grammar is LEXICAL: `ref:` = entry, `options:` = authored
group, group-level `workloads:` = fan group. History: the first plan version said "the
closure decides (fanned closure ⇒ selectable group)" — that wording produced attempt 1's
provisional-member reclassification layer; post-mortem reversed it to the lexical rule.
Note the deeper symmetry: build set = closure over declared edges; fan = closure over the
same edges' declared fans. Same computation family.

D7 — The composer. One function serves annotations and markers:
`repository (config) + directory stem + selected variant (only when the image fans) + tag`.
Substrate-aware output: compose renderings emit the per-machine slot spelling through the
existing sentinel pass (consumer locks stay authoritative over committed bundles); k8s
renderings emit literals (tree tags locally, pins when published). Selection reaches the
composer via entry vars (compose options carry `{AXIS: variant}`) and `k8s.vars`.

D8 — Version. `requires` stays `>=0.7`. The operator explicitly overrode the plan's
version-bump line ("stay on 0.7, it's fine"). Consumer repos pin `requires: ">=0.7"`; no
consumer churn ships with this line.

D9 — The wrench (recorded here because the operator had it dropped from the plan): api and
lease-server ride core-cuda/core-rocm without varying — real duplication, deliberately NOT
fixed. Blocked by live service-port references crossing the GPU boundary both ways
(api→localizer via `LOCALIZER_CONTAINER_URL` at api.yaml:28; reconstructor→lease-server via
`LEASE_SERVER_URL` at reconstructor.yaml:12). Bundles are separate score generations on
separate per-entry networks (`{name}-net`); cross-bundle service discovery does not exist
(only the edge is wired across networks). The fix — assembly-time service-port resolution
with shared networking — is its own future line. Post-refactor the wrongness is gone (the
axis no longer claims api varies; its image gets identical refs in both renderings); what
remains is compose-text duplication. Splitting would also churn Make-it-Sing (refs both
core-cuda and core-rocm), so it was never "purely placeframe-side" anyway.

D10 — placeframe cosmetics. When placeframe's boatyard.yml is edited, routes and ports are
re-spelled in YAML flow style (`- {path: /*, upstream: api:8000}`); confirmed valid
(`/*` and `keycloak:8080` are legal plain scalars in flow mappings) and boatyard never
rewrites boatyard.yml. In the plan's transition section.

## The target state, concretely (placeframe)

boatyard.yml (delta: +built entry, −group axes, flow-style routes/ports; everything else
byte-identical; values/bundles/ref:s unchanged):

```yaml
base_images:
  UV_BASE_IMAGE: "ghcr.io/astral-sh/uv:0.12.15-python3.13-trixie-slim"
  # ... 11 more pulled entries unchanged ...
  NEURAL_NETWORKS_BASE:
    dockerfile: workloads/neural-networks-base/Dockerfile
    axis:
      TORCH_DEVICE: [cuda, rocm]

services:
  auth: {options: {keycloak: {...unchanged...}}}
  core:
    workloads: [api.yaml, lease-server.yaml, reconstructor.yaml, localizer.yaml]
    routes:
      - {path: /grafana/*, upstream: grafana:3000}
      - {path: /loki/*, upstream: loki:3100}
      - {path: /*, upstream: api:8000}
    ports:
      - {service: api, host_ip: 127.0.0.1, published: 5679, target: 5678}
      - {service: lease-server, host_ip: 127.0.0.1, published: 5683, target: 5678}
      - {service: seaweedfs-admin, published: 59001, target: 59001}
  tunnel: {...unchanged...}
k8s: {...unchanged, k8s.vars TORCH_DEVICE: cuda stays as the selection...}
```

Dockerfiles: reconstructor and localizer add `ARG NEURAL_NETWORKS_BASE` and re-spell
`FROM neural-networks-base` as `FROM ${NEURAL_NETWORKS_BASE}`. neural-networks-base
unchanged (its `ARG TORCH_DEVICE` stays as the checked mechanism). Workload files: image
lines deleted; sources declared via annotations (api/lease-server/reconstructor/localizer =
dockerfile paths; keycloak/ngrok = lock keys). Provisioners: `{{ env "X_SHA" }}` image
lines become `dockerfile:` markers; compose observability's `${X_IMAGE:?err}` slots become
`lock:` markers. Make-it-Sing and values files: no changes.

The annotation VALUE syntax is deliberately NOT specified here — that is open question #2.

## Attempt 1 post-mortem (+600/−217, reverted)

The executor implemented the plan faithfully; the plan's generality caused the growth.
Not-real cases supported:
- multi-axis dict shape on built entries plus a transplanted cross-product validator
  (the old `ServiceGroup._validate_axes` moved homes rather than dying — a wash counted
  as a deletion);
- `_inherited_axis` recursion: built-base-on-built-base chains, leaves FROMing multiple
  built bases, same-axis unanimity merging (zero instances anywhere);
- stem-collision guard (two declared Dockerfiles whose dirs share a name — theoretical);
- chart-values dockerfile-marker scanning (no chart references a built image; the plan
  said the grammar "allows" it and the executor implemented scanning for it);
- the provisional-member classification layer (my "closure decides" wording made them
  build two-phase member reclassification, group-level vars, auth-check relocation);
- the per-entry workload reload survived (the smear the refactor exists to kill).
Plan fix at `1385057`: every not-real case became a refusal, not a mechanism; lexical
member grammar; one-hop fan; load-once ruling; chart values lock-only.

## Attempt 2 post-mortem (+517/−207, reverted)

The executor was faithful to the pinned plan (lexical grammar, one-hop fan, load-once
cache, no chart scanning, no recursion, no reclassification). Growth traced to four plan
insufficiencies, not executor error:
1. **Annotation value syntax was never specified.** The plan said "a container-name →
   source map on metadata" — one parenthetical, zero syntax. Attempt 1 widened
   annotations to YAML mappings (ripples); attempt 2 invented a string DSL
   (`outernet.internal/dockerfile: "reconstructor=workloads/reconstructor/Dockerfile"`)
   with its own parser (~35 lines). Two agents, two inventions, same hole.
2. **The per-rendering image closure is a real new concept the plan never named.** The new
   world needs "which images belong to this entry / this k8s rendering" for variant
   selection in composition, fan-group derivation, and k8s pins demand. Because the plan
   never named it, the agent invented `_entry_stems`, called it from three sites, and
   cached results in two new ConfigFile fields (`entry_images`, `k8s_images`) plus
   `rendering_documents` scaffolding. The smear recreated itself inside the new design.
3. **"One lens, one pass" is unsatisfiable as written.** The compose lens
   (`ComposeWorkloadDocument` = base + probe-adapter check, compose-only for a real
   reason) is needed at compose generation, so the agent re-parses cached raws per entry
   (`loaded._replace(document=ComposeWorkloadDocument.parse(...))`) — a third validation
   pass hidden inside a ruling claiming to eliminate the second. Same class:
   `select_provisioner_files` now runs 3× per load (edge collection, surfaces, render);
   the load-once ruling covered workloads only, not provisioner selection.
4. **The arithmetic was never done.** Mandated additions ≈ +245 (composer+marker arm+
   injection ~75; dual-shape base_images plumbing ~40; BuiltBase ~35 — itself a wash, the
   axis-value grammar was re-homed from ServiceGroup, not deleted; per-unit closures ~60;
   annotation DSL ~35) against ~150 of cleanly deletable code (walk ~25, group axes
   validator+expansion ~45, variant_axes ~16, _refuse_unknown_axis_refs ~18, image-field
   validator ~8, per-entry load ~4, bare-name FROM machinery ~15). **Intrinsic floor ≈
   +100 net no matter how faithful the execution.** Observed +310 = floor + ~200
   avoidable duplication from items 1–3. The plan's acceptance test ("net negative or
   near-zero") was written without this ledger and is false as stated.
Also noted: attempt 2 quietly changed k8s pins demand from "scan workload texts for
`*_SHA`" to "the rendering's image closure's sha_envs" — behaviorally reasonable but
unruled; the next session should rule on it deliberately.

## The open fork (the next session's central question)

- **Option A — accept the ~+100 floor, consolidate to it.** Name the per-unit closure as
  ONE concept (one function, one computation at load, one cache); design the annotation
  syntax ONCE in the plan; rule the lens split honestly (base lens at load, compose check
  at compose render — or unify if a way exists); dedupe provisioner selection. The floor
  is the true price of correctness: the old world was smaller because human hands composed
  the refs; the new world must know, per rendering, what participates.
- **Option B — trade back a design decision to actually shrink code.** The candidate is
  container-addressing via metadata annotations. The original self-addressing slot
  (`image: ${RECONSTRUCTOR_IMAGE}`) needed no parser and no injection-side lookup; it was
  killed for the redundant-spelling sin (annotation + slot = two declarations needing an
  agreement check). That reasoning still stands, but its mechanical price (~container→
  source mapping machinery) was never ledgered. If "less code" is the hard property, this
  is the decision to revisit.
My stated position at session end (not ruled on by the operator): A — finish + one
consolidation pass — with the plan honestly reframing the acceptance test around the
floor. The operator chose: revert, drill the design fresh in a new session.

## Open questions (drill list)

1. The fork above: accept the floor or trade back annotation-addressing.
2. Design the annotation value syntax deliberately (or eliminate the annotation channel).
3. Name and place the per-unit image closure (entry→stems, k8s→stems): one computation,
   one home, computed at load.
4. The lens problem: how many validation passes exist, honestly; base-vs-compose lens
   split ruling.
5. Provisioner selection: how many times per load, where cached.
6. k8s pins demand: closure-derived vs text-scanned (attempt 2 changed this quietly).
7. Implementation sequencing: consider landing deletions first so the line's net shape is
   visible mid-flight (never ruled; floated here).
8. Deferred wrench: cross-bundle service-port resolution — next line's candidate, not this
   line's.

## Repo ground truths (verified this session)

placeframe `workloads/`: 14 directories, 11 with Dockerfiles (api, auth-initializer,
database-manager, database-migrator, lease-server, localizer, neural-networks-base,
postgres, reconstructor, s3-initializer, seaweedfs), 3 config-only (alloy: config.alloy;
grafana: provisioning/; loki: config.yaml — documented in workloads/AGENTS.md as
"digest-pinned stock mirror images, not built wrappers"). Consumption: 10 run-consumed
via `*_SHA` slots (six of them through provisioner manifests in stack/score/{compose,k8s}/);
neural-networks-base FROM-consumed by reconstructor and localizer only.

neural-networks-base/Dockerfile: `FROM ${UV_BASE_IMAGE}` (line 1), `ARG TORCH_DEVICE`
(line 28), `uv pip install -r pylock.neural-networks-${TORCH_DEVICE}.toml` (line 32) —
ALL device branching lives in the base. reconstructor/localizer Dockerfiles mention
TORCH_DEVICE zero times; they `FROM neural-networks-base` (line 1) and declare
`ARG RECONSTRUCTOR_SHA` / `ARG LOCALIZER_SHA` (cache-bust args).

bake redirect (build.py, pre-refactor): `additional_contexts = {context:
"target:{context}-{variant}}"` — how a fanned leaf's FROM resolves to the flavored base
target. The plan leaves "arg value vs additional_contexts re-keying" as an executor
decision to record.

Consumers: Make-it-Sing refs auth-keycloak, core-cuda AND core-rocm (both variants),
rathole-client (infra-rathole), livekit (lbe-toolkit); its values select
`bundles: [core-cuda, rathole-client]` with a path_override for core-cuda. placeframe's
own values: `bundles: [core-cuda, auth-keycloak, tunnel-ngrok]`. k8s side:
`k8s.workloads` flat list (api, lease-server, keycloak, reconstructor, localizer),
`k8s.vars: {TORCH_DEVICE: cuda, ...}`, charts loki + grafana with values files under
stack/score/k8s/values/ (already using lock-image markers).

Machinery that survives the refactor unchanged: tree_sentinels + TREE_SENTINEL_PATTERN
rewrite, env_lock_slot namespacing, the lock and its pairing laws (minus built entries),
owned_images (ALPINE/CADDY), provisioner selection by resource types, GPU template
selection (compose: GPU_COMPOSE_TEMPLATES keyed on entry.vars values; k8s: GPU_POD_TEMPLATE),
render_score's per-run copy expansion (where injection rides), the barrier.

## Session errors and corrections (so the next session doesn't re-make them)

- I claimed placeframe had three "zombie Dockerfiles" (loki/grafana/alloy). Wrong: I
  counted directories, not Dockerfiles — those three are config-only directories; the
  walk already ignored them. Caught by the operator's confusion; corrected; the corrected
  accounting (zero unconsumed Dockerfiles) became evidence FOR strict closure.
- My "the closure decides" discrimination wording produced attempt 1's classification
  layer. Reversed to the lexical rule.
- My acceptance test ("net negative or near-zero") was written without an addition ledger
  and is arithmetically false (~+100 intrinsic floor). Must be reframed when the plan is
  next touched.
- Two of attempt 1's not-real cases (closure-decides, fan-inheritance generality) trace to
  my plan wording, not executor invention. Attempt 2's growth traces to four plan holes
  (annotation syntax, unnamed closure concept, unsatisfiable one-lens ruling, no
  arithmetic), not executor error.

## Operator rulings log and working style

Rulings, verbatim intent: stay on 0.7; no reverse Dockerfile check (unreferenced = silently
not built); drop the wrench from the plan ("just cruft"); flow-style routes/ports; both
attempts reverted whole; drill the design in a fresh session before re-implementing.

Working style for the next session: the operator wants explicit declarations over
conventions, hates ceremony in discussion (no design-note formality mid-derivation; the
plan file was a deliberate exception), asks for ELI5 when lost and expects the register to
drop immediately, decides by single-letter ruling after an options+tradeoffs presentation
with a stated recommendation, treats code growth as a smell of wrong design, and does not
want the question tool used. Present options with tradeoffs and a recommendation; wait for
the ruling; do not move to the next question until the current one is resolved.
