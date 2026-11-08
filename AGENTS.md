# boatyard

Docker-stack lifecycle verbs for any repo shipping a compose graph; `up` derives the whole assembly from the root `boatyard.yml`. No Python API — the contracts are the verb CLI, the declaration schema (`boatyard.yml` + `boatyard.lock.yml`), and committed
  artifact formats. Three-class principle: authored facts in the config, registry-resolved in the lock, tree-derived (`tree-<hash>`) computed on demand, never committed — `boatyard.k8s-pins.yml` (a bump-written deploy record) alone is committed.
  github-actions-toolkit owns the consumer workflow contract and is never a project dependency; from
  1.0 on breaking changes ship with a manually bumped `major_minor`. Fresh-repo assumption: no code handles an obsolete format — a past artifact gets the plain parse refusal, never a migration path.

## Commands

Quick loop: `uv run pytest`, `uv run ruff check`; full gate: `uv run --locked scripts/preflight.py`.

## Verbs

One module per command under `verbs/`, each with its local bare `app = typer.Typer()` + `@app.command()`; `cli.py` composes them with nameless `app.add_typer(...)` (a named `add_typer` nests a group). Exception law: nothing is caught only to be re-raised as a
  different exception; guards and third-party exceptions surface raw with full traceback; `typer.BadParameter` owns the CLI-flag layer; `raise SystemExit` only in `verbs/publish.py`, `verbs/score.py`, `lock.py`, `build.py`. Flag laws: `build`'s `--push`/`--push-to`
  refuse together; `generate-score`'s `--target` default derives from the config; a spelled target naming an absent substrate refuses naming the narrower spelling;
  `docker-build-matrix` answers a bare JSON array. `--lock-only`/`--check`/`--upgrade` belong to `lock`, never `build`; no `--gpu`, no `--compose-file` (the verbs own the compose invocation); `tree-tags` answers a bare string — no JSON, no `{SERVICE}_SHA` names.
  `bump` writes `boatyard.k8s-pins.yml` (sole writer; `git revert` rolls back — never hand-edit): clean tree required (the pins file itself tolerated), HEAD's tree tags certified against the CI shelf's `sha-{HEAD}` shards, k8s output
  regenerated from the written pins, self-skips where no `k8s:` section.

## Modules

`config/config.py` schema + load; `ConfigFile.resolve_services(choices)` folds a selection into leaf services; `dockerfiles.py` declarations + sha machinery (`ServiceBuild`, `derive_services`); `config/lock.py` lock schema + paired `LockFile.load(path, config)`;
  `yaml.py` `load_yaml` strict load (duplicate-key walk) + `load_yaml_model`; `config/values.py` the typed values file (sections, load-time validation, interpolation, the `assembly()` derivation `up` renders from); `config/k8s_pins.py` `parse_pins`/`load_pins` (pydantic-validated, answer `dict[str, str]`); `paths.py` the consumer-repo document paths; `temp.py` shared digest/platform/tree-tag regex spellings; `lock.py` relock
  engine + crane resolution; `compose.py` the compose invocation spelling `compose_command` (both verbs); `build.py` build pipeline; `markers.py` marker resolution; `score.py` score shared base; `score_compose.py`/`score_k8s.py` rendering pipelines;
  `score_generate.py` `generate_artifacts` (the score verb); `publish.py` publish preparation; `crane.py` pinned crane installer; `verbs/up.py` + the up-only
  `render_env_entries`/`lock_env_unwind`. Model modules run root → leaves: root record first (`RootModel` subscripts quote forward refs); `dockerfiles` ← `config/config` ← `config/lock` ←
  `lock` one-way. Also in `config/config.py` (the shared leaf): `double_dollars` (the one `$`-doubling home), `ENV_NAME_PATTERN`, `EDGE_SERVICE_NAME`, `BARRIER_SERVICE`, `lock_key_prefix`, `chart_lock_key`, `bundle_lock_key`,
  `env_lock_slot`/`LOCK_SLOT_MARKER` + `env_tree_slot`/`TREE_SLOT_MARKER` + `ConfigFile.tree_sentinels`/`TREE_SENTINEL_PATTERN` (`RESERVED_SLOT_MARKERS`), `compose_bundle_output_path` (the one compose-output path join), `qualified_option_name` (the one qualified-name join), `SlotDemand`/`SecretsDeclaration`, `authored_slot_demands` (the config-load consumer-surface scan) / `artifact_slot_demands` (the marker-excluding artifact scan), `scan_bundle_texts` (answers `BundleScan`),
  `refuse_unresolved_references` (`prefix` composes the `{entry}-` namespace), `load_workloads`, `select_provisioner_files`, `refuse_colliding_service_names`.

## ConfigFile schema

- `extra="forbid"` everywhere; per-field shape laws in `field_validator`s, cross-field laws in one `model_validator(mode="after")` per model.
- `requires` self-checks the installed version at load (`0.0.0.dev0` dev sentinel bypasses the check, not the field).
- `base_images`: ALL_CAPS keys become build args; `path:tag`/`path@digest` values; `{uv}`/`{python}` templates over toolchain pins (`{uv}` from `tool.uv.required-version`, `==` pin only; `{python}` from `.python-version`); a literal value in place of a template =
  the deliberate-skew escape hatch.
- `variants`: ALL_CAPS axis → bare lowercase values, first = the flagless build's default; declared, never probed. `platforms` required exactly when `base_images` is; charts arch-neutral, exempt.
- `project_name` and a non-empty `services` are required on every config (compose is unconditional; `k8s` is the optional substrate); `gateway` is optional — routes require it, and declaring it requires `CADDY_IMAGE` and renders the edge, routes or not.
- `services`: groups (`options:` mutually exclusive) + bare riding entries; every entry/option is `ref:` (tag-pinned OCI include — digest/`latest`/range spellings refuse at load) or `workloads:`/`vars:`; workload references are
  flat `*.yaml` names (`metadata.name` pins on stems); the vocabulary refuses duplicates at load; `auth` reserved — options declare exactly one route; routes `/`-rooted, edge-wide unique, `strip_prefix` requires a wildcard; ports use
  the mapping form (service, published, target).
- `k8s`: `workloads`, `vars`, `charts` name → `oci://…:tag|@digest`; the map key IS the `{NAME}_CHART` lock key; a section declaring neither workloads nor charts refuses.
- Load refusals (`ConfigFile.resolve`): Dockerfile services or local entries without `repository`; a declared chart without `stack/score/k8s/values/{chart}.yaml`; an absent workload file; a bare service name claimed twice in one entry — containers, type-joined provisioner `services:`, the reserved `wait-for-resources` (`refuse_colliding_service_names`; ambiguous `{stem}-{container}` spellings refuse too). Config load never reads the lock — the lock verb bootstraps a first lock from nothing.
- Every `load()` takes a full path and rides `load_yaml_model`; a missing file surfaces as raw `FileNotFoundError` at the call site that failed to check — `LockFile.load` is the one exception
  (missing-as-empty, under Lock).
- `mirrored_ref(reference, prefix)` = the only prefix join — injects the prefix, rejects an already-prefixed reference.
- Derivation shape law: `ConfigFile` carries authored data and validation; a member may hold real derivation logic (`local_service_entries`, `declared_charts`), but a member that only delegates to another module's function is a pass-through —
  call the function at the consumer, `compute_service_shas` module-qualified; `root` defaults to the working directory, `resolve` injects the load root, excluded, never-authored.
- `boatyard.values.yaml` (consumer root, gitignored) is the only machine-values source — the ambient shell fills nothing; `up` refuses without it, score generation and `down` run without it (`score_compose` reads `platform` as `None`). Closed typed top level
  (`bundles`, `path_overrides`, `public_url`, `platform`, `values`) with `extra="forbid"` at every level; the sections `values.images.<local entry>` / `values.bundles.<ref entry>` / `values.shared` hold scalar values under ALL_CAPS keys — a value may carry bare
  `${NAME}` references whose roots are the flattened keys ∪ minted slots (defaults/operators/nesting, marker-bearing names, unresolvable references, and cycles refuse at load, the unresolvable naming the roots; resolution happens once, at `assembly()`'s
  env-file render — score generation never resolves); `PUBLIC_URL` (minted from `public_url`) refuses by hand. Every check rides `ValuesFile.load(path, config, lock)` over the demand table (authored consumer surfaces ∪ lock bundle `slots` records ∪ selected-override artifact scans): fill fires
  for participating units (bare + selected + overrides; own keys ∪ shared ∪ minted cover; shell-set unfilled names named as ignored), orphan/placement/shared fire selection-free (one demander → its home section, none → no surface, many → shared), and every
  demanded key is declared in `secrets:` or covered by a values key. Path-override refusals (empty, URL-scheme, wrong layout, non-ref target; a selected override's file must exist) fire at load; the scheme-less `public_url` refusal fires at parse.

## Service derivation

- The Dockerfile-derived build set computes in `dockerfiles.py`'s `derive_services`, never authored: Dockerfile dirs one level below a top-level dir = services (spanning several top-level dirs refuses); context edges from bare-identifier `FROM`s naming a
  sibling; a literal `FROM` or slash-bearing `COPY --from=` refuses; `ARG <axis>` fans over the axis's declared values, dependents inherit the base's variants exactly (independent axes over fanned bases, disagreeing parents, cycles refuse);
  `ARG {SERVICE}_SHA` declares the stamp receiver.
- `.dockerignore` = the allowlist the sha machinery hashes over a disk walk, staged per service into a temp git index (`git write-tree` minting the `tree-` OID); an empty service set returns `{}` without walking.

## Lock

- Format: top-level `platforms:` records the resolved platform set; `images:` name → `{index, platforms: {os/arch: reference}}`; `charts:` name → flat digest-pinned reference; `bundles:` name → `{reference, slots: {NAME: {required: bool}}}` — digest-pinned reference plus the bundle's
  slot demands (`:-`-defaulted = required false); `bundles:` keys are `{QUALIFIED_NAME}_BUNDLE` (a bare entry is its own name); written only by the `lock` verb and `build`'s incremental freeze (`relock` inline), over declared names only —
  built-image digests are unrepresentable, orphans prune by construction; never hand-authored.
- `LockFile.load(path, config)` = the only parser consumers use — config rides validation context and a missing lock reads as the empty lock paired against the config; schema-only refusals fire on every parse (non-digest-pinned entries, cross-section
  collisions; empty lock legal); the context-gated pairing validators refuse wholesale staleness naming the lock verb: recorded platforms ≠ `config.platforms`, an image entry's platform keys ≠ the top-level set, base_images/charts keys the lock lacks
  (coverage — bundles exempt, path overrides skip their entry), lock names the config no longer declares (orphans).
- `relock` (reads the committed lock schema-only inline — drift and old formats are its input, never refused) computes per `base_images` entry the mirror-prefixed reference resolved into the index digest + one platform-manifest digest per declared platform
  (`crane manifest` + `crane digest`); `@digest` declarations pass through prefixed-unresolved; declaration-incremental freeze: unchanged declarations copy forward, tag bump or platform-set change re-resolves, `--upgrade` re-resolves everything; charts freeze by
  prefix match; bundle refs resolve direct (first-party, never mirror-prefixed), frozen by tag match.
- Upstream manifest selection (inline in `relock`) = a pure function of the declarations — the writing machine's arch never participates; exact `os`/`architecture` match, `unknown/unknown` skipped; a platform matching anything but exactly one manifest refuses.
- Projection selection law: `lock.index_references()` answers the lock's image index references (build edge); `lock.manifest_references(platform)` answers the concrete per-platform manifests (compose edge) — a recorded singleton self-selects, a multi-platform lock
  names `platform:` through `ValuesFile.platform` (missing/not recorded refuses listing the recorded platforms), never host introspection; the consumer's machine platform governs producer locks too.
- The `lock` verb self-skips where no config or declarations exist (print, exit 0; CI runs it unconditionally); `--check` asserts instead of writes (in-memory relock, exit 1, nothing touches the tree).

## Mirror namespace

- Third-party images and charts are consumed through the org mirror namespace: upstream manifests get GC'd; CI = the single writer — engineers never push, and error text never teaches them to.
- Declarations are upstream-native; the prefixed form is derived; the `mirror` verb never enumerates bundle declarations.
- `up` never probes the registry — missing digests surface unclassified at bring-up via compose's include fetch and `--pull missing`.
- The `mirror` verb probes every recorded digest (`crane digest`; partially mirrored still triggers the copy; probe failure falls through to `crane copy`); one `crane copy {ref} {prefix}/{ref}` per lacking reference copies the whole
  index; every declared `base_images`/charts reference is a target; zero declared targets = a loud error; crane's installer always runs.
- `build --push` shelves one digest-manifest shard per matrix leg: `sha-{HEAD}-{sha256-8 of the leg's sorted baked targets}` over `{REGISTRY}/builds/images-digests-all`, manifest `{target: {ref, digest, tags}}`; the toolkit's release verb
  enumerates a SHA's shards and merges. `--push-to` never shelves; a repository push without `REGISTRY` env refuses naming it.

## Compose assembly and selection

- Selection law: `bundles:` names options only, by qualified `group-dash-option` spelling (a bare entry's name refuses as unselectable); ≤ one option per group; zero options leave riders/routes carrying the assembly; unknown names refuse listing the
  qualified vocabulary; every selection refusal and the fold into `choices` ride `ValuesFile.load(root, config, lock)` on every values load (`up` always; `generate-score` when present; `down` never). Membership law: selection validation and the override/env derivation live in the values module (`assembly()`); rendering and staged inputs live inline in `verbs/up.py`; nothing else lands there or in `compose.py`.
- Leaf-service law: `ConfigFile.resolve_services(choices)` answers leaf services — the option's routes/ports ride before its group's (the auth option's route is `routes[0]`), its resolved service carries `auth: True`, an unselected group rides nothing.
- Bundle overrides ride the values file's `path_overrides` map (qualified name → a `stack/generated/compose/`-layout path, repo-relative); unknown names refuse listing the vocabulary, unselected names are inert, and a name must resolve to a `ref:`
  entry — a local-entry name refuses listing the ref entries; an overridden ref skips its lock-entry requirement; a selected override's file must exist at values load and its artifact text is its demand surface; the producer repo's config + paired lock (the `boatyard.yml` at `parents[3]` above the output, projected under the consumer's platform) and the producer tree fans ride the values module's `assembly()` member; no boatyard.yml-bearing root at `parents[3]`, or a drifted producer lock, refuses naming the remedy.
- The materialized root carries the project `name:`, the `include:` list (remote entries as `oci://` + the lock's `{NAME}_BUNDLE` digest ref — the yaml tag never rides an include; a selected ref lacking its entry refuses naming `boatyard lock`; local outputs by
  absolute path), the emitted edge (iff a gateway is declared, attached to every selected bundle's `{entry}-net`), parent-file port stanzas.
- Namespacing law: score renders every bundle resource under `{entry}-` (services, configs keys) on a per-bundle network with the bare name as service alias — accidental cross-bundle DNS fails; cross-bundle coupling rides the
  consumer's declared surface. `up` composes `{entry}-` onto authored route upstreams and port owners; `boatyard.yml` stays bare. Local-output lock-key slots (the lock's key set draws the line) rewrite under `env_lock_slot(entry, key)`
  — the `LOCK_SLOT_MARKER` infix reserving them against the values file; consumer-surface slots stay bare. Service-sha slots ride the sentinel barrier: `generate_compose` mints
  `tree_sentinel` values over the build-set slot supply (score tools refuse bare `${VAR}` placeholders), `render_score` expands them into `${X_SHA}` workload slots as plain
  variable bindings, and `score_compose`'s post-pass rewrites sentinels to `env_tree_slot(entry, sha)` (single `$`, env-file filled) — so committed
  compose artifacts never bake `tree-` tags and are a pure function of stable inputs.
- Validation teeth: relock reads every declared bundle's content inline and refuses any namespaced `{entry}-` route upstream or port owner resolving to no service in the bundle; `assembly()` applies the same refusal to local outputs and path overrides, beside the reserved-`edge` refusal and the missing-output refusal naming generate-score; the slot fill/placement teeth ride values load; `up` never invokes `docker compose config`.
- Edge stanza: `image:` carries the lock's platform manifest for `CADDY_IMAGE` baked at assembly, `gateway:` as published port, `FORWARDED_PROTO`/`FORWARDED_PORT` derived from `public_url` under a selected auth option (refusing when absent); the edge does no
  token validation — re-protection is a bundle-shipped oauth2-proxy sidecar treaty; the service name `edge` is reserved.
- Caddyfile: one site on internal `:8443`; `handle` blocks mutually exclusive; `handle_path` for `strip_prefix`; `header_up X-Forwarded-*` only on the auth route; `raw:` content rides verbatim (escape hatch, review flag); the whole file rides inline as
  `configs.content`.
- Per-run files: lifetimes ride the caller-owned `ExitStack`, never an exception handler; the handoff execs — a successful run never unwinds; on failure the compose files unlink,   the env file keeps, path-line-only echo (`lock_env_unwind` — contents may carry
  secrets); `up` writes ONE per-run dotenv (`render_env_entries`): resolved values (minted slots included), lock entries, tree tags under `#` source headers — the lock section carries only `{ENTRY}__BOATYARD_LOCK__KEY` fans, one per selected local entry and one per path override (producer pins win; a remote-only
  selection attaches none); the tree section carries `{ENTRY}__BOATYARD_TREE__{SERVICE}_SHA` fans computed on demand from the source tree (`compute_service_shas` over the consumer repo for local entries, the producer repo for overrides; dirt is content);
  the values parse's marker refusal is the collision law; `compose_command` prefixes sorted `env -u` unsets when `slot_names` is non-empty; `down` reads no
  values file, no lock, no selection — a name-only root + `--remove-orphans` removes every container the project ever created; a config-less or `services:`-less repo refuses at load.

## Build

- `run_build` renders the deterministic per-run bake file (authored key order, byte-identical for identical configs); a failed bake re-raises with the rendered content attached.
- Push axis: `--push` (bool) pushes for the config's `repository`; `--push-to <addr>` re-roots tags under that registry (ad-hoc destination); the flags together refuse; a malformed address (scheme, trailing slash, bare hostname)
  refuses; `--push` without a declared `repository` refuses.
- `host_platform()` — the flagless default set is every service's first-declared variant restricted to this machine's arch (an empty default set refuses naming the arch + `--targets`); `--targets` addresses are `name` or `name/variant`
  (a bare name on an axis service refuses naming its variants; unknown names/variants refuse listing the vocabulary).
- The incremental lock write rides every building run (the `uv sync` shape — anyone who builds also locks), landing only after target selection + its refusals; a building run with zero derived services refuses naming the lock verb.
- Build is images-only: no artifact regeneration rides a build — freshness is wholly `generate-score --check`'s, keeping the score toolchain off build-only machines.
- `metadata.json`: cwd-stable, unlinked before the run, coverage-checked against the requested targets after; a repository push distills it into the shelved shard; local default-target builds run the daemon GC-limit check
  (`MIN_KEEP_STORAGE_GB` = 60; absent/null `defaultKeepStorage` refuses; no readable daemon.json warns and continues).

## Score generation

- Deterministic generation from `stack/score/` into `stack/generated/`; one compose rendering per local option + bare entry (output `stack/generated/compose/compose.{name}.yaml`); k8s = one monolithic rendering over `k8s.workloads` + `k8s.vars`,
  state.yaml pre-seeding `InstanceSuffix` = `-` + first 10 hex of sha256(metadata.name) per workload; `--local` writes the gitignored local output (local storage class); charts render through `helm template` against the lock's `{NAME}_CHART`
  entry (a chart without its lock entry refuses naming the lock verb; helm is a renderer, never a deployer); `stack/score/` stays read-only.
- Substrate law: compose renderings consume the lock's platform manifests (the artifact records the exact bytes one machine runs); k8s renderings consume the index form (kubelet resolves per node).
- `generate_artifacts` installs the target's tools into `/usr/local/bin` (presence + version probe, fetch on absence or pin mismatch).
- `--check` = the CI staleness gate: self-skips (exit 0) where nothing generates; otherwise regenerates; any `git status` change over the outputs exits 1 printing the diff; `--check` + `--local` are mutually exclusive. Compose outputs are
  git-free (identity rides tree slots) — only authored inputs move them; the k8s output bakes the pins file's tags — absent pins or an unpinned workload `*_SHA` slot refuses naming
  `bump`; `--local` bypasses pins with fresh working-tree tags; nothing reads the manifest back.
- Post-passes on compose output are typed transformations on `ComposeDocument`, dumped exactly once: a workload-major pass collapses `{workload}-{container}` services to the container name and inlines every `type: bind` volume as a top-level
  `configs.content` entry — zero host-filesystem references in committed artifacts; the barrier pins the `ALPINE_IMAGE` lock slot (`{entry}-wait-for-resources`).
- The image lock is required and non-empty; compose generation refuses until `base_images` declares `ALPINE_IMAGE` (the barrier's image).
- `$`-doubling law, stated once: a literal `$` doubles whenever file text rides inside a compose document (edge Caddyfile, publish's file-sourced configs, score's bind inlining); text outside a compose document (k8s-side inclusion, k8s manifests) never doubles.
- `docker-build-matrix` leg schema (`targets`, `variant`) — renaming a key breaks the binding's fanout YAML; one partition per closure × variant; `targets` carries `--targets` addresses, never dashed target names.

## Provisioners and markers

- Authored provisioner files carry `state`/`outputs`/`volumes`/`services` as real YAML mappings + `manifests` as a real list — score's string form is generated, never authored; a field already in string form = a loud validation error; `extra="forbid"`.
- Compilation resolves include markers, then lock markers, both before validation; the compiled form lives in a per-run tempdir under `score_dir`, never the committed tree; authored key order controls dump order; registration derives by resource-type closure in
  sorted-filename order; orphan provisioner files silently no-ship.
- Marker grammar: a single-key mapping; `include:` inserts bytes verbatim, `include-yaml:` inserts parsed structure; neither transforms content (`${VAR}` slots pass through); paths anchor at the repo root — absolute
  paths, `..` escapes, missing files, cycles = loud errors; text content is never scanned for markers; `lock: NAME`/`lock-image: NAME` substitute the full reference / registry/repository/tag/digest, scoped to provisioner compiles and chart values; workloads use
  `${NAME}` slots; a literal `image:` (no `${`/`{{` slot) refuses at every marker resolution — provisioner compiles, every chart values file (markers or not), and the workload lens.

## Secrets

- `boatyard.yml`'s `secrets:` section holds the declaration (absent refuses at generate-score; `secrets: {}` states zero) — `KEY: description` or `KEY: {description, mint: true}` (string specs coerce; keys ALL_CAPS); the cluster Secret derives `{project_name}-secrets`; k8s consumption = `secretKeyRef` naming it, validated as a subset per k8s rendering (chart-rendered evidence, the earliest moment it exists); compose-side coverage rides the values-load scan (every demanded key declared in `secrets:` or covered by a values key); the mint list artifact (`stack/generated/secrets.txt`) generates from the declaration alone.

## Publish

- Publishing happens iff local entries are declared — no flag, no section; artifact names derive from `repository` (`{repository}/{qualified-or-entry-name}`); `--plan` prints the entries as a bare JSON array (`[]` when none), no login, no publish — the CI getter's
  derivation source; bake substitutes the committed artifact's slots — the lock's index projection plus lock-key and tree-slot fans under the bundle prefix (`env_lock_slot`/`env_tree_slot`, the latter against `service_shas()` tree-tags); unresolved vars stay slots in the artifact for the
  consumer's values file; authored `configs.content` rides verbatim (single-`$` slots = the include consumer's interpolation surface — `$`-doubling is score's concern, not publish's); image refs under `{repository}/` pin to `@sha256` via `crane digest`; each entry
  publishes once under the `<sha>` tag, aliasing `<branch>` via `crane tag` (only the alias makes both tags name one digest); registry login reads `REGISTRY_USERNAME`/`REGISTRY_TOKEN`.

## Deployment-surface annotations

- Substrate needs beyond score's container model live in the workload file as `outernet.internal/*` annotations — `boatyard.yml` never carries a per-workload runtime fact; the shipped `score_patch_templates` are
  the reader (consumer repos never author one; refusal laws live in boatyard code); the lens is `WorkloadDocument`/`ComposeWorkloadDocument` (`metadata.name` pinned to the workload file stem; the compose
  lens adds httpGet-requires-probe-adapter), one spelling `WorkloadDocument.parse(raw, file_name)`.

| Annotation | Behavior |
|---|---|
| `outernet.internal/gpu` | cuda/rocm compose pair registers per `GPU_VARIANT`; `gpu-pod.tpl` on every k8s init; nodeSelector `placeframe.io/gpu`; runtime vocabulary, distinct from build-time variant axes |
| `outernet.internal/restart` | compose enum (`no`/`always`/`on-failure`/`unless-stopped`); outside refuses |
| `outernet.internal/probe-adapter` | `python-urllib` or `bash-http`; an httpGet probe without the annotation refuses loudly — score-compose silently drops httpGet |
| `outernet.internal/consumer-interpolated` | declared `files:` names ride un-doubled; a name matching no declared file refuses |

## This file's rules

The only reader is a coding agent: the code is the source of truth for mechanism; this file carries only what an agent would otherwise get wrong.

- Budget: 200 lines and 26KB, both hard — an addition crossing either cap cuts something else here; admit a line only if removing it would cause a mistake.
- One law, one home: search before adding; an existing law updates in place, never a second copy. The module map carries one line per module; laws live in sections named for the concern.
- Identifiers, flags, file paths, and refusal phrasings stay exact — they are what agents grep; no line exceeds 300 characters.
- A code change that invalidates a line here is a doc change in the same commit; a fact verifiable only against another repo or CI is a pointer to that owner, never restated here.
