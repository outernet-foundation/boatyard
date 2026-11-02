# AGENTS.md compression: from architecture textbook to operations manual

Captured 2026-11-01 at the end of the docs-audit session. The refactor is
paused for this initiative; this plan is the whole initiative. Execution is a
single-session prose pass over `AGENTS.md`. Nothing in `src/` or `tests/`
changes (the test-cruft half of the audit already landed as commit
`0bd0c5c`).

## Orientation: the problem

`AGENTS.md` is 65,701 bytes across 100 lines (~16K tokens), injected whole at
every session start — and re-injected as a system-reminder on file reads in
some harnesses. Its module-table rows run past 2,000 characters, which is the
Read tool's per-line truncation point: the file's densest payload is
mechanically unreadable by its only reader. Every core law (compose ≥ 2.40
merge floor, up-probe stderr classification, mirror verb mechanics, lock
shape, crane byte-identity, gpu-not-runtime-vocabulary) is stated 2–3× across
module rows and thematic sections. The file contains zero dev commands — the
one content category with the strongest evidence behind it — while narrating
Caddyfile rendering order at length.

The file's only reader is a coding agent. No human reads it. That changes the
optimum completely: an agent has the code one grep away, follows instructions
it can find, and drowns in narrative.

Evidence this plan rests on:

- Müller et al., "Evaluating AGENTS.md" (arXiv:2602.11988): context files do
  not improve task success rates; instructions inside them are well followed;
  repository overviews do not help agents find relevant files faster. Only
  non-derivable instructions earn their tokens.
- Chatlatanagulchai et al. (arXiv:2601.20404): what AGENTS.md does buy is
  efficiency — −29% median runtime, −17% output tokens. Orientation and
  mistake prevention, not competence.
- "Agent READMEs" (arXiv:2511.12884): context files degrade "through
  frequent, small additions" — accretion is the universal failure mode, not a
  local mistake.
- Anthropic's memory guidance: under 200 lines; longer files reduce
  adherence; conflicting instructions resolve arbitrarily.
- Firsthand (this session's audit): every backtick symbol in the current file
  resolves against `src/` (accuracy is not the problem); the audit itself
  used perhaps 5% of the file's bytes; the 2–3× duplication actively
  manufactured verification work (which copy is current?).

Target: ~150–200 lines, ≤10KB, every law stated exactly once, zero
mechanism-narration, budget enforced by rules embedded in the file itself.

## Target state

### Budget

- ≤200 lines and ≤10,000 bytes, both hard.
- No line exceeds 300 characters (the 2,000-char Read-tool truncation is the
  cliff; stay far under it). Wrap prose lines near 100.

### Section order (fixed)

1. `# docker-devkit` — identity: 3–6 lines. What the package is, no Python
   API (contracts are exactly the verb CLI, the declaration schema
   `docker-devkit.yaml` + `docker-devkit.lock`, and committed artifact
   formats), the three-class principle in one sentence, consumer-workflow
   contract owned by github-actions-toolkit's `AGENTS.md`.
2. `## Commands` — dev commands: `uv run pytest`, `uv run ruff check`,
   preflight via `uv run --locked scripts/preflight.py`. Currently absent;
   highest-value section in the file.
3. `## Verbs` — the nine entry points, one line each: name → module → flags →
   one-phrase contract. Fold in the CLI-edge law (app object is `app`,
   command function names the command underscore-spelled, both Typer flag
   suppressions set) and the gone-flags law.
4. `## Modules` — the map, one line per module, table or list. Role routing
   only; zero laws in map rows.
5. `## <Concern>` sections — the laws, one home per law, sections named for
   the concern so they grep by the vocabulary an agent would search:
   `## Config schema`, `## Service derivation`, `## Lock`, `## Mirror
   namespace`, `## Compose assembly and BUNDLES=`, `## Build`, `## Score
   generation`, `## Provisioners and markers`, `## Secrets`, `## Publish`,
   `## Deployment-surface annotations`.
6. `## This file's rules` — the self-rules (below), always last.

### Phrasing rules

- One law per bullet, imperative mood, present tense. A law with three
  em-dash asides is three bullets.
- Exact identifiers, flags, paths, refusal phrasings stay verbatim — they
  are what agents grep, and they made the endgame reconcile cheap. Prose
  around them compresses ~10:1.
- Rationale stays only where it prevents a likely mistake (compose ≥ 2.40
  floor and the 2.24–2.39 error string; "bake with no targets builds every
  service, silently defeating the selection"; probe classification so a
  host-side failure can't masquerade as CI debt). History and persuasion
  ("proved unownable", "the disease placeframe already killed") compress to
  a clause or die.
- Mechanism narration is out of scope everywhere: constructor linearity,
  comprehension shapes, render pipelines — the code asserts these. The doc
  keeps only the invariants an editor would violate (per-run file lifetimes
  ride the caller-owned `ExitStack`; `compute_service_shas` stays a
  module-level late-bound global as the test seam; one-way import
  `config_lock` ← `config`).
- Nothing moves to `design/` or a new docs tree. This is compression, not
  relocation: the current file is present-truth doc per the org law. Dropped
  rationale essays die here (git history and this plan hold them).

## The self-rules section (embed verbatim, as the final section)

```markdown
## This file's rules

This file's only reader is a coding agent. It is an operations manual, not an
architecture description — the code is the source of truth for mechanism, and
this file carries only what an agent would otherwise get wrong.

- Budget: 200 lines and 10KB, both hard. An addition that would cross either
  cap requires cutting something else here in the same change.
- Admit a line only if removing it would cause an agent to make a mistake it
  otherwise wouldn't. Mechanism narration, history, and rationale that
  prevents no specific mistake are out of scope.
- One law, one home. Search this file before adding a statement; if the law
  already exists, update it in place — never state it a second time.
- The module map carries one line per module and no laws. Laws live in
  sections named for the concern.
- Identifiers, flags, file paths, and refusal phrasings stay exact — they
  are what agents grep.
- A code change that invalidates a line here is a doc change in the same
  commit.
- A fact verifiable only against another repo or a CI workflow is written as
  a pointer to that owner, never restated as a fact here.
- No line exceeds 300 characters; wrap prose near 100.
```

The rules section counts against the budget like any other section (~16
lines). It models the discipline it prescribes.

## Execution sequence

Whole-file replacement in one pass — not incremental edits. Incremental
dedup is how the file got here. Prose-only change; commits per the repo law
(prose separate from any code; none is expected).

1. **Verify the inventory's anchors.** The inventory below was extracted
   against the tree at commit `0bd0c5c`. Spot-check anything the intervening
   commits may have moved (`git log --oneline 0bd0c5c..HEAD -- src/`), and
   re-run the symbol scan if src moved: every backtick-quoted identifier in
   the new file must resolve against `src/` (extract with
   `rg -o '`\''[a-zA-Z_][a-zA-Z0-9_./-]*`'\''` --no-filename`, basename the
   last `.`/`/` segment, `rg --fixed-strings` each against `src/`).
2. **Resolve the flag list** (below) — each flag either verifies against
   code/toolkit and lands as a fact, or rewrites to a pointer/verified form.
3. **Write the new file** from the inventory: identity, commands, verbs,
   module map, law sections, self-rules. The inventory lines are near-target
   phrasings; tighten, don't expand. If the assembly overflows 200 lines,
   compress laws further — do not drop laws to fit; the drop list is already
   exhaustive below.
4. **Coverage diff.** Against `git show 0bd0c5c:AGENTS.md`, walk the old file
   claim-by-claim; every claim lands in exactly one bucket: present in the
   new file (once), on the drop list (with rationale below), or on the flag
   list (resolved in step 2). Anything in none of the three buckets is a
   regression — add it back compressed.
5. **Mechanical checks.** Line/byte budget; no line >300 chars; duplicate-law
   scan (grep the new file for each law's key phrase — exactly one hit);
   symbol scan from step 1; `uv run pytest -q` and `uv run ruff check` green
   (nothing should change, but the doc references tests).
6. **Land and clean up.** One commit for the rewrite; then delete this plan
   file in a second commit (the refactor-plan precedent). CLAUDE.md stays
   the one-line `@AGENTS.md` import — it is already correct.

## Law inventory (the coverage contract)

Near-target phrasings, grouped by target section. `K` = keep as law;
`M` = fold into the module map line only.

### Identity

- K: generic package owning the Docker-stack lifecycle verbs; consumers
  depend on it for bring-up/tear-down, cross-build, score generation; `up`
  derives the entire assembly — including the emitted edge caddy — from the
  root `docker-devkit.yaml`.
- K: no Python API; contracts = verb CLI + declaration schema + committed
  artifact formats.
- K: three-class principle — authored facts in the config, registry-resolved
  in the lock, tree-derived (`tree-<hash>`) computed on demand, never
  committed.
- K: PyPI identity starts at 0.1.0; `stack-toolkit` ≤0.1.1 distributions are
  deprecation signposts, not this package's history.
- K: deps resolve from PyPI (bashrun, packaging, pathspec, pydantic, pyyaml,
  strictyaml, typer); git-source pins only in scratch branches.
- K: github-actions-toolkit owns the consumer workflow contract; never a
  project dependency (resolver cycle); from 1.0 on breaking changes ship
  with a manually bumped `major_minor`.

### Verbs

- One line each: up (`--attached/-a`, `--quiet-pull/-q`, `--build`,
  `--dev`), down (`--volumes/-v`), build (`--push` optional-value,
  `--targets/-t`, `--no-cache`, `--dev`), lock (`--upgrade/-u`, `--check`),
  tree-tags (positional names, bare `tree-` tags on stdout), mirror,
  publish-compose (`--plan`, `--bundle`, `--branch`), generate-score
  (`--target`, `--local`, `--check`), docker-build-matrix (bare JSON array).
- K: gone flags stay gone — no `--lock-only`/`--check`/`--upgrade` on build
  (the lock verb owns them), no `--gpu` anywhere (variant cells are
  declared, not detected), no `--compose-file` (the verbs own the compose
  invocation).
- K: `tree-tags`' whole contract is a string — no JSON, no service-list
  endpoint, no `{SERVICE}_SHA` names (consumers own those names where they
  author the compose holes).

### Module map (M lines)

config.py (schema + record + sha machinery) · config_lock.py (lock schema +
parsers) · lock.py (lock engine + projections) · assembly.py (resolve/render/
validate stage) · bake.py (build pipeline) · score.py (score generation) ·
publish.py (publish preparation) · documents.py (shared typed lenses) ·
crane.py (pinned installer) · optional_value_option.py (typer `--push` seam,
`# noqa-wrapper:` boundary) · verbs/* (one module per command).

### Config schema

- K: `extra="forbid"` everywhere; exactly one `model_validator(mode="after")`
  per model, checks in declaration order; the single `mode="before"`
  exception is the services port-string coercion.
- K: `requires` version stamp self-checked at load (`0.0.0.dev0` dev sentinel
  bypasses the check, not the field); duplicate keys and unknown top-level
  keys refuse; dead sections (`generate-score`, `publish-compose`,
  `lifecycle`, `bundles`, `lifecycle_prefix`) refuse as extras.
- K: `base_images` — ALL_CAPS keys become build args; upstream-native
  `path:tag`/`path@digest` values; `{uv}`/`{python}` template over the
  toolchain pins (`{uv}` from `tool.uv.required-version`, exact `==` pin
  only; `{python}` from `.python-version`); literal value in place of a
  template is the deliberate-skew escape hatch.
- K: `variants` — ALL_CAPS axis name → bare lowercase values, first value is
  the default cell; declared, never probed.
- K: `platforms` required exactly when `base_images` is declared; charts are
  arch-neutral and exempt.
- K: `project_name` required iff `services` declared; `gateway` required iff
  any route is declared anywhere.
- K: services vocabulary — groups (`options:` mutually exclusive), bare
  riding entries; every entry/option is exactly one of `ref:` (digest-pinned
  OCI include) or `workloads:`/`vars:`; names share one flat vocabulary and
  collide-refuse; `auth` is a reserved group whose options declare exactly
  one route; route paths `/`-rooted and edge-wide unique, `strip_prefix`
  requires a wildcard; string-form ports name their entry's namesake
  service, group-level ports must use the mapping form.
- K: `k8s` — `workloads`, `vars`, `charts` name → `oci://…:tag|@digest`; the
  map key IS the `{NAME}_CHART` lock key; a section declaring neither
  workloads nor charts refuses.
- K: `mirror_prefix` root override (non-empty, no trailing slash), else
  `DEFAULT_MIRROR_PREFIX`.
- K: invisible defaults ride the root model — `score_dir` (`stack/score`),
  k8s output paths, the three tool pins.
- K: filesystem-gated refusals at load — leftover `workloads/images.yml` or
  `workloads/images.lock`; Dockerfile services or local entries without
  `repository`; `base_images`/`k8s.charts` without the committed lock
  (pairing law); a declared chart without `stack/score/k8s/values/{chart}.yaml`;
  routes declared without `CADDY_IMAGE` in the lock (cutover-lenient via
  `parse_committed_lock`).
- K: `mirrored_ref(reference, prefix)` is the only prefix join; injects the
  prefix, rejects an already-prefixed reference (double-prefix guard); lives
  in `config.py` beside `DEFAULT_MIRROR_PREFIX`.
- K: shape law — multi-consumer derivations ride `DockerDevkitConfig` as
  members (`compose_bundles`, `tool_requirements`, `image_lock`,
  `declared_charts`, `service_shas`); single-call-site derivations inline at
  their consumer; `score_dir_path()` is the only path member; `root` is
  load-injected, excluded, never-authored, asserted-present where needed;
  `compute_service_shas` stays a module-level late-bound global (the test
  seam) and is never inlined into the member; `parse_dev_toggles` is the one
  dev-toggles parser (ALL_CAPS, non-empty defaults, no duplicates).
- K: `.env` carries `BUNDLES=`, `COMPOSE_PROFILES`, `PLATFORM=` (multi-platform
  repos), dev-toggle overrides, and the consumer's interpolation variables.

### Service derivation

- K: the Dockerfile-derived build set computes inside `load_config`, never
  authored — Dockerfile-bearing dirs one level below a top-level dir are
  services; spanning several top-level dirs refuses; context edges come from
  bare-identifier `FROM`s naming a sibling (stage names skipped); a literal
  `FROM` and a slash-bearing `COPY --from=` refuse loudly (base images ride
  as build args under `base_images`); a Dockerfile declaring `ARG <axis>`
  fans over that axis's declared values, dependents inherit the base's cells
  exactly (independent axes over fanned bases, disagreeing parents, and
  cycles refuse); `ARG {SERVICE}_SHA` declares the stamp receiver; a genuine
  dev stage is an `AS dev` among several stages — single-stage `AS dev` names
  the published image.
- K: `.dockerignore` (where images build locally) is the allowlist the sha
  machinery hashes over `git ls-tree HEAD`; an empty service set returns `{}`
  without touching git.

### Lock

- K: lock format — `images:` name → `{index, platforms: {os/arch: reference}}`,
  `charts:` name → flat digest-pinned reference; written only by the `lock`
  verb and `build`'s incremental freeze, never hand-authored.
- K: `parse_image_lock` is the only reader any consumer uses (path:line
  errors; duplicate keys and non-digest-pinned entries reject; empty lock
  legal); `parse_committed_lock` is the lenient cutover read (absent → empty
  lock, env-line format → None); `ImageLock.require` is the one "lock lacks
  NAME" refusal; `recorded_digests` is the digest set the probes walk; the
  reference grammar (`LOCK_PATH`, `PLATFORM_PATTERN`, `SHA256_DIGEST_REGEX`)
  homes in `config_lock.py` and imports one-way — config imports it, never
  the reverse.
- K: `relock` computes per `base_images` entry the mirror-prefixed reference
  resolved into the index digest plus one platform-manifest digest per
  declared platform (`crane copy` byte-identity holds the digests under both
  names); `@digest` declarations pass through prefixed-unresolved and record
  uniformly; declaration-incremental freeze — unchanged declaration copies
  its frozen entry forward (same-tag re-pushes never diff a green branch),
  a tag bump or platform-set change re-resolves, `--upgrade` re-resolves
  everything; the computed lock owns exactly the declared names, so orphans
  prune by construction; charts freeze by the same prefix match.
- K: `resolve_remote_manifests` is a pure function of the declarations — the
  writing machine's arch never participates; exact `os`/`architecture` match,
  `unknown/unknown` and attestation entries skipped, multiple matches refuse,
  a missing platform refuses listing the index's platforms, a
  manifest-reference records uniformly.
- K: projection selection law — `env_entries` with no platform answers index
  references (build edge), with a platform its concrete manifests (compose
  edge); `compose_env_entries` owns platform selection: a declared singleton
  self-selects, a multi-platform repo names `PLATFORM=` through the merged
  shell-over-`.env` view (missing/empty/undeclared refuses listing the
  declared platforms), never host introspection; read projections stay in
  `lock.py` by authored placement — every consumer reaches through it.
- K: `run_lock` self-skips where no config or no declarations exist (print,
  exit 0 — the freshness step runs it unconditionally in any repo);
  `--check` asserts instead of writes (in-memory relock, unified diff, exit
  1, nothing touches the tree); an unparseable committed lock re-resolves
  wholesale — the rewrite is the cutover migration, no dual-read.

### Mirror namespace

- K: third-party images and charts are consumed through the org mirror
  (`ghcr.io/outernet-foundation/mirror/…`) — upstream manifests get GC'd and
  an upstream yank once broke external cloners; strangers, CI, and prod
  always pull the mirror; CI is the single writer — engineers never push to
  the namespace, and error text never teaches them to.
- K: declarations are upstream-native; the prefixed form is derived, the
  prefix the single place the namespace is named.
- K: `up` fails closed over the lock — probes every recorded digest
  (index + platform manifests + charts) before bring-up; stderr is
  classified: only a registry 404 / manifest-unknown verdict is the
  not-mirrored error; unauthorized or transport failures report as probe
  failures carrying the underlying text, so a host-side problem can't
  masquerade as unmirrored CI debt; no escape flag — the fix is to let CI's
  mirror job run.
- K: the `mirror` verb probes presence per reference over every recorded
  digest (`crane digest`; partially mirrored still triggers the copy; probe
  failure falls through to `crane copy`, the loud gate — probe errors are
  never classified); one `crane copy {ref} {prefix}/{ref}` per lacking
  reference copies the whole index; every declared reference is a target —
  nothing filtered, so a drifted declaration cannot silently mirror zero;
  zero declared targets is a loud error; footer splits copied/present;
  bashrun + plain prints — nothing from any runner floor; crane comes from
  the shared pinned installer (`crane.py` — install always runs so the pin,
  not a preexisting binary, decides the version).
- K: a `build --push` run leaves bake's `metadata.json` in the working
  directory — the handoff the toolkit's builds_ref step reads; the
  builds_ref distilling and all ORAS knowledge live toolkit-side in
  build-artifact-registry; the devkit reads no `REGISTRY` facts.

### Compose assembly and BUNDLES=

- K: selection law — `BUNDLES=` names options only (a bare entry's name
  refuses as unselectable); at most one option per group; zero options legal
  exactly when riders or routes carry the assembly (omitting the auth pick
  is the anonymous edge); an options-only repo with an empty selection
  refuses; unknown names refuse listing the vocabulary; the constructor owns
  the law; `COMPOSE_PROFILES` shell-env-over-`.env`, comma-split, deduped,
  forwarded as explicit `--profile` flags, unknown profiles refuse before
  probe and build.
- K: the materialized root carries the project `name:`, the `include:` list
  (remote refs verbatim, local outputs by absolute path, in `services:`
  declaration order), the emitted edge (iff any route rides), parent-file
  port stanzas; compose merges parent ports into included services and
  honors `!reset` — the parent-over-include merge needs compose ≥ 2.40
  (2.24–2.39 answer `conflicts with imported resource`); a test pins this
  against a version-pinned standalone compose binary.
- K: validation teeth — in the `Assembly` constructor, whenever routes or
  ports exist, `docker compose config --services` must list every route
  upstream and every port's owning service; selected profiles must appear in
  `config --profiles`; a selected bundle output that does not exist refuses
  naming generate-score; two selected local outputs sharing a service name
  refuse.
- K: edge stanza — `image: ${CADDY_IMAGE:?err}` (the lock hole), the
  Caddyfile configs entry, the `gateway:` value as published port,
  `FORWARDED_PROTO`/`FORWARDED_PORT` derived from `PUBLIC_URL` under a
  selected auth option (refusing when absent); the edge does no token
  validation — re-protection is a bundle-shipped oauth2-proxy sidecar
  treaty, not edge vocabulary; the service name `edge` is reserved.
- K: Caddyfile — one site on internal `:8443`; `handle` blocks mutually
  exclusive (caddy sorts longest-path-first, so declaration order cannot
  shadow); `handle_path` for `strip_prefix`; `header_up X-Forwarded-*` only
  on the auth route; `raw:` content rides verbatim (escape hatch, review
  flag); the whole file rides inline as `configs.content` with every `$`
  doubled.
- K: invariants an editor would violate — per-run file lifetimes ride the
  caller-owned `ExitStack`, never an exception handler; the handoff execs,
  so a successful run never unwinds and the files ride out in the OS temp
  dir; on failure the compose files unlink, the lock env file prints and
  keeps; `merged_environment` is authored here (assembly state, not a config
  derivation; missing `.env` reads as empty so score generation runs on
  fresh checkouts); the lock env file materializes the selected-platform
  entries as a per-run second `--env-file`; `compose_command` is the one
  docker compose invocation spelling; `down` never reads the selection and
  never resolves an include — a name-only root plus `--remove-orphans`
  removes every container the project ever created; a config-less or
  `services:`-less repo refuses loudly on both verbs.
- K: dev overlay — under `--dev`, an assembly-defined service with a genuine
  dev stage gets `image: {repository}/{service}:dev` plus the repo-root →
  `/app:ro` bind; dev-toggles pairs become `${NAME:-default}` environment
  holes (`.env` overrides apply only under `--dev`); overlay keys outside
  the selected local outputs are filtered; `--dev` with no dev shape
  refuses.

### Build

- K: `run_build` renders the deterministic per-run bake file (authored key
  order, byte-identical for identical configs; bake validates its own input
  — no document model); root/config injection defaults so `up --build`
  passes its already-loaded pair; a failed bake re-raises with the rendered
  content attached.
- K: push axis — `push=None` local `--load`; `push=""` pushes for the
  config's `repository`; `push=<addr>` re-roots tags and cache refs under
  that registry (one law for both destinations); an address with a scheme,
  trailing slash, or host carrying neither `.` nor `:` refuses; `--push`
  alone without a declared `repository` refuses; `--dev` with a push
  destination refuses (dev stages are local-only).
- K: `host_platform()` is the one host fact left — default targets are every
  service's default cell restricted to this machine's arch, and an empty
  default set refuses naming the arch and `--targets` (bake with no target
  arguments builds every service, silently defeating the selection);
  `--targets` addresses: bare name = default cell, `name/cell`, suffixed
  target name; unknown names/cells refuse listing the vocabulary.
- K: per target — dockerfile by convention; `target: dev` only under `--dev`;
  platforms collapse to the host arch in load mode (a single declared
  platform rides through); args: every `base_images` key as `${KEY:-}` on
  every service, the axis ARG at its cell value, `{SERVICE}_SHA` as
  `${…:?err}` for declaring Dockerfiles; tags `{namespace}/{target}:latest`
  and `:${SERVICE_SHA}` (dev builds tag `{namespace}/{service}:dev`, never
  SHA-tagged); `target:`-prefixed `additional_contexts`; registry cache refs
  under `{namespace}/build-cache:{target}` in push mode.
- K: the incremental lock write rides every building run (the `uv sync`
  shape — anyone who builds also locks), landing only after target selection
  and its refusals; bake reads the fresh entries as environment (index
  form); a building run with zero derived services refuses naming the lock
  verb.
- K: `metadata.json` is cwd-stable, unlinked before the run, and
  coverage-checked against the requested targets after (a target with no
  tagged references rejects; output not covering the targets refuses);
  local default-target builds run the daemon GC-limit check
  (`MIN_KEEP_STORAGE_GB` = 60; absent/null `defaultKeepStorage` refuses with
  a sample snippet, an explicit zero is a set value refusing below the
  floor; no readable daemon.json warns and continues).

### Score generation

- K: deterministic generation from `stack/score/` into `stack/generated/`;
  one compose rendering per local option and bare entry (output derives
  `stack/generated/compose/compose.{name}.yaml`; the compose state dir is
  re-created per entry); k8s is one monolithic rendering over
  `k8s.workloads` with `k8s.vars`, untouched by `BUNDLES=`; `--local` writes
  the gitignored local output with the local storage class; charts render
  through `helm template` against the lock's `{NAME}_CHART` entry (a chart
  without its lock entry refuses naming the lock verb; helm is a renderer,
  never a deployer); both tool state dirs are per-run temp — output
  reproduces from a fresh clone.
- K: substrate law — compose renderings consume the lock's platform
  manifests (the artifact records the exact bytes one machine runs); k8s
  renderings consume the index form (kubelet resolves per node).
- K: the pinned tools must already be on `PATH` at the exact pinned versions;
  generation refuses loudly listing them.
- K: `--check` is the CI staleness gate — self-skips (exit 0) where nothing
  generates; otherwise regenerates in-process and any `git status` change
  over the written outputs exits 1 printing the diff; `--check` and
  `--local` are mutually exclusive; the verb owns the pinned-tool fetch into
  a per-run temp bin prepended to `PATH`.
- K: post-passes on compose output — service-name collapse
  (`{workload}-{container}` → workload, collision loud); per-entry gate
  rename `wait-for-resources` → `wait-for-resources-{entry}` with
  `depends_on` following (collision loud — compose merges same-name services
  across includes silently); the edge-reserved check; bind inlining (every
  `type: bind` volume becomes a top-level `configs.content` entry — zero
  host-filesystem references in committed artifacts; the `$`-doubling law's
  single exception is `outernet.internal/consumer-interpolated` files).
- K: the image lock is required and non-empty; a gate-forming run
  additionally requires `ALPINE_IMAGE` (templates are data and cannot
  refuse).
- K: `$`-doubling law, stated once here — a literal `$` doubles whenever
  file text rides inside a compose document (edge Caddyfile, publish's
  file-sourced configs, score's bind inlining); text riding outside a
  compose document (k8s-side inclusion, k8s manifests) never doubles.
- K: `docker-build-matrix` cell schema (`targets`, `variant`) is a named
  contract with the binding — renaming a key breaks its fanout YAML; one
  partition per closure × variant cell.

### Provisioners and markers

- K: authored provisioner files carry `state`/`outputs`/`volumes`/`services`
  as real YAML mappings and `manifests` as a real list — score's string form
  is generated, never authored; a field already in string form is a loud
  validation error; `extra="forbid"`.
- K: compilation resolves include markers, then lock markers, both before
  validation; the compiled form lives in a per-run tempdir under `score_dir`
  and never touches the committed tree; `--provisioners` paths stay
  POSIX-relative; `declares_services` is the phantom-service condition the
  gate keys on; authored key order controls dump order at every level;
  comments are dropped; registration derives by resource-type closure in
  sorted-filename order (order-independence pinned by a test); orphan
  provisioner files silently no-ship.
- K: marker grammar — a single-key mapping (keeps authored sources
  plain-YAML-parseable); `include:` inserts bytes verbatim, `include-yaml:`
  inserts parsed structure; neither transforms content (`${VAR}` holes pass
  through; `$`-handling is per-consumer); paths anchor at the repo root —
  absolute paths, `..` escapes, missing files, and cycles are loud errors;
  text content is never scanned for markers; `lock: NAME` substitutes the
  full reference (the single digest source for k8s provisioners, which have
  no runtime env file); `lock-image: NAME` expands to
  registry/repository/tag/digest (grafana's `digest_field: sha` spelling); a
  hand-written literal `image:` value (no `${` hole, no `{{ }}` fill)
  refuses at lock resolution.

### Secrets

- K: the contract activates by `stack/secrets.yaml` presence —
  `KEY: description` or `KEY: {description, mint: true}` (string specs
  coerce; keys ALL_CAPS); each rendering's consumed keys validate as a
  subset (pair-variance is legitimate); the cluster Secret derives
  `{project_name}-secrets`; k8s consumption is `secretKeyRef` naming it
  (wholesale `envRef`s impose no per-key constraint); compose consumption is
  `${ALL_CAPS}` brace holes with odd `$`-parity (`$$`-escaped
  `configs.content` doesn't count); the mint list artifact
  (`stack/generated/secrets.txt`) generates from the declaration alone.

### Publish

- K: publishing happens iff local entries are declared — no flag, no
  section; artifact names derive from `repository`
  (`{repository}/{entry-or-option}`); `--plan` prints the entries as a bare
  JSON array (`[]` without local entries) with no login and no publish —
  the CI getter's derivation source; bake substitutes `${ALL_CAPS}` holes
  against `service_shas()` tree-tags plus the lock's index projection;
  unresolved vars stay holes in the artifact for the consumer's `.env`
  (non-empty stubs set only so publish's interpolation validation passes);
  authored `configs.content` rides verbatim (single-`$` holes are the
  include consumer's interpolation surface — `$`-doubling is score's
  concern, not publish's); image refs under `{repository}/` pin to
  `@sha256` via memoized `imagetools inspect`; each entry publishes once
  under the `<sha>` tag and aliases `<branch>` with `crane tag` (compose
  stamps wall-clock `created`, so the alias is the only way both tags name
  one digest); registry login reads `REGISTRY_USERNAME`/`REGISTRY_TOKEN`.

### Deployment-surface annotations

- K: everything the substrate needs that score's container model doesn't
  carry lives in the workload file as `outernet.internal/*` annotations —
  `docker-devkit.yaml` never carries a per-workload runtime fact; the
  shipped patch templates (package data under `src/docker_devkit/
  patch_templates/`, resolved via `importlib.resources`; a packaging test
  asserts every shipped template resolves) are the annotation reader — one
  annotation, one template, one behavior; consumer repos never author or
  declare one; refusal laws live in devkit code (templates are data and
  cannot refuse); score propagates `metadata.annotations` into both
  renderings, so adding or renaming an annotation changes the committed
  artifacts — regeneration diff expectations must include the annotation
  lines.
- K: the annotation table survives as a table, one row each — gpu (cuda/rocm
  pair registers per `GPU_VARIANT`; `gpu-pod.tpl` on every k8s init;
  nodeSelector `placeframe.io/gpu`; runtime vocabulary, distinct from the
  build-time variant axes), restart (compose enum, outside refuses),
  probe-adapter (`python-urllib`|`bash-http`; an httpGet probe without the
  annotation refuses loudly — score-compose silently drops httpGet),
  consumer-interpolated (declared `files:` names ride un-doubled; a name
  matching no declared file refuses), dev-toggles (consumed at `up --dev`;
  no template, no artifact change).
- K: `outernet.internal/publish` died — host publishes are `ports:` under
  `services:` now; a carrier refuses loudly at generation.

### Miscellaneous

- K: lifecycle stays a separate package from codegen (disjoint dependency
  surfaces — bundling would force stack-only consumers to install
  sqlacodegen, psycopg, datamodel-code-generator).
- K: `bashrun` pointer under See also; `is_mapping`/`is_list`,
  `EDGE_SERVICE_NAME`, `chart_lock_key` become map-line mentions (M).

## Drop list (derivable; removal loses nothing an agent needs)

- All mechanism narration inside module rows: constructor linearity essays,
  comprehension-shape descriptions, render-pipeline walkthroughs, the
  `ParsedDockerfile` construction-order narrative, the `Assembly` row's
  stage-by-stage prose, the score.py row's pass-by-pass prose. The code
  asserts these; overviews measurably don't help (arXiv:2602.11988).
- The three restatements of every core law — the near-verbatim duplicate
  pairs named in Orientation die; the inventory above names each law's
  single home.
- The Mirror-consumption section's consumer-workflow detail (checkout +
  setup-uv + ghcr login + permissions) — consumer-repo knowledge owned by
  github-actions-toolkit's AGENTS.md; keep only the verb's own contract.
- The release-flow prose beyond the two identity bullets; the `stack-toolkit`
  history sentence keeps its one identity line.
- House-style rhetoric: "stated once" metacommentary, em-dash asides,
  persuasive framing. The new file states laws; it doesn't argue them.

## Flag list (resolve before or during the rewrite)

1. "a CI invariant forbids built-image digests in the committed, reviewed
   lock" — no such invariant found in this repo's workflows, toolkit src, or
   placeframe's preflight. Likely resolution: the true law is already
   stronger and local — the lock is written only by `relock`/`write_lock`,
   which record only declared third-party declarations, so built-image
   digests are unrepresentable. Verify against `lock.py`; if that holds,
   drop the CI-invariant clause and state the local law.
2. Cross-repo drift, report-only: github-actions-toolkit's `AGENTS.md` still
   documents the getter's lock gate as `build --lock-only --check` — flags
   this repo's refactor removed (pinned by
   `test_build_no_longer_carries_the_lock_flags`). The fix is toolkit-side
   (`lock --check`); surface it to the operator, do not edit the other repo
   in this pass.
3. Pre-existing `ruff format` drift (11 files) — out of scope; reformatting
   is a separate, deliberate change.

## Out of scope

- `AGENTS-SHARED.md` (pulsar) owns the cross-repo doctrine; the self-rules
  embedded here are the repo-local enforcement. Upstreaming the budget
  concept to the shared file is a separate decision for the operator.
- No `design/` split, no new docs tree, no CLAUDE.md changes.
- The paused refactor resumes after this lands; nothing here touches `src/`
  or `tests/`.
