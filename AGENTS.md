# boatyard

Docker-stack lifecycle verbs as a generic package for any repo shipping a compose graph: bring-up/tear-down, cross-build, score generation; `up` derives the whole assembly, edge caddy included, from the root `boatyard.yml`. No Python
  API — the contracts are the verb CLI (`boatyard <verb>`), the declaration schema (`boatyard.yml` + `boatyard.lock.yml`), and committed artifact formats. Three-class principle: authored facts in the config, registry-resolved in the lock, tree-derived
  (`tree-<hash>`) computed on demand, never committed. PyPI identity starts at `0.1.0`; deps resolve from PyPI, git-source pins only in scratch branches. github-actions-toolkit
  owns the consumer workflow contract and is never a project dependency (resolver cycle); from 1.0 on breaking changes ship with a manually bumped `major_minor`. Fresh-repo assumption: no code handles an obsolete format
  — a past artifact gets the plain parse refusal, never a migration path.

## Commands

- Quick loop: `uv run pytest`, `uv run ruff check`.
- Full gate: `uv run --locked scripts/preflight.py` (wraps `preflight-python`).

## Verbs

One module per command under `verbs/`, each with its local bare `app = typer.Typer()` + `@app.command()` on the function; `cli.py` composes them with nameless `app.add_typer(...)` — flat top-level verbs (a named `add_typer` nests a group); one `boatyard`
console script; `--install-completion` once covers all verbs. Error-channel law: no verb boundary — authored-input refusals propagate with full traceback — hand-raised bare `ValueError` (byte-stable text), pydantic parses raw `ValidationError`; `typer.BadParameter`
owns the CLI-flag layer; `raise SystemExit` only in `verbs/publish.py` (registry credentials) and `verbs/score.py` (stale artifacts).

- `up`: `--attached/-a` `--quiet-pull/-q` `--build` `--dev`
- `down`: `--volumes/-v`
- `build`: `--push` (bool, the config's repository) `--push-to <addr>` (mutually exclusive) `--targets/-t` `--no-cache` `--dev`
- `lock`: `--upgrade/-u` `--check`
- `tree-tags`: positional service names, bare `tree-` tags on stdout
- `mirror`: no flags
- `publish-compose`: `--plan` `--bundle` `--branch`
- `generate-score`: `--target` `--local` `--check`; `both` means both substrates — the verb refuses (BadParameter) a one-substrate repo under `both` or the absent target, naming the matching narrower `--target`; the rendering pipelines carry no substrate-presence checks
- `docker-build-matrix`: bare JSON array on stdout

Gone flags stay gone: no `--lock-only`/`--check`/`--upgrade` on `build` (the `lock` verb owns them), no `--gpu` anywhere, no `--compose-file` (the verbs own the compose invocation). `tree-tags`' whole contract is a bare string — no JSON, no `{SERVICE}_SHA` names.

## Modules

`config.py` schema + load; `dockerfiles.py` Dockerfile declarations + sha machinery (`ServiceBuild`, `derive_services`); `config_lock.py` lock schema + parsers; `lock.py` lock engine + projections; `compose.py` compose invocation
  (`select`/`render` + kit); `bake.py` build pipeline; `markers.py` marker resolution (include splices, lock substitution); `score.py` score shared base; `score_compose.py`/`score_k8s.py` rendering pipelines; `verbs/score.py` the score verb (generation inline) + `ensure_tools`;
  `publish.py` publish preparation; `crane.py` pinned crane installer; `cli.py` the single console-script app composing the verb apps namelessly; `verbs/*` one module per command. Model modules run root → leaves: root record first
  (`RootModel` subscripts quote forward refs; the one inversion is a parent whose field default constructs the child), functions callers-before-callees; imports absolute; `config_lock`/`dockerfiles` ← `config` one-way. Also in `config.py` (the shared leaf): `Yaml`,
  `is_mapping`/`is_list`, `double_dollars` (the one `$`-doubling home), `PLACEHOLDER`/`COMPOSE_HOLE`/`ENV_NAME_PATTERN`, `EDGE_SERVICE_NAME`, `chart_lock_key`, `load_yaml_file`,
  `compose_bundle_output_path` (the one compose-output path join), `qualified_option_name` (the one qualified-name join). Lifecycle stays a separate package from codegen (disjoint dependency surfaces — bundling would force
  stack-only consumers to install `sqlacodegen`, `psycopg`, `datamodel-code-generator`).

## Config schema

- `extra="forbid"` everywhere; per-field shape laws in `field_validator`s, cross-field laws in one `model_validator(mode="after")` per model; both run in declaration order.
- `requires` self-checks the installed version at load (`0.0.0.dev0` dev sentinel bypasses the check, not the field); duplicate keys, unknown top-level keys, dead sections (`generate-score`, `publish-compose`, `lifecycle`, `bundles`,
  `lifecycle_prefix`) refuse.
- `base_images`: ALL_CAPS keys become build args; upstream-native `path:tag`/`path@digest` values; `{uv}`/`{python}` templates over toolchain pins (`{uv}` from `tool.uv.required-version`, exact `==` pin only; `{python}` from
  `.python-version`); a literal value in place of a template = the deliberate-skew escape hatch.
- `variants`: ALL_CAPS axis → bare lowercase values, first = the default cell; declared, never probed. `platforms` required exactly when `base_images` is; charts arch-neutral, exempt.
- `project_name` required iff `services`; `gateway` is optional — it refuses at assembly only when routes need it.
- `services`: groups (`options:` mutually exclusive) + bare riding entries; every entry/option is `ref:` (digest-pinned OCI include) or `workloads:`/`vars:`; workload references are flat `*.yaml` names (both sections; `metadata.name` pins on stems); an option's name outside its group is its
  qualified `group-dash-option` name; the vocabulary (qualified + bare spellings) refuses duplicates at load; `auth` reserved — options declare exactly one route; routes `/`-rooted, edge-wide unique, `strip_prefix` requires a wildcard; ports use the mapping form (service, published, target).
- `k8s`: `workloads`, `vars`, `charts` name → `oci://…:tag|@digest`; the map key IS the `{NAME}_CHART` lock key; a section declaring neither workloads nor charts refuses.
- `mirror_prefix`: root override (non-empty, no trailing slash), else `DEFAULT_MIRROR_PREFIX` (`ghcr.io/outernet-foundation/mirror`).
- Root-model invisible defaults: `score_dir` (`stack/score`), k8s output paths, the three tool pins.
- Load refusals (`Config.resolve`): Dockerfile services or local entries without `repository`; `base_images`/`k8s.charts` without the committed lock; a declared chart without `stack/score/k8s/values/{chart}.yaml`; routes without `CADDY_IMAGE` in the lock.
- `mirrored_ref(reference, prefix)` = the only prefix join — injects the prefix, rejects an already-prefixed reference.
- Derivation shape law: `Config` carries authored data and validation; a member may hold real derivation logic (`local_service_entries`, `declared_charts`), but a member that only delegates to another module's function is a
  pass-through — call the function at the consumer, `compute_service_shas` module-qualified (test seam); `root` defaults to the working directory, `resolve` injects the load root, excluded, never-authored;
  `parse_dev_toggles` = the one dev-toggles parser (ALL_CAPS, non-empty defaults, no duplicates).
- `.env` carries `BUNDLES=`, `COMPOSE_PROFILES`, `PLATFORM=`, dev-toggle overrides, the consumer's interpolation variables.

## Service derivation

- The Dockerfile-derived build set computes in `dockerfiles.py`'s `derive_services` (called from `Config.resolve`), never authored: Dockerfile dirs one level below a top-level dir = services (spanning several top-level dirs refuses); context edges from bare-identifier `FROM`s naming a
  sibling (stage names skipped); a literal `FROM` or slash-bearing `COPY --from=` refuses (base images ride as build args); `ARG <axis>` fans over the axis's declared values, dependents inherit the base's cells exactly (independent axes
  over fanned bases, disagreeing parents, cycles refuse); `ARG {SERVICE}_SHA` declares the stamp receiver; a genuine dev stage = an `AS dev` among several stages — single-stage `AS dev` names the published image.
- `.dockerignore` (where images build locally) = the allowlist the sha machinery hashes over a disk walk, staged per service into a temp git index (`git -c core.autocrlf=false update-index --add --stdin`, `git write-tree` minting the `tree-` OID); an empty service set returns `{}` without walking.

## Lock

- Format: `images:` name → `{index, platforms: {os/arch: reference}}`; `charts:` name → flat digest-pinned reference; written only by the `lock` verb and `build`'s incremental freeze (`relock`/`write_lock`), over declared third-party names only — built-image
  digests are unrepresentable, orphans prune by construction; never hand-authored.
- `parse_config_lock` = the only parser (path:line errors; duplicate keys, non-digest-pinned entries reject; empty lock legal); `read_config_lock` = the only reader of the committed file (a missing file reads as the empty lock); `ConfigLock.require` = the one "lock lacks NAME" refusal
  `recorded_digests` = the digest set the probes walk; the reference grammar (`LOCK_PATH`, `PLATFORM_PATTERN`, `SHA256_DIGEST_REGEX`) homes in `config_lock.py`.
- `relock` computes per `base_images` entry the mirror-prefixed reference resolved into the index digest + one platform-manifest digest per declared platform (`crane copy` byte-identity holds the digests under both names); `@digest`
  declarations pass through prefixed-unresolved, record uniformly; declaration-incremental freeze: unchanged declaration copies its frozen entry forward (same-tag re-pushes never diff a green branch), tag bump or platform-set change
  re-resolves, `--upgrade` re-resolves everything; charts freeze by the same prefix match.
- `resolve_remote_manifests` = a pure function of the declarations — the writing machine's arch never participates; exact `os`/`architecture` match; `unknown/unknown` + attestation entries skipped; multiple matches refuse; a missing platform refuses listing the index's platforms.
- Projection selection law: `env_entries` with no platform answers index references (build edge), with a platform its concrete manifests (compose edge); `compose_env_entries` owns platform selection — a declared singleton self-selects, a
  multi-platform repo names `PLATFORM=` through the merged shell-over-`.env` view (missing/empty/undeclared refuses listing the declared platforms), never host introspection; read projections stay in `lock.py`.
- `run_lock` self-skips where no config or no declarations exist (print, exit 0 — the CI freshness step runs it unconditionally in any repo); `--check` asserts instead of writes (in-memory relock, unified diff, exit 1, nothing touches the
  tree).

## Mirror namespace

- Third-party images and charts are consumed through the org mirror namespace: upstream manifests get GC'd; all pulls hit the mirror; CI = the single writer — engineers never push to the namespace, and error text
  never teaches them to.
- Declarations are upstream-native; the prefixed form is derived; `mirrored_ref` = the single join.
- `up` fails closed over the lock: probes every recorded digest (index + platform manifests + charts) before bring-up; only a registry 404 / manifest-unknown stderr verdict = the not-mirrored error — unauthorized or transport failures
  report as probe failures carrying the underlying text; no escape flag; the fix is to let CI's mirror job run.
- The `mirror` verb probes presence per reference over every recorded digest (`crane digest`; partially mirrored still triggers the copy; probe failure falls through to `crane copy`, the loud gate — probe errors never classified); one
  `crane copy {ref} {prefix}/{ref}` per lacking reference copies the whole index; every declared reference is a target — nothing filtered, so drifted declarations cannot silently mirror zero; zero declared targets = a loud error; the
  footer splits copied/present; bashrun + plain prints; crane's installer always runs so the pin, not a preexisting binary, decides the version.
- `build --push` leaves bake's `metadata.json` in the working directory — the handoff the toolkit's builds_ref step reads; boatyard reads no `REGISTRY` facts.

## Compose assembly and BUNDLES=

- Selection law: `BUNDLES=` names options only, by qualified `group-dash-option` spelling (a bare entry's name refuses as unselectable); ≤ one option per group; zero options legal exactly when riders or routes carry the assembly (omitting the auth pick = the anonymous edge); an options-only
  repo with an empty selection refuses; unknown names refuse listing the qualified vocabulary; the `up` entry gate refuses a set `BUNDLES=` in a repo without `services:`; `select()` owns the selection laws. `COMPOSE_PROFILES`: shell-env-over-`.env`, comma-split, deduped, forwarded
  as `--profile` flags. Membership law: everything about the docker compose invocation — selection, rendering, invocation spelling, staged inputs — lives in `compose.py`; nothing else lands there.
- The materialized root carries the project `name:`, the `include:` list (remote refs verbatim, local outputs by absolute path, in `services:` declaration order), the emitted edge (iff any route rides), parent-file port stanzas; compose
  merges parent ports into included services and honors `!reset` — the merge needs compose ≥ 2.40.
- Validation teeth, in `render()`: with routes or ports, `docker compose config --services` must list every route upstream + every port's owning service; selected profiles must appear in `config --profiles`; a selected
  bundle output that does not exist refuses naming generate-score; two selected local outputs sharing a service name refuse.
- Edge stanza: `image: ${CADDY_IMAGE:?err}` (the lock hole), the Caddyfile configs entry, `gateway:` as published port, `FORWARDED_PROTO`/`FORWARDED_PORT` derived from `PUBLIC_URL` under a selected auth option (refusing when absent); the
  edge does no token validation — re-protection is a bundle-shipped oauth2-proxy sidecar treaty; the service name `edge` is reserved.
- Caddyfile: one site on internal `:8443`; `handle` blocks mutually exclusive (caddy sorts longest-path-first, declaration order cannot shadow); `handle_path` for `strip_prefix`; `header_up X-Forwarded-*` only on the auth route; `raw:`
  content rides verbatim (escape hatch, review flag); the whole file rides inline as `configs.content` with every `$` doubled.
- Per-run files: lifetimes ride the caller-owned `ExitStack`, never an exception handler; the handoff execs — a successful run never unwinds, the files ride out in the OS temp dir; on failure the compose files unlink, the lock env file
  prints and keeps; `merged_environment` is authored in `compose.py` (assembly state, not a config derivation; a missing `.env` reads as empty so score generation runs on fresh checkouts); the lock env file materializes the
  selected-platform entries as a per-run second `--env-file`; `compose_command` = the one docker compose invocation spelling; `down` never reads the selection, never resolves an include — a name-only root + `--remove-orphans` removes
  every container the project ever created; a config-less or `services:`-less repo refuses loudly on both verbs.
- Dev overlay under `--dev`: an assembly-defined service with a genuine dev stage gets `image: {repository}/{service}:dev` + the repo-root → `/app:ro` bind; dev-toggles pairs become `${NAME:-default}` environment holes (`.env` overrides
  apply only under `--dev`); overlay keys outside the selected local outputs are filtered; `--dev` without a dev shape refuses.

## Build

- `run_build` renders the deterministic per-run bake file (authored key order, byte-identical for identical configs; bake validates its own input — no document model); it takes the already-loaded `config` — the
  build verb loads the config and owns its flag refusals; a failed bake re-raises with the rendered content attached.
- Push axis: `--push` (bool) pushes for the config's `repository`; `--push-to <addr>` re-roots tags + cache refs under that registry; the flags together refuse; an address with a scheme, trailing slash, or host
  carrying neither `.` nor `:` refuses; `--push` without a declared `repository` refuses; `--dev` with either push flag refuses (dev stages are local-only).
- `host_platform()` = the one host fact left: default targets are every service's default cell restricted to this machine's arch, and an empty default set refuses naming the arch + `--targets` (bake with no target arguments builds every
  service, silently defeating the selection); `--targets`: bare name = default cell, `name/cell`, suffixed target name; unknown names/cells refuse listing the vocabulary.
- Per target: dockerfile by convention; `target: dev` only under `--dev`; platforms collapse to the host arch in load mode (a single declared platform rides through); args — every `base_images` key as `${KEY:-}` on every service, the axis
  ARG at its cell value, `{SERVICE}_SHA` as `${…:?err}` for declaring Dockerfiles; tags `{namespace}/{target}:latest` + `:${SERVICE_SHA}` (dev builds tag `{namespace}/{service}:dev`, never SHA-tagged); `target:`-prefixed
  `additional_contexts`; registry cache refs under `{namespace}/build-cache:{target}` in push mode.
- The incremental lock write rides every building run (the `uv sync` shape — anyone who builds also locks), landing only after target selection + its refusals; bake reads the fresh entries as environment (index form); a building run with
  zero derived services refuses naming the lock verb.
- `metadata.json`: cwd-stable, unlinked before the run, coverage-checked against the requested targets after (a target with no tagged references rejects; output not covering the targets refuses); local default-target builds run the daemon
  GC-limit check (`MIN_KEEP_STORAGE_GB` = 60; absent/null `defaultKeepStorage` refuses with a sample snippet, an explicit zero is a set value refusing below the floor; no readable daemon.json warns and continues).

## Score generation

- Deterministic generation from `stack/score/` into `stack/generated/`; one compose rendering per local option (qualified name) + bare entry (output `stack/generated/compose/compose.{name}.yaml`; the compose state dir is re-created per entry); k8s = one
  monolithic rendering over `k8s.workloads` + `k8s.vars`, untouched by `BUNDLES=`, with instance names deterministic by construction — state.yaml pre-seeds `InstanceSuffix` = `-` + first 10 hex of sha256(metadata.name) per workload between init
  and generate (state-format coupling bounded by the pinned tool version); `--local` writes the gitignored local output with the local storage class; charts render through `helm template` against the
  lock's `{NAME}_CHART` entry (a chart without its lock entry refuses naming the lock verb; helm is a renderer, never a deployer); both tools run in their output directory (`score.py`); state deleted after the run — `stack/score/` stays read-only; output reproduces from a fresh clone.
- Substrate law: compose renderings consume the lock's platform manifests (the artifact records the exact bytes one machine runs); k8s renderings consume the index form (kubelet resolves per node).
- `ensure_tools` (verbs/score.py) installs the three pinned tools into `/usr/local/bin` (crane law): `version`-probe each, fetch on absence or pin mismatch.
- `--check` = the CI staleness gate: self-skips (exit 0) where nothing generates; otherwise regenerates in-process; any `git status` change over the outputs exits 1 printing the diff; `--check` + `--local` are mutually
  exclusive.
- Post-passes on compose output are typed transformations on `ComposeDocument`, dumped exactly once — pydantic's declared-fields-first order is the artifacts' deterministic key order: a workload-major pass attributes `{workload}-{container}` services per the workload's declared containers,
  collapsing each to the container name and inlining every `type: bind` volume as a top-level `configs.content` entry — zero host-filesystem references in committed artifacts; the `$`-doubling exception rides
  the owning workload's consumer-interpolated declaration; workload-less services (synthesized, provisioners) ride through under their rendered names; per-entry barrier rename `wait-for-resources` → `wait-for-resources-{entry}` pinned to the
  `ALPINE_IMAGE` lock hole with `depends_on` following; each collapsed service name is claimed once (collision loud); the edge-reserved check.
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
  the cluster Secret derives `{project_name}-secrets`; k8s consumption = `secretKeyRef` naming it (wholesale `envFrom`/`extraEnvFrom` `secretRef`s impose no per-key constraint); compose consumption = `${ALL_CAPS}` brace holes with odd `$`-parity (`$$`-escaped
  `configs.content` doesn't count); the mint list artifact (`stack/generated/secrets.txt`) generates from the declaration alone.

## Publish

- Publishing happens iff local entries are declared — no flag, no section; artifact names derive from `repository` (`{repository}/{qualified-or-entry-name}`); `--plan` prints the entries as a bare JSON array (`[]` when none), no
  login, no publish — the CI getter's derivation source; bake substitutes `${ALL_CAPS}` holes against `service_shas()` tree-tags + the lock's index projection; unresolved vars stay holes in the artifact for the consumer's `.env`
  (non-empty stubs set only so publish's interpolation validation passes); authored `configs.content` rides verbatim (single-`$` holes = the include consumer's interpolation surface — `$`-doubling is score's concern, not publish's); image
  refs under `{repository}/` pin to `@sha256` via memoized `imagetools inspect`; each entry publishes once under the `<sha>` tag and aliases `<branch>` with `crane tag` (compose stamps wall-clock `created`, so the alias is the only way both
  tags name one digest); registry login reads `REGISTRY_USERNAME`/`REGISTRY_TOKEN`.

## Deployment-surface annotations

- Everything the substrate needs that score's container model doesn't carry lives in the workload file as `outernet.internal/*` annotations — `boatyard.yml` never carries a per-workload runtime fact; the shipped patch templates
  (package data in `src/boatyard/patch_templates/`) are the annotation reader — one annotation, one template, one behavior; consumer repos never author one; refusal laws live in boatyard code (templates are data and cannot
  refuse); the lens is two models — `WorkloadDocument` (strict `dict[str, str]` annotations, `metadata.name` pinned to the workload file stem, typed `resources` map with `type` required) and `ComposeWorkloadDocument` (adds httpGet-requires-probe-adapter) — one lens spelling:
  `WorkloadDocument.parse(raw, file_name)` (stem pin; the compose lens inherits it); the score pipelines parse workloads once (`load_workloads` → `LoadedWorkload`: file name, raw, document); score propagates `metadata.annotations` into both renderings.

| Annotation | Behavior |
|---|---|
| `outernet.internal/gpu` | cuda/rocm compose pair registers per `GPU_VARIANT`; `gpu-pod.tpl` on every k8s init; nodeSelector `placeframe.io/gpu`; runtime vocabulary, distinct from build-time variant axes |
| `outernet.internal/restart` | compose enum (`no`/`always`/`on-failure`/`unless-stopped`); outside refuses |
| `outernet.internal/probe-adapter` | `python-urllib` or `bash-http`; an httpGet probe without the annotation refuses loudly — score-compose silently drops httpGet |
| `outernet.internal/consumer-interpolated` | declared `files:` names ride un-doubled; a name matching no declared file refuses |
| `outernet.internal/dev-toggles` | consumed at `up --dev`; no template, no artifact change |

## This file's rules

This file's only reader is a coding agent — an operations manual: the code is the source of truth for mechanism; this file carries only what an agent would otherwise get wrong.

- Budget: 200 lines and 26KB, both hard — an addition crossing either cap cuts something else here in the same change; admit a line only if removing it would cause an agent to make a mistake it otherwise wouldn't.
- One law, one home: search this file before adding a statement; if the law already exists, update it in place — never state it a second time. The module map carries one line per module and no laws; laws live in sections named for the concern.
- Identifiers, flags, file paths, and refusal phrasings stay exact — they are what agents grep.
- A code change that invalidates a line here is a doc change in the same commit.
- A fact verifiable only against another repo or a CI workflow is written as a pointer to that owner, never restated as a fact here.
- No line exceeds 300 characters; wrap prose to hold the 200-line cap.
