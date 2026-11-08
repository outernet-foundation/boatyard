# refactor.md — the 0.7 line: slot/pins/values refactor

Design record for the boatyard refactor consolidated on 2026-11-07 across three sessions
(this file's session + the placeframe auth/JWKS session; see
`compaction-placeframe-auth-values-2026-11-07.md`). This is a plan file, not present-state
docs.

Branch shape law: the branch over `origin/main` carries exactly one commit changing
AGENTS.md and exactly one commit changing closeout.md and refactor.md together, in that
order, at the tip. A doc change never rides a code commit and never adds a further doc
commit — AGENTS.md lines a move invalidates fold into the AGENTS.md commit, and
closeout.md records and this file's updates fold into the tip commit, always by
rewriting those two commits in place. Code commits beneath stay doc-free, so the line
reviews as pure code changes capped by the doc record.

Session state: Moves 1–2 LANDED (2026-11-07 — Move 1: tree slots in committed compose artifacts,
the sentinel barrier, tree fans in `up`'s per-run env file, the images-only build;
tests pin slot spellings and compose byte-stability; both markers BOATYARD-ified
(`__BOATYARD_LOCK__`/`__BOATYARD_TREE__`) and the sentinel derived from `TREE_SLOT_MARKER`
(`marker + name + "__"` — no independent spelling, so Move 9's rename propagates).
Move 2: `boatyard.k8s-pins.yml` at root (`config/k8s_pins.py`, ALL_CAPS `*_SHA` keys,
`tree-<oid>` values, sorted render + header inline in `bump`); no shas parameter exists anywhere on
the generation path — `generate_k8s` owns the substrate policy (`--local` computes fresh
tree tags; otherwise `load_pins(path, demanded)` — riding the strict `load_yaml`
loader — carries both refusals, absent pins and unpinned demanded slot, naming `bump`,
with the `*_SHA` demand scan riding the same dump text `render_score` substitutes over),
`generate_compose` passes `ConfigFile.tree_sentinels` (minted over `config.service_builds`'
slot supply alone; values never enter a compose artifact, generation is git-free), and the `bump` verb computes HEAD's tree
tags solely as certification evidence against the CI shelf shards (D2 inlined, the
release-verb enumerate-and-merge shape), writes the pins file, then regenerates the k8s
output from the file it just wrote — the fixed point exercised by its own writer. The
clean-tree check tolerates only `boatyard.k8s-pins.yml` itself (the bump's own output must
not block its retry; idempotence per head holds pre- and post-commit). The
branch over `origin/main` is eight commits under the shape law above: the pre-session
`score_generate` extraction (first, so the pure refactor reviews separately from the
session's code; its build-epilogue half was never landed — Move 1 obviated it, and
earlier records describing it describe a state absent from git), the Move 1 code, the Move 2
code, the compaction handoff, the drive-by boundary refactor (render bindings merged at the
`render_score` call sites with the lock images as a callee-owned underlay; the sentinel
spelling family centralized in `config/config.py` with `ConfigFile.tree_sentinels` minting;
`load_pins`/`parse_pins` replacing the `K8sPinsFile` class over a strict `load_yaml`
loader; the compose post-pass as two named `re.sub` passes; empty `vars` values refusing
at parse; single-call helpers inlined), the compose-doctrine commit (`services` required
on every config at load — compose is unconditional, `k8s` the optional substrate; the
ALPINE refusal rehomed to `generate_compose` naming `base_images`), the AGENTS.md record,
and the tip doc commit carrying this file with the 0.6.6 closeout record. A mid-session
extraction of the fill
check from `up` onto `ValuesFile`, built as Move 4 groundwork, was dropped in the same
rewrite rather than landed and superseded: Move 1 carries the marker generalization of
`up`'s inline check, and Move 4 deletes that check wholesale. The values redesign was
ruled in the session's later block — Move 4 rewritten below (values sections +
load-time validation), laws 7–8 added, Move 5 amended. The boatyard-owned image pins
were ruled after the doctrine commit — Move 8 added below, the rename renumbered to
Move 9. The next session opened with a pre-Move drive-by (one code commit
beneath the AGENTS.md commit, both doc commits rewritten in place): the collapsed-name
collision check moved from the compose post-pass to `ConfigFile.resolve`
(`refuse_colliding_service_names` — workload containers ∪ type-joined provisioner
`services:` ∪ the unconditionally reserved `wait-for-resources`, plus ambiguous
`{stem}-{container}` spellings; absent workload files refuse at load, the chart-values
precedent); `load_workloads`/`select_provisioner_files` relocated into `config/config.py`
with the render path sharing the one join; the post-pass duplicate guard deleted — a guard
whose only firing condition past the load check is boatyard's own model being wrong is a
delayed test, and delayed tests belong in CI goldens, not consumer runtimes. The same
ruling was then extended to the compose secrets tail check: Moves 3–4, Move 8, and What
dies amended so the consumer-surface scan (authored workload ∪ provisioner ∪
consumer-interpolated bind text, minus the lock-key vocabulary) lands at config load
feeding both the values surfaces and the secrets subset check, the compose
`validate_consumed` tail and its `consumed` accumulation die, and the k8s side stays at
its rendering (chart-rendered `secretKeyRef`s are helm-template evidence — law 8's
earliest moment).

Execution law: one move per session. A session lands exactly one move as one doc-free code
commit, folds the AGENTS.md lines it invalidates plus this file's session-state record into
the branch's two doc commits by rewriting them in place, and yields its handoff; the next
session picks up the next move. (The law was set mid-session-3, after Moves 3–4 had already
landed together in it.)

Session 3 landed Moves 3–4 (2026-11-07). Move 3: the secrets declaration folded into
`boatyard.yml` as the `secrets:` section (`SecretsDeclaration`/`SecretKey` rehomed to
`config/config.py`; absent section refuses where the repo generates; `secrets: {}` states
zero; `SECRETS_FILE` died; the k8s tail check stays at its rendering). Move 4: values
sections + load-time validation — the values top level is a closed typed set (`bundles`,
`path_overrides`, `public_url`, `platform`, `values`; `_fold_selection` died, the open
ALL_CAPS wall died, `extra="forbid"` at every level); lock bundle records became
`{reference, slots: {NAME: {required}}}` (relock writes `slots` via `artifact_slot_demands`);
the consumer-surface scan lives at config load (`authored_slot_demands` into
`ConfigFile.consumer_surfaces`) feeding both the values surfaces and the secrets vocabulary
check; one section validator over the demand table — fill for participating units,
orphan/placement/shared-cardinality selection-free; the compose `validate_consumed` tail and
its `consumed` accumulation died; `up`'s requirements loop and inline fill check died; the
override derivation moved into the values module's `assembly()` member with the
producer-root hunt collapsed to `parents[3]`; new `tests/test_values.py` (17 cases).

Rulings closed in session 3, recorded so they are not re-litigated: (a) the `gateway:`/
`ports:` typed values overrides land with Move 7 — Move 4's closed-set enumeration describes
the end state, and config port slots are homeless in the interim (tests ride int literals);
(b) scan parity per source — workload text contributes suffixed odd-`$` slots only (bare
slots are render-expanded or refused at `render_score`), provisioner
`services:`/`state:`/`outputs:` text contributes odd-`$`, consumer-interpolated `files:`
content contributes even-`$` (authored `$$` lands single-`$` in the artifact; the rathole
golden pins it); (c) fill fires for participating units (bare entries + selected options +
their overrides), orphan/placement/shared/undeclared fire selection-free, and optional
(`:-`) demands must be declared or covered but need not be filled. Known debt: AGENTS.md
sits over its own 26KB hard cap (27.2K at the Move 1–2 fold, ~28.0K after the Move 3–4
fold, 28.3K after the Move 5 fold) — a dedicated pruning pass owes the file before the line
closes (the operator's `wip.md` draft rides the branch tip).

Session 4 landed Move 5 (2026-11-07): values interpolation — section values carry bare
`${NAME}` references over the workload `PLACEHOLDER` grammar; `PublicUrl` retains the raw
authored string and the `minted`/`resolved` members answer the slot pair (`PUBLIC_URL` = the
raw URL; `EDGE_BASE_URL` joins `minted` at Move 6). Grammar/marker/unresolvable/cycle
refusals all fire at load in `_refuse_unresolvable_references` (the literal-only refusal in
`_refuse_section_faults` died; the unresolvable names the roots; the cycle names its chain —
`peel_references`/`reference_cycle`/`resolve_value` module helpers under the class). Root
membership is selection-free: any flattened key is a reference root, inert sections
included. Resolution happens once at the env-file render — `assembly()` answers `resolved`
(minted ⊕ substituted flattened values, peel-ordered successors first) and its values ride
`up`'s dotenv, so the minted `PUBLIC_URL` lands in the per-run env file and `plan.values`
carries it into `compose_command`'s `env -u` unsets; `generate-score` still resolves
nothing (it reads `platform`). New tests pin grammar refusals (default/lowercase/nested
spellings), the marker refusal, roots listing, cycles incl. self-reference, chained
resolution, and the assembly render (minted + resolved side by side). A concurrent
operator commit (`wip.md`, the AGENTS.md pruning draft) landed mid-session and rides above
the tip doc commit untouched.

Next session: Move 6 (machine-minted `EDGE_BASE_URL`), then Moves 7–9 one per session in
order (Move 9 is the line's last item and its name is still undecided — decide before it
executes); after Move 9, update `closeout.md` with consumer-repo changes from "Consumer
transition". The push is the operator's.

---

## Decisions — both resolved

### D1 — who cuts the k8s pin bump commit — RESOLVED: operator, always

Post-merge, an operator (or agent) runs the bump on a clean checkout of `dev` HEAD and
pushes. No CI law changes; no release-verb carve-out to the CI-commit-free invariant.
Deploy latency = operator attention, accepted.

(Ruled out regardless: merge-gate absorbing the bump — the unity library cache promotion
depends on the merge commit's tree equaling the PR head's tree; the gate must never modify
the tree.)

### D2 — what the bump verifies before writing pins — RESOLVED: CI certification

A pin is only writable for content CI built and pushed at exactly that head. The check:
`git rev-parse HEAD` → enumerate the head's shelf shards (`list_build_tags` from
build-artifact-registry over `{REGISTRY}/builds` / `images-digests-all`, matching
`sha-{HEAD}` and `sha-{HEAD}-*`) → pull and merge each shard's
`{target: {ref, digest, tags}}` (a repeated target across shards is a loud failure, the
release verb's precedent) → every computed tree tag must appear in the union of recorded
`tags[]`. Refusals: no shards at HEAD ("nothing to pin — rebuild through CI"; this is the
ad-hoc `--push-to` case refusing, correctly), and missing tags named with the same remedy.
Auth rides bar's neutral chain (`REGISTRY`/`REGISTRY_USERNAME`/`REGISTRY_TOKEN` env or
ambient docker config) — the identical burden `build --push` and `publish-compose` already
impose. Roughly 40–60 lines in the bump verb; boatyard already depends on bar (`>=0.2.2`).
The shard-enumerate-and-merge shape duplicates logic inside the toolkit's release verb —
inline in boatyard first; lift into bar only if a third consumer appears. Ad-hoc
`--push-to` builds become unpinnable: the feature, not the loss.

---

## Rulings closed during synthesis (recorded so they are not re-litigated)

Each of these looked like a decision and resolved to one answer under the session's laws:

- **No pin bootstrap.** `generate-score` never writes the pins file. First k8s generation
  without a pins file refuses naming the bump verb, always. The lock-bootstrap precedent
  does not transfer: the lock bootstraps *resolutions* (recomputable, checkable); a
  bootstrap pin is an *unverified decision* — records cannot bootstrap. First-time flow:
  build/push (CI), then bump, then generate passes.
- **The bump verb is the sole writer of the pins file.** Rollback is `git revert` of the
  bump commit — never a hand-edit. One writer, audit trail = git log. A hand-edited pin is
  an unverified decision and unwritable by construction.
- **The bump writes the whole certified head's tag set.** Not per-service partial bumps.
  With content-addressed tree tags this is not a restriction: unchanged services hash
  identically and are no-ops by identity. The semver line converges to the same shape via
  tag-everything (unchanged units alias at existing versions).
- **`NGROK_DOMAIN` stays hand-set; no `host-of` operator in interpolation.** Host-of-URL
  is legitimate divergence (rathole's control endpoint differs); disagreement is not a bug,
  so it is a decision, not a derivation. No filters/functions in the interpolation grammar,
  ever — bare references only.
- **`GATEWAY_PORT` is not derived from `public_url`.** They coincide only under direct
  exposure; under tunnel exposure the URL is tunnel-terminated while the door is a local
  port. Independent decisions that sometimes correlate.
- **`PUBLIC_URL` is unconditionally reserved**, like `EDGE_BASE_URL`: refuses by hand
  always; minted when its declaration exists (`public_url` stated / gateway declared).
  Reserved names refuse always; minting follows declaration — no conditionals.
- **`--local` k8s generation reads no pins.** The local (gitignored) output deploys local
  bits: fresh working-tree tags, dirt is content.
- **Unused `environment:` keys stay legal** (values legitimately carry keys for unselected
  bundles). Old port spellings post-migration go silently inert; accepted, no detector —
  the samples and the one-time reshuffle carry it.
- **Interpolation roots are hand variables + minted slots only.** Marker-bearing names
  (`__BOATYARD_LOCK__`/`__BOATYARD_TREE__` spellings) refuse both as keys and as references; typed fields
  participate only via their minted slots (no `${public_url}` lowercase references).
- **Routes get no override surface, ever.** Routing is versioned deployment topology; a
  per-machine upstream want is a dev-overlay concern, not a values concern.
- **Compose is unconditional; `k8s` is the optional substrate.** Every boatyard repo ships
  a compose graph: `services` is required (non-empty) at config load, and the k8s-only repo
  shape is dead. The ALPINE barrier requirement keys at compose generation, refusing until
  `base_images` declares `ALPINE_IMAGE` — not at config load, because base_images entries
  carry verb-wide side effects (relock resolution) and the barrier is a render-only need.
  Move 8 dissolves this placement question wholesale by changing the declaration's owner.
- **The barrier and edge images are boatyard-owned, not base images.** They are inputs to
  no Dockerfile; they are the images of services boatyard itself emits. Declaring them in
  `base_images` was the category error behind both placement checks (gateway/CADDY at
  load, ALPINE at generate) and behind the build-arg pollution. Boatyard-owned pins ship
  as defaulted config fields in the tool-pin shape and move with boatyard releases
  (Move 8).

---

## Cross-cutting laws (the invariants the refactor establishes)

1. **Three-class principle, restored and amended**: authored facts in the config,
   registry-resolved in the lock, tree-derived computed on demand — with one declared
   carve-out: the k8s pins file is a *deploy record* (tree-derived by origin, decision by
   nature, written only by the bump verb, never recomputed or compared against the tree).
   A cache can be stale; a record can only be superseded.
2. **Slot boundary**: env slots exist only for values committed *artifacts* consume.
   Anything consumed only by boatyard's own assembly (edge stanza, port stanzas) is a
   typed config field with typed values overrides — never a compose interpolation spelling.
3. **`boatyard.yml` contains zero `${...}` spellings.** Interpolation spellings live
   exactly where interpolation happens: score sources and committed artifacts.
4. **Decide-or-copy test** (values file): every literal in the values file is an operator
   decision (hand), a machine-minted slot, or an interpolation of the two. Everything else
   is eliminated by: typed-field ownership (`public_url`→`PUBLIC_URL`), machine minting
   (`EDGE_BASE_URL`), interpolation (repo-specific projections), or config defaults with
   deviation overrides (ports).
5. **Values override keys mirror the config fields they override** (`gateway:`,
   `ports.<service>`). Unknown names refuse listing the declared vocabulary; names valid
   but belonging to an unselected option ride inert (the `bundles`/`path_overrides`
   pattern).
6. **One file, one nature**: no file mixes recomputable bytes with decision bytes (why the
   pins are not a lock section; why pins do not live inside `manifests.yaml`).
7. **Four files at root**: `boatyard.yml`, `boatyard.lock.yml`, `boatyard.values.yaml`,
   `boatyard.k8s-pins.yml` always live at the repo root of any boatyard repo. A directory
   walk accounting for any other location is a raise. The producer-root hunt collapses to
   `parents[3]` (the override layout check already pins the shape) + a raise.
8. **Validate at the earliest moment the evidence exists; derive at the moment the
   product is needed.** Section checks fire at values load (paths, workload files, and
   lock records exist then); cross-repo fan derivation runs only when `up` needs the
   env file — `generate-score` loads values but never pays for derivation.

---

## Moves

### Move 1 — compose tree-tag slots [LANDED 2026-11-07]

- Committed compose artifacts carry odd-parity `${...}` slots for service content
  identity instead of baked `tree-<oid>` literals; `SERVICE_VERSION` rides the slot.
- Score tools refuse bare `${VAR}` placeholders, so use the barrier idiom: `render_score`
  expands `${X_SHA}` workload slots to a sentinel; a post-pass in `score_compose.py`
  (beside the `COMPOSE_SLOT` namespacing pass) rewrites sentinels to
  `{ENTRY}__BOATYARD_TREE__{SERVICE}_SHA`-style marker-protected slots (single `$`, env-file
  filled). New `TREE_SLOT_MARKER` + `env_tree_slot` beside `env_lock_slot`/
  `LOCK_SLOT_MARKER` in `config/config.py`.
- `up` computes `compute_service_shas` on demand — local entries plus path-override
  producer repos (against the producer tree, dirt is content) — and writes tree fans into
  the per-run env file beside the lock fans. Unfilled-slot validation treats tree-marker
  slots as satisfied by construction.
- `build.py`: delete `_regenerate_stale_artifacts`, `stale_service_targets`,
  `TREE_TAG_PATTERN`, and the epilogue call. Build is images-only; artifact freshness is
  wholly `generate-score --check`'s. This keeps the score toolchain off build-only
  machines.
- Publish path unchanged (already substitutes independently at publish time).
- The sentinel is the tree slot's pre-image, derived from `TREE_SLOT_MARKER`
  (`marker + name + "__"`); minting (`ConfigFile.tree_sentinels` over the build-set slot
  supply) and the find-pattern `TREE_SENTINEL_PATTERN` share the marker family in
  `config/config.py`, and `render_score` stays the neutral expansion primitive — it lays
  the lock images underneath as bindings, and each call site merges only its own
  `variable_values` (compose: sentinels ⊕ entry vars; k8s: tree tags/pins ⊕ k8s vars);
  no second spelling to drift. Both marker
  values carry the BOATYARD namespace (`__BOATYARD_LOCK__`, `__BOATYARD_TREE__`);
  Move 9 renames the tree spelling again (name undecided).
- Effect: compose artifacts become a pure function of stable inputs; `--check` is
  source-change-invariant for compose; the clean-clone regeneration ritual dies (dirt
  never enters committed bytes).

### Move 2 — k8s pins file + bump verb [LANDED 2026-11-07]

- New committed file `boatyard.k8s-pins.yml` at repo root: typed pydantic model, keyed by
  sha-slot name (`API_SHA: tree-...`), never hand-authored. Name rationale: "pin" = a
  deliberately-held reference chosen by an act — survives the grammar changing to semver
  tags later ("digests" is wrong on facts — tags, not digests — and wrong on concept:
  digest implies derivability, the cache smell this refactor kills).
- k8s generation (`score_k8s.py`) takes pins from the file; never computes from the
  working tree. Refusals: pins file absent → name the bump verb (including first-ever
  generation: no bootstrap); a workload's service lacking a pin (new service in the k8s
  list) → name the bump verb; removed service → pin drops silently (orphans prune by
  construction). `--local` bypasses pins (fresh tags).
- New bump verb (sole writer): requires a clean tree; computes tree tags of HEAD; verifies
  per D2; regenerates the k8s output with explicit pins (never hand-edits the manifest);
  writes the pins file. Idempotent per head. Self-skips where no `k8s:` section.
- `--check` semantics: regenerate (meat + pins) and diff — a fixed point across two files,
  each with one role: pins file = input (record), manifest = output (derivation). Nothing
  reads the manifest back (no feedback-by-scraping; no regex state recovery).
- Semver future (`semver-image-bundle-lines.md`): the pins file is what version tags later
  ride; the bump machinery is the semver bump machinery. Tree grammar now is correct
  sequencing (semver design is pre-implementation).

### Move 3 — secrets declaration folds into `boatyard.yml`

- `stack/secrets.yaml` dies; `boatyard.yml` gains a `secrets:` section (KEY: description |
  KEY: {description, mint}; ALL_CAPS; top-level, substrate-neutral).
- `secrets: {}` = the stated empty declaration; an absent section refuses where the repo
  generates (same teeth as today's missing-file refusal — zero secrets is a stated act).
- Subset validation stays per-rendering on the k8s side (chart-rendered `secretKeyRef`s
  are evidence only `helm template` mints — law 8's earliest moment is the rendering); the
  compose side's tail check is transitional, superseded at Move 4 by the load-time
  consumer-surface scan. `stack/generated/secrets.txt` unchanged; `SECRETS_FILE` path
  constant dies.

### Move 4 — values sections + load-time validation

- The open ALL_CAPS wall dies. `public_url` stays a top-level special (owns minted
  `PUBLIC_URL`; hand-set always refuses); every other hand value lives in a section:
  `values.images.<service>` (local services; surface = this repo's workload files,
  `:-`-defaulted slots included), `values.bundles.<name>` (remote includes; surface =
  the lock's per-bundle slot records), `values.shared` (multi-demander values only — a
  shared key demanded by exactly one thing refuses naming its home section).
- Lock bundle records become `{reference, slots: {NAME: {required: bool}}}` — a map,
  not a list (the duplicate-key walk is the teeth); `slots` over `values` (that word
  names the values file). `requires` and the surface are projections of the one record:
  the required-true filter / the key set. Machine-written by relock; fresh-repo rule,
  no migration.
- One section validator, three authorities, one record shape — workload scans, lock
  records, and override-text scans all produce `{NAME: {required}}`. Checks: fill (a
  section's required slots covered by its keys ∪ shared ∪ minted slots), orphan (a
  key no surface mentions refuses naming the section), placement (a key demanded
  elsewhere refuses naming its home). Collision at flatten: one name, one declaration —
  the same name in two sections, or in `shared` and a section, refuses. Sections are
  placement, not namespaces: the flattened ALL_CAPS set is the reference layer.
- The consumer-surface scan moves to config load and is the workload-scan authority:
  odd-`$` authored slots over the entry's workload text ∪ type-joined provisioner
  `services:`/`state:`/`outputs:` text ∪ `consumer-interpolated` bind sources, minus the
  lock-key vocabulary (`base_images` keys; the boatyard-owned pin keys join the
  subtraction at Move 8). Bare `${NAME}` never reaches the surface — expanded or refused
  at `render_score` — and the tool mints no consumer-surface slots (barrier → lock key,
  sentinels → tree slots), a contract the goldens certify. The secrets subset check rides
  the scan here — vocabulary: `config.secrets` ∪ the section surfaces' own demands (a
  values key covering a slot is the other spelling of declaring it) — so every verb
  refuses an undeclared consumed key before any tool runs. The compose render-tail
  `validate_consumed` call and the `consumed` accumulation in `_namespace_slot` die:
  past the load check their only firing condition is boatyard's model being wrong, the
  delayed-test ruling this line already took for the post-pass collision guard. The k8s
  tail check stays (Move 3's chart-evidence law).
- Marker-bearing names never enter section surfaces (the hand can't set machine slots;
  the leaf marker refusal stays) — an override's machine-slot spellings are verified
  against the fans the derivation computes, not against sections.
- All checks ride `ValuesFile.load(path, config, lock)`. `up`'s requirements-gathering
  loop and its inline up-time fill check die here, superseded.
- Override logic moves out of `up` into the values module: declaration validation and
  the override file's surface scan at load (the file exists then — its path rides the
  file being loaded); the heavy derivation (producer config/lock projection under the
  consumer platform, producer tree fans) in an on-demand member only `up` calls. `up`
  receives selection, resolved includes, fan entries, and the flattened validated
  values — and renders.
- The top level stays a closed typed set (`bundles`, `path_overrides`, `public_url`,
  `platform`, `gateway`, `ports`, `values`); `_fold_selection` dies; `extra="forbid"`
  at every level (a typo'd key fails as unknown, not as "not ALL_CAPS").

### Move 5 — values interpolation [LANDED 2026-11-07]

- Section values may reference other slots: bare `${NAME}`, ALL_CAPS namespace,
  roots = minted slots (`PUBLIC_URL` live with Move 5; `EDGE_BASE_URL` joins at Move 6) ∪ the
  flattened hand namespace
  (sections are placement; references ride flattened names — law of Move 4's flatten
  layer). No operators, defaults, filters, nesting; marker-bearing references refuse;
  cycles refuse; unresolvable refuses listing the roots. Validate references at load;
  resolve once at the per-run env-file render (compose does not re-interpolate
  env-file values); generate-score never resolves (it reads only `platform`).
- Repo-specific projections (placeframe's AUTH block: issuer/endpoints from
  `${PUBLIC_URL}`, JWKS from `${EDGE_BASE_URL}`) live in each consumer's values
  sections as never-edited interpolation lines taught by the sample.

### Move 6 — machine-minted `EDGE_BASE_URL`

- Whenever a gateway is declared, boatyard derives `EDGE_BASE_URL=http://edge:8443`
  (reserved service name + emitted Caddyfile listen constant — boatyard-minted facts,
  typed by zero humans) into the per-run env file; interpolation root; unconditionally
  reserved against hand-set. The `keycloak:8080` incident class (hand-copied
  boatyard-minted facts rotting in gitignored files) becomes unwritable.

### Move 7 — ports: config defaults, values deviations

- `gateway:` and `ports[].published` in `boatyard.yml` become plain integer literals
  (repo-owned defaults, versioned, reviewed). A load-time refusal rejects any `${`
  spelling anywhere in the config (law 3's teeth).
- Values file gains typed overrides: `gateway:` (scalar) and `ports:` (service →
  published), law 5's vocabulary validation. Boatyard resolves default ⊕ override and
  writes integers into the materialized stanzas; ports leave the env file entirely.
- `FORWARDED_PROTO`/`FORWARDED_PORT` edge-stanza derivation from `public_url` under an
  auth selection: unchanged (assembly-baked, not slots).

### Move 8 — boatyard-owned image pins

- The category error dies: `base_images` returns to meaning exactly "Dockerfile build
  inputs" (`FROM` bases and build args). The barrier (`wait-for-resources`) and the edge
  are boatyard-**emitted** services — no Dockerfile consumes them — so their image pins
  are boatyard-owned facts, not base images.
- New config fields `barrier_image:` / `edge_image:` in the tool-pin shape
  (`score_compose_version` et al.): plain `path:tag` literals, defaults shipped by the
  boatyard release, repo-overridable, no values surface (images are not machine-specific).
  The version coupling is the point — the rendered Caddyfile syntax moves with the Caddy
  major, the barrier's behavior with its image; both bump with boatyard releases under the
  `major_minor` discipline, and the lock's resolved digests surface the effective change
  at relock (the tool-pin precedent for silent default motion).
- Relock resolves both into the lock as today (manifests needed at `up` and in committed
  artifacts); mirror treatment unchanged; slot spellings (`${ALPINE_IMAGE:?err}`, the
  CADDY manifest ref) stay — renaming them rides Move 9; the consumer-surface scan's
  lock-key subtraction (Move 4) gains both keys as they leave `base_images`.
- The placement checks die with nothing to forget: the gateway/CADDY load refusal dies
  (the edge image exists by default), and the ALPINE refusal at `generate_compose`
  (added in the doctrine commit) dies with it. `ALPINE_IMAGE`/`CADDY_IMAGE` become
  reserved names in `base_images` — the hand can't declare what boatyard owns.
- Co-designed with Move 7 (which makes `gateway:` a plain int literal; `edge_image:`
  sits beside it). Consumer churn: drop both entries from `base_images`; rides the 0.7
  churn event, `requires` floor bump with the release.

### Move 9 — tree vocabulary rename [LAST; NAME UNDECIDED]

- The line's last item. The whole tree family renames in one stroke so nothing forks
  mid-refactor: the marker (`__BOATYARD_TREE__` → `__BOATYARD_{NAME}__`, the second
  rename of the tree spelling — accepted), the `tree-<oid>` tag spelling, the
  `tree-tags` verb, `TREE_SLOT_MARKER`/`env_tree_slot`, `compute_service_shas`, and the
  docs' "tree-derived"/"tree tag"/"tree fan" vocabulary. The sentinel follows the marker
  for free (derived spelling). Moves 2–8 keep the current spellings.
- The replacement name is still open (`SOURCE` was considered and rejected in session);
  decide before this move executes. The bar: a cold reader stumbling on
  `{ENTRY}__BOATYARD_{NAME}__{SERVICE}_SHA` knows what it names without git-internals
  knowledge.
- Rides the 0.7 consumer-churn event with everything else; the bump verb as the pins
  file's sole writer makes the consumer re-pin one bump run. Committed-artifact test
  spellings update in the rename commit and AGENTS.md lines fold into the branch's
  AGENTS.md commit; historical closeout.md lines stay as written.

---

## Release and consumer transition

- **0.7 line.** `closeout-0.6.6` lands as-is first; the refactor opens 0.7. Moves 1–3
  (artifact semantics), 4–7 (values surface), and 8 (boatyard-owned image pins) are
  internally independent — separate PRs inside the line, one consumer-churn event at the
  end. `requires` floor bump rides the release.
- **Per-consumer transition order matters**: run the bump first (creates the pins file at
  the current head; k8s manifest stays byte-stable if its baked tags were fresh — and
  heals them if stale), then `generate-score` (compose artifacts slot-ify; secrets
  section migrates). Then: per-machine values reshuffle (values sections — images/
  bundles/shared, drop `PUBLIC_URL`/port spellings, AUTH block → interpolation lines),
  drop `ALPINE_IMAGE`/`CADDY_IMAGE` from `base_images` (boatyard defaults take over),
  sample rewrite, `stack/secrets.yaml` deletion, committed-artifact test updates
  (compose assertions gain slot spellings; k8s unchanged).
- **Toolkit**: getter's `generate-score --check` step command unchanged; its documented
  rationale line rewrites (structural freshness, not tag freshness). Bump-step
  integration depends on D1. Toolkit AGENTS.md line ~60 update.
- **Placeframe-side (not boatyard work)**: floor fixes now (sample + live `AUTH_JWKS_URL`
  → `${EDGE_BASE_URL}` edge spelling post-refactor, literal edge spelling pre-refactor);
  API JWKS probe as `/health`-reflects-JWKS (preferred over boot refusal — backoff beats
  restart storms, and the assembly's wait-for-resources may need to gate api on keycloak
  under the auth option); `loki_query.py` hardcoded container name; the Loki label-gap
  investigation (possible OTLP plumbing dropped in the bundles cutover — second silent
  casualty of the same topology change; investigate early).
- **Design docs**: `precision-attribution.md` and `semver-image-bundle-lines.md`
  "Release and regeneration" sections assume committed tree tags — amend to the
  slots/pins/bump model. Attribution work itself remains build-side and still worth
  doing; it does not fix the staleness problem (only removes over-inclusion noise).
- **Boatyard tests**: compose goldens gain slot spellings; new cases — source change
  leaves compose artifacts byte-stable; k8s regeneration preserves pins (fixed point);
  absent/new-service pins refuse naming bump; bump idempotence and verification refusal;
  values sections (orphan/placement/shared refusals, collision-at-flatten, lock
  `slots`-record shape, interpolation grammar incl. cycles); the load-time
  consumer-surface scan (undeclared consumed key refuses at load naming the file;
  lock-key/tree-slot exclusions; provisioner-text and consumer-interpolated-bind slots);
  ports override vocabulary
  refusals; `up` fills tree + minted slots into the env file.

## What dies

`stale_service_targets`, `TREE_TAG_PATTERN`, `_regenerate_stale_artifacts`, the build
epilogue regeneration, `_fold_selection`, `SECRETS_FILE`, `stack/secrets.yaml`, the
compose render-tail `validate_consumed` call and its `consumed` accumulation (→ Move 4's
load-time consumer-surface scan), `${...}` spellings in `boatyard.yml`, `PUBLIC_URL`/port
spellings in values files, the open ALL_CAPS values wall, `up`'s requirements-gathering
loop and its inline fill check, the producer-root walk (→ `parents[3]` + raise), lock
bundle `requires` lists
(→ the `slots` record), the clean-clone regeneration ritual (placeframe
`stack/AGENTS.md`), per-source-change "Run generate-score" commits for compose-only
changes, the gateway/CADDY load refusal, the ALPINE refusal at `generate_compose`, the
`ALPINE_IMAGE`/`CADDY_IMAGE` base_images entries (→ reserved names), and their build-arg
injection into every bake.
