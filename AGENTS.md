# boatyard

Docker-stack lifecycle verbs as a generic package for any repo shipping a compose graph; `up` derives the whole assembly from the root `boatyard.yml`. No Python
  API — the contracts are the verb CLI (`boatyard <verb>`), the declaration schema (`boatyard.yml` + `boatyard.lock.yml`), and committed artifact formats. Three-class principle: authored facts in the config, registry-resolved in the lock, tree-derived
  (`tree-<hash>`) computed on demand, never committed. PyPI identity starts at `0.1.0`; deps resolve from PyPI, git-source pins only in scratch branches. github-actions-toolkit  owns the consumer workflow contract and is never a project dependency (resolver cycle); from 1.0 on breaking changes ship with a manually bumped `major_minor`. Fresh-repo assumption: no code handles an obsolete format
  — a past artifact gets the plain parse refusal, never a migration path.

## Commands

Quick loop: `uv run pytest`, `uv run ruff check`; full gate: `uv run --locked scripts/preflight.py`.

## Verbs

One module per command under `verbs/`, each with its local bare `app = typer.Typer()` + `@app.command()` on the function; `cli.py` composes them with nameless `app.add_typer(...)` — flat top-level verbs (a named `add_typer` nests a group); one `boatyard`
console script. Exception law: nothing is ever caught only to be re-raised as a different exception with added information — hand-raised guards and third-party exceptions surface raw, uncaught, with full traceback; `typer.BadParameter`
owns the CLI-flag layer; `raise SystemExit` only in `verbs/publish.py` (registry credentials), `verbs/score.py` (stale artifacts), and `lock.py` (stale lock). Flags live in the typer signatures in `verbs/*.py`; the flag laws: `build`'s
`--push`/`--push-to` refuse together; `generate-score --target both` refuses (BadParameter) a one-substrate repo naming the matching narrower `--target` (the rendering pipelines carry no substrate-presence checks); `docker-build-matrix` answers a bare JSON array.
`--lock-only`/`--check`/`--upgrade` belong to `lock`, never `build`; no `--gpu`, no `--compose-file` (the verbs own the compose invocation); `tree-tags` answers a bare string — no JSON, no `{SERVICE}_SHA` names.

## Modules

`config/config.py` schema + load; `ConfigFile.resolve_services(choices)` folds a selection into leaf services; `dockerfiles.py` Dockerfile declarations + sha machinery (`ServiceBuild`, `derive_services`); `config/lock.py` lock schema + paired `LockFile.load(path, config)` + the projection members; `yaml.py` `load_yaml_model` + its duplicate-key walk (the YAML→pydantic parse boundary); `config/values.py` the typed values file; `paths.py` the consumer-repo document paths (`DEFAULT_CONFIG_PATH`/`VALUES_FILE`/`LOCK_PATH`); `temp.py` the shared digest/platform regex spellings
  (`ValuesFile.load`, `PublicUrl`); `lock.py` relock engine + crane resolution + `render_env_entries`; `compose.py` the compose invocation spelling
  `compose_command` (both verbs) + the up-only `require_env_file`/`lock_env_unwind`; `bake.py` build pipeline; `markers.py` marker resolution; `score.py` score shared base; `score_compose.py`/`score_k8s.py` rendering pipelines; `verbs/score.py` the score verb + `ensure_tools`;
  `publish.py` publish preparation; `crane.py` pinned crane installer; `verbs/up.py` selection + rendering inlined in `up`; `verbs/*` one module per command. Model modules run root → leaves: root record first
  (`RootModel` subscripts quote forward refs; the one inversion is a parent whose field default constructs the child), functions callers-before-callees; imports absolute; `dockerfiles` ← `config/config` ← `config/lock` ← `lock` one-way. Also in `config/config.py` (the shared leaf): `Yaml`,
  `is_mapping`/`is_list`, `double_dollars` (the one `$`-doubling home), `PLACEHOLDER`/`COMPOSE_HOLE`/`ENV_NAME_PATTERN`, `EDGE_SERVICE_NAME`, `lock_key_prefix`, `chart_lock_key`, `bundle_lock_key`,
  `compose_bundle_output_path` (the one compose-output path join), `qualified_option_name` (the one qualified-name join), `required_compose_holes` (the odd-parity hole scan over compose texts), `scan_bundle_texts`
  (the refuse-then-requires wrapper answering `BundleScan`), `refuse_unresolved_references`
  (the shared route/port refusal over compose texts; answers the services keys; `prefix` composes the `{entry}-` namespace).

## ConfigFile schema

- `extra="forbid"` everywhere; per-field shape laws in `field_validator`s, cross-field laws in one `model_validator(mode="after")` per model; both run in declaration order.
- `requires` self-checks the installed version at load (`0.0.0.dev0` dev sentinel bypasses the check, not the field); duplicate keys, unknown top-level keys, dead sections (`generate-score`, `publish-compose`, `lifecycle`, `bundles`,
  `lifecycle_prefix`) refuse.
- `base_images`: ALL_CAPS keys become build args; upstream-native `path:tag`/`path@digest` values; `{uv}`/`{python}` templates over toolchain pins (`{uv}` from `tool.uv.required-version`, exact `==` pin only; `{python}` from
  `.python-version`); a literal value in place of a template = the deliberate-skew escape hatch.
- `variants`: ALL_CAPS axis → bare lowercase values, first = the default cell; declared, never probed. `platforms` required exactly when `base_images` is; charts arch-neutral, exempt.
- `project_name` required iff `services`; `gateway` is optional until any route is declared (entry, group, or option) — then load refuses without it.
- `services`: groups (`options:` mutually exclusive) + bare riding entries; every entry/option is `ref:` (tag-pinned OCI include — digest/`latest`/range spellings refuse at load; any published channel spelling pins) or `workloads:`/`vars:`; workload references are flat `*.yaml` names (both sections; `metadata.name` pins on stems); an option's name outside its group is its
  qualified `group-dash-option` name; the vocabulary (qualified + bare spellings) refuses duplicates at load; `auth` reserved — options declare exactly one route; routes `/`-rooted, edge-wide unique, `strip_prefix` requires a wildcard; ports use the mapping form (service, published, target).
- `k8s`: `workloads`, `vars`, `charts` name → `oci://…:tag|@digest`; the map key IS the `{NAME}_CHART` lock key; a section declaring neither workloads nor charts refuses.
- Load refusals (`ConfigFile.resolve`): Dockerfile services or local entries without `repository`; a declared chart without `stack/score/k8s/values/{chart}.yaml`; routes without `gateway` or without `CADDY_IMAGE` in `base_images` (the lock half of that law rides the paired read's coverage). Config load never reads the lock — the lock verb bootstraps a first lock from nothing; lock consumers refuse wholesale on drift at the paired read, a missing entry refuses per `require`, and both name the remedy.
- Every `load()` takes a full path and rides `load_yaml_model` (`yaml.py`: read → duplicate-key walk → `yaml.safe_load` → None→`{}` → `model_validate` with context); a file parsed straight into a pydantic model rides it too (secrets, tool state, compose output); a missing file surfaces as raw `FileNotFoundError` at the call site that failed to check — `LockFile.load` is the one exception (its missing-as-empty policy is stated under Lock). Missing-file policy lives at callers: `lock`/`matrix`/`score`/`publish` self-skip or answer empty via their own `is_file` gate; relock's committed read is schema-only inline (`load_yaml_model` in `lock.py` — drift and old formats are its input, never refused); `score_compose` reads `platform` as `None` when the values file is absent; `up` refuses without it via `require_env_file`; `down` never reads it.
- `mirrored_ref(reference, prefix)` = the only prefix join — injects the prefix, rejects an already-prefixed reference.
- Derivation shape law: `ConfigFile` carries authored data and validation; a member may hold real derivation logic (`local_service_entries`, `declared_charts`, `resolve_services`), but a member that only delegates to another module's function is a
  pass-through — call the function at the consumer, `compute_service_shas` module-qualified (test seam); `root` defaults to the working directory, `resolve` injects the load root, excluded, never-authored.
- `boatyard.values.yaml` (consumer root, gitignored) is the only machine-values source — the ambient shell fills nothing; `up` refuses without it (`require_env_file`), score generation and `down` run without it (`score_compose` reads `platform` as `None` on fresh checkouts). `ValuesFile.load(path, config)` hands the raw document to `model_validate` — the selection fold into `choices`, the selection refusals, and the path-override vocabulary/ref-entry refusals ride pydantic validators, with `ConfigFile` arriving via validation context. Typed fields (`bundles` list, `path_overrides` map keyed by qualified name, `public_url`, `platform`) carve the consumed spellings; every other key is a hole value on the open `variables` map (ALL_CAPS enforced — `ENV_NAME_PATTERN`). Path-override refusals (empty, URL-scheme, non-`stack/generated/compose/` layout) and the scheme-less `public_url` refusal are pydantic `ValidationError`s at parse; paths resolve in `up` against the repo root.

## Service derivation

- The Dockerfile-derived build set computes in `dockerfiles.py`'s `derive_services`, never authored: Dockerfile dirs one level below a top-level dir = services (spanning several top-level dirs refuses); context edges from bare-identifier `FROM`s naming a
  sibling (stage names skipped); a literal `FROM` or slash-bearing `COPY --from=` refuses (base images ride as build args); `ARG <axis>` fans over the axis's declared values, dependents inherit the base's cells exactly (independent axes
  over fanned bases, disagreeing parents, cycles refuse); `ARG {SERVICE}_SHA` declares the stamp receiver.
- `.dockerignore` = the allowlist the sha machinery hashes over a disk walk, staged per service into a temp git index (`git -c core.autocrlf=false update-index --add --stdin`, `git write-tree` minting the `tree-` OID); an empty service set returns `{}` without walking.

## Lock

- Format: top-level `platforms:` records the platform set the lock resolved for; `images:` name → `{index, platforms: {os/arch: reference}}` with every entry's platform keys equal to the top-level set; `charts:` name → flat digest-pinned reference; `bundles:` name → `{reference, requires}` — digest-pinned reference plus the
  bundle's odd-parity `${NAME}` holes minus `:-`-defaulted ones, recorded at fresh resolution and recomputed from the pinned
  artifact while the digest freezes (identical by content-addressing); `bundles:` keys are `{QUALIFIED_NAME}_BUNDLE` (qualified selection name upper-cased + `_BUNDLE`; a bare entry is its own name), arch-neutral; written only by the `lock` verb and
  `build`'s incremental freeze (`relock` inline), over declared names only — built-image digests are unrepresentable, orphans prune by construction; never hand-authored.
- `LockFile.load(path, config)` = the only parser consumers use — config rides validation context (the `ValuesFile.load` precedent) and a missing lock reads as the empty lock paired against the config (`score` pre-gates with its own lock-required refusal); schema-only refusals fire on every parse (duplicate keys refuse naming the line; non-digest-pinned entries, cross-section name collisions reject; malformed or non-mapping documents surface raw pydantic/pyyaml errors; empty lock legal); the context-gated pairing validators refuse wholesale staleness naming the lock verb: recorded platforms ≠ `config.platforms`, an image entry's platform keys ≠ the top-level set, base_images/charts keys the lock lacks (coverage — bundles exempt, path overrides skip their entry), lock names the config no longer declares (orphans).
- `relock` (module-level in `lock.py`; reads the committed lock schema-only inline — drift and old formats are its input, never refused) computes per `base_images` entry the mirror-prefixed reference resolved into the index digest + one platform-manifest digest per declared platform (`crane manifest` + `crane digest`, crane always installed at relock entry; `crane copy` byte-identity holds both digests); `@digest`
  declarations pass through prefixed-unresolved; declaration-incremental freeze: unchanged declarations copy forward, tag bump or platform-set change re-resolves, `--upgrade` re-resolves everything; charts freeze by
  the same prefix match; bundle refs resolve direct — first-party, never mirror-prefixed — via `crane digest`, frozen by the same tag match.
- Upstream manifest selection (inline in `relock`) = a pure function of the declarations — the writing machine's arch never participates; exact `os`/`architecture` match; `unknown/unknown` + attestation entries skipped; a platform matching anything but exactly one manifest refuses.
- Projection selection law: `lock.index_references()` answers the lock's image index references (build edge); `lock.manifest_references(platform)` answers the concrete per-platform manifests (compose edge) and owns platform selection over the lock's recorded set — a recorded singleton self-selects, a multi-platform lock names `platform:` through `ValuesFile.platform` (missing/not recorded refuses listing the recorded platforms), never host introspection; the consumer's machine platform governs producer locks too; charts ride in both (single depth).
- The `lock` verb self-skips where no config or no declarations exist (print, exit 0 — CI's freshness step runs it unconditionally); `--check` asserts instead of writes (in-memory relock, exit 1, nothing touches the
  tree).

## Mirror namespace

- Third-party images and charts are consumed through the org mirror namespace: upstream manifests get GC'd; CI = the single writer — engineers never push, and error text never teaches them to.
- Declarations are upstream-native; the prefixed form is derived; `mirrored_ref` = the single join; the `mirror` verb never enumerates bundle declarations.
- `up` never probes the registry — compose's include fetch and `--pull missing` surface missing digests, unclassified, at bring-up; a partial stack under `-d` is the accepted cost of no pre-flight probe.
- The `mirror` verb probes presence per reference over every recorded digest (`crane digest`; partially mirrored still triggers the copy; probe failure falls through to `crane copy`, the loud gate — probe errors never classified); one
  `crane copy {ref} {prefix}/{ref}` per lacking reference copies the whole index; every declared `base_images`/charts reference is a target — nothing filtered; zero declared targets = a loud error; the
  footer splits copied/present; crane's installer always runs so the pin, not a preexisting binary, decides the version.
- `build --push` leaves bake's `metadata.json` in the working directory — the handoff the toolkit's builds_ref step reads; boatyard reads no `REGISTRY` facts.

## Compose assembly and selection

- Selection law: `bundles:` names options only, by qualified `group-dash-option` spelling (a bare entry's name refuses as unselectable); ≤ one option per group; zero options leave the riders/routes carrying the assembly (omitting the auth pick = the anonymous edge) — an options-only
  repo with an empty selection hands compose a name-only root and compose's `no service selected` refuses at bring-up; unknown names refuse listing the qualified vocabulary; every selection refusal (unknown names, one-per-group) and the fold into `choices` (group → option) ride
  `ValuesFile.load(root, config)` (the model's validators) on every values load (`up` always; `generate-score` when the file exists; `down` never). Membership law: selection consumption,
  rendering, and staged inputs live inline in `verbs/up.py`; `compose.py` carries the invocation spelling `compose_command` (both verbs) and the up-only `require_env_file`/`lock_env_unwind`; nothing else lands there.
- Leaf-service law: `ConfigFile.resolve_services(choices)` answers leaf services — the option's routes/ports ride before its group's (the auth option's route is `routes[0]`), its resolved service carries `auth: True`, an unselected group rides nothing; `up` consumes only these.
- Bundle overrides ride the values file's `path_overrides` map per machine (qualified name → a `stack/generated/compose/`-layout path, relative to the repo root); unknown names refuse listing
  the vocabulary, unselected names are inert, and a name must resolve to a `ref:` entry — a local-entry name refuses listing the ref entries; an overridden ref skips its lock-entry requirement; the override carries the local-output teeth (exists, parses, no `edge` service, names and
  requirements validated against the file) and contributes
  the producer repo's config + paired lock — `_producer_root(path)` walks up to the nearest `boatyard.yml`, then `ConfigFile.load` + the paired `LockFile.load`, projected via `manifest_references` under the consumer's platform — to the per-run env file under the entry prefix; no repo root above the override, or a drifted producer lock, refuses naming the remedy.
- The materialized root carries the project `name:`, the `include:` list (remote entries as `oci://` + the lock's `{NAME}_BUNDLE` digest ref — the yaml tag never rides an include; a selected ref lacking its entry refuses naming
  `boatyard lock`; local outputs by absolute path; `services:` declaration order), the emitted edge (iff any route rides, attached to every selected bundle's `{entry}-net` network), parent-file port stanzas; compose
  merges parent ports into included services and honors `!reset` — the merge needs compose ≥ 2.40.
- Namespacing law: score renders every bundle resource under `{entry}-` (services, configs keys) on a per-bundle network carrying the bare name as a service alias — include collisions are structurally impossible,
  rendered hostnames still resolve, and accidental cross-bundle DNS fails instead of silently resolving: a bundle references only its own services; cross-bundle coupling rides the consumer's declared surface
  (env holes, route upstreams). `up` composes `{entry}-` onto authored route upstreams and port owners; `boatyard.yml` stays bare. Local-output lock-key holes (the lock's key set draws the line)
  rewrite under `lock_key_prefix(entry)` in the same spelling (`{ENTRY}_ALPINE_IMAGE`-style); consumer-surface holes (edge image, gateway/ports, secrets, consumer-interpolated files) stay bare; publish fans
  its bake substitution keys with the prefix mechanically.
- Validation teeth: relock reads every declared bundle's content inline (the compose files ride as inline base64 layer data in `crane manifest`, `crane blob` the
  fallback) and refuses any namespaced `{entry}-` route upstream or port owner that resolves to no service in the bundle; `up` applies the same refusal to local outputs and path overrides by scanning the local
  file; `up` refuses a selected bundle whose recorded `requires` no environment source fills (boatyard.values.yaml or the
  lock env file; lock-key holes are satisfied by construction; a shell-set unfilled name gains a "set in the shell and ignored" clause); a selected bundle output that does not exist refuses naming generate-score; `up` never invokes `docker compose config`.
- Edge stanza: `image: ${CADDY_IMAGE:?err}` (the lock hole; the config-declared key is coverage-guaranteed in the lock), the Caddyfile configs entry, `gateway:` as published port, `FORWARDED_PROTO`/`FORWARDED_PORT` derived from `public_url` under a selected auth option (refusing when absent); the
  edge does no token validation — re-protection is a bundle-shipped oauth2-proxy sidecar treaty; the service name `edge` is reserved.
- Caddyfile: one site on internal `:8443`; `handle` blocks mutually exclusive (caddy sorts longest-path-first, declaration order cannot shadow); `handle_path` for `strip_prefix`; `header_up X-Forwarded-*` only on the auth route; `raw:`
  content rides verbatim (escape hatch, review flag); the whole file rides inline as `configs.content` with every `$` doubled.
- Per-run files: lifetimes ride the caller-owned `ExitStack`, never an exception handler; the handoff execs — a successful run never unwinds, the files ride out in the OS temp dir; on failure the compose files unlink, the env file
  keeps with a path-line-only echo (`lock_env_unwind` — its contents may carry secrets); `up` writes ONE per-run dotenv inline (`render_env_entries` from `lock.py`): values then lock entries under `#` source headers; a key in both refuses loudly (the lock is its only source); `up` fills it from the consumer's lock bare (the root document's holes) plus a fan under every selected local entry's prefix, and each path-override producer's lock under the overridden entry's prefix (producer
  pins win; empty entries attach none — a bundles-only lock carries nothing compose consumes);
  `compose_command` = the one docker compose invocation spelling, prefixed `env -u K1 -u K2 …` (sorted) when `hole_names` is non-empty — the scrub defeating compose's shell-beats-env-file precedence; `up` passes values ∪ lock entries ∪ requirement names; `down` reads no values file, no lock, no selection — a name-only root + `--remove-orphans` removes
  every container the project ever created; a config-less or `services:`-less repo refuses loudly on both verbs.

## Build

- `run_build` renders the deterministic per-run bake file (authored key order, byte-identical for identical configs; bake validates its own input — no document model); it takes the already-loaded `config` — the build verb owns its flag refusals; a failed bake re-raises with the rendered content attached.
- Push axis: `--push` (bool) pushes for the config's `repository`; `--push-to <addr>` re-roots tags + cache refs under that registry; the flags together refuse; an address with a scheme, trailing slash, or host
  carrying neither `.` nor `:` refuses; `--push` without a declared `repository` refuses.
- `host_platform()` = the one host fact left: default targets are every service's default cell restricted to this machine's arch, and an empty default set refuses naming the arch + `--targets` (bake with no target arguments builds every
  service, silently defeating the selection); `--targets`: bare name = default cell, `name/cell`, suffixed target name; unknown names/cells refuse listing the vocabulary.
- Per target: dockerfile by convention; platforms collapse to the host arch in load mode (a single declared platform rides through); args — every `base_images` key as `${KEY:-}` on every service, the axis
  ARG at its cell value, `{SERVICE}_SHA` as `${…:?err}`; tags `{namespace}/{target}:latest` + `:${SERVICE_SHA}`; `target:`-prefixed `additional_contexts`; registry cache refs under `{namespace}/build-cache:{target}` in push mode.
- The incremental lock write rides every building run (the `uv sync` shape — anyone who builds also locks), landing only after target selection + its refusals; bake reads the fresh entries as environment (index form); a building run with
  zero derived services refuses naming the lock verb.
- `metadata.json`: cwd-stable, unlinked before the run, coverage-checked against the requested targets after; local default-target builds run the daemon
  GC-limit check (`MIN_KEEP_STORAGE_GB` = 60; absent/null `defaultKeepStorage` refuses with a sample snippet, an explicit zero is a set value refusing below the floor; no readable daemon.json warns and continues).

## Score generation

- Deterministic generation from `stack/score/` into `stack/generated/`; one compose rendering per local option + bare entry (output `stack/generated/compose/compose.{name}.yaml`; the compose state dir is re-created per entry); k8s = one
  monolithic rendering over `k8s.workloads` + `k8s.vars`, untouched by `bundles:`, with instance names deterministic by construction — state.yaml pre-seeds `InstanceSuffix` = `-` + first 10 hex of sha256(metadata.name) per workload between init
  and generate (state-format coupling bounded by the pinned tool version); `--local` writes the gitignored local output with the local storage class; charts render through `helm template` against the
  lock's `{NAME}_CHART` entry (a chart without its lock entry refuses naming the lock verb; helm is a renderer, never a deployer); `stack/score/` stays read-only; output reproduces from a fresh clone.
- Substrate law: compose renderings consume the lock's platform manifests (the artifact records the exact bytes one machine runs); k8s renderings consume the index form (kubelet resolves per node).
- `ensure_tools` installs the three pinned tools into `/usr/local/bin`: version-probe each, fetch on absence or pin mismatch.
- `--check` = the CI staleness gate: self-skips (exit 0) where nothing generates; otherwise regenerates in-process; any `git status` change over the outputs exits 1 printing the diff; `--check` + `--local` are mutually
  exclusive.
- Post-passes on compose output are typed transformations on `ComposeDocument`, dumped exactly once — pydantic's declared-fields-first order is the artifacts' deterministic key order: a workload-major pass collapses `{workload}-{container}` services to the container name and inlines every `type: bind` volume as a top-level
  `configs.content` entry — zero host-filesystem references in committed artifacts; the `$`-doubling exception rides the owning workload's consumer-interpolated declaration; workload-less services ride through under
  their rendered names; the barrier pins the `ALPINE_IMAGE` lock hole, namespacing like every service (`{entry}-wait-for-resources`, `depends_on` rewritten per rendered dependency) and every lock-key hole under the
  entry prefix; each collapsed service name is
  claimed once (collision loud); the `{entry}-` namespace pass covers services, configs keys, and the per-bundle network with bare aliases.
- The image lock is required and non-empty; the compose target requires the `ALPINE_IMAGE` entry.
- `$`-doubling law, stated once: a literal `$` doubles whenever file text rides inside a compose document (edge Caddyfile, publish's file-sourced configs, score's bind inlining); text outside a compose document (k8s-side inclusion, k8s manifests) never doubles.
- `docker-build-matrix` cell schema (`targets`, `variant`) = a named contract with the binding — renaming a key breaks its fanout YAML; one partition per closure × variant cell.

## Provisioners and markers

- Authored provisioner files carry `state`/`outputs`/`volumes`/`services` as real YAML mappings + `manifests` as a real list — score's string form is generated, never authored; a field already in string form = a loud validation error; `extra="forbid"`.
- Compilation resolves include markers, then lock markers, both before validation; the compiled form lives in a per-run tempdir under `score_dir`, never the committed tree; `--provisioners` paths stay POSIX-relative;
  authored key order controls dump order at every level; comments are dropped; registration derives by resource-type closure in sorted-filename order;
  orphan provisioner files silently no-ship.
- Marker grammar: a single-key mapping (authored sources stay plain-YAML-parseable); `include:` inserts bytes verbatim, `include-yaml:` inserts parsed structure; neither transforms content (`${VAR}` holes pass through; `$`-handling is
  per-consumer); paths anchor at the repo root — absolute paths, `..` escapes, missing files, cycles = loud errors; text content is never scanned for markers; `lock: NAME`/`lock-image: NAME` substitute the full reference /
  registry/repository/tag/digest (grafana's `digest_field: sha`), scoped to provisioner compiles and chart values (k8s provisioners have no env file); workloads use `${NAME}` holes; a literal `image:` (no `${`/`{{`
  hole) refuses at every marker resolution — provisioner compiles and every chart values file, markers or not — and at the workload lens.

## Secrets

- `stack/secrets.yaml` is required (missing refuses at generate-score; an empty file = the empty declaration) — `KEY: description` or `KEY: {description, mint: true}` (string specs coerce; keys ALL_CAPS); each rendering's consumed keys validate as a subset (pair-variance legal);
  the cluster Secret derives `{project_name}-secrets`; k8s consumption = `secretKeyRef` naming it (`envFrom`/`extraEnvFrom` `secretRef`s impose no per-key constraint); compose consumption = `${ALL_CAPS}` brace holes with odd `$`-parity (`$$`-escaped
  `configs.content` doesn't count; lock-key holes don't count — the per-run env file fills them); the mint list artifact (`stack/generated/secrets.txt`) generates from the declaration alone.

## Publish

- Publishing happens iff local entries are declared — no flag, no section; artifact names derive from `repository` (`{repository}/{qualified-or-entry-name}`); `--plan` prints the entries as a bare JSON array (`[]` when none), no
  login, no publish — the CI getter's derivation source; bake substitutes `${ALL_CAPS}` holes against `service_shas()` tree-tags + the lock's index projection, the lock keys fanned under the bundle prefix for the
  artifact's namespaced spellings; unresolved vars stay holes in the artifact for the consumer's values file
  (non-empty stubs exist only so publish's interpolation validation passes); authored `configs.content` rides verbatim (single-`$` holes = the include consumer's interpolation surface — `$`-doubling is score's concern, not publish's); image
  refs under `{repository}/` pin to `@sha256` via memoized `crane digest`; each entry publishes once under the `<sha>` tag, aliasing `<branch>` via `crane tag` (compose stamps wall-clock `created` — only the alias makes both tags name one digest); registry login reads `REGISTRY_USERNAME`/`REGISTRY_TOKEN`.

## Deployment-surface annotations

- Everything the substrate needs that score's container model doesn't carry lives in the workload file as `outernet.internal/*` annotations — `boatyard.yml` never carries a per-workload runtime fact; the shipped patch templates
  (package data in `src/boatyard/score_patch_templates/`) are the annotation reader — one annotation, one template, one behavior; consumer repos never author one; refusal laws live in boatyard code (templates are data and cannot
  refuse); the lens is `WorkloadDocument`/`ComposeWorkloadDocument` (`metadata.name` pinned to the workload file stem; the compose lens adds httpGet-requires-probe-adapter), one spelling `WorkloadDocument.parse(raw, file_name)`;
  score parses workloads once and propagates `metadata.annotations` into both renderings.

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
