# Module-tiers refactor (single session)

Behavior-preserving restructure of docker-devkit's config and compose
surfaces, decided in the 2026-11-01 design session and executed in one
follow-up session. It lands the load state
`plan-artifact-identity-implementation.md` (Plan B) phase 8 builds against,
and states the tier law the module map follows from here on. The design is
closed; the executing session implements, it does not redecide.

## Standing constraints

- Behavior-preserving: every refusal message stays byte-identical, every
  artifact format and verb CLI contract unchanged. Test edits are import
  retargets and construction swaps only — never fixtures or expectations.
- The Dockerfile remains the sole build declaration; nothing moves into
  docker-devkit.yaml.
- One-way import law: `dockerfiles` imports nothing from `config`
  (config → dockerfiles). `config_lock` ← `config` stays one-way.
- One commit per step; the AGENTS.md lines a step invalidates update in
  that step's commit; prose and code never share a commit.
- Branch `refactor`, from a clean tree — the in-flight `AllCapsName`
  validator work in config.py lands first.

## End state

Three tiers, stated once in AGENTS.md and followed by the module map:
declarations (pure parsers of authored formats), resolution (reconcile
declarations with the repo — pure functions that refuse loudly and write
nothing), materialization (render per-run artifacts, invoke tools).

### dockerfiles.py (new) — the Dockerfile declaration parser

- `ServiceBuild` moves here unchanged (`variants` stays a load-time-frozen
  field beside `axis`); `bake.py` and tests retarget the import.
- `derive_services(root, axes) -> dict[str, ServiceBuild]` — service-dir
  discovery, the line scan, per-file refusals, fan assignment; constructs
  `ServiceBuild` records directly, no intermediate record.
  `DOCKERFILE_NAME`, the Dockerfile line patterns, and
  `_require_single_top` move here; config.py imports `_require_single_top`
  for `compute_service_shas`.
- `_effective_axis(name, builds, path) -> str | None` — the parent walk
  replacing memoized recursion: full cycle path, disagreeing parents, a
  child declaring an axis over parents; refusals and messages
  byte-identical to today's solver.
- Dies: `ParsedDockerfile`, `_parse_dockerfiles`, `_resolve_axis`, the
  call-site transcription loop, and the resolved-memo threading.

### config.py — schema plus a three-step load

- `read_config(root)` — strictyaml parse + `model_validate`.
- `resolve_config(root, config)` — version gate, `root` inject, `{uv}`/
  `{python}` template resolution, the `derive_services` call, the sha-arg
  computability check (`service_sha_env` stays in config.py), repository
  and lock pairing laws.
- `load_config(root)` — thin composer over the two; every verb's call site
  unchanged.
- `require_compose_stack` moves here from assembly.py.
- Unmoved: the schema models, `compute_service_shas`, `service_sha_env`,
  `mirrored_ref`, `parse_dev_toggles`, `is_mapping`/`is_list`.

### compose.py (assembly.py renamed and restructured)

Membership law: everything about the docker compose invocation — selection,
rendering, invocation spelling, staged inputs. Nothing else lands here.

- Kit unchanged: `merged_environment`, `compose_command`,
  `write_lock_env_file`, `LockEnvUnwind`, `keep_lock_env_file`,
  `write_temp_compose_file`, `require_env_file`.
- `select(config, environment) -> Selection` — pure: the `BUNDLES=` laws,
  include flattening, `FORWARDED_*` derivation, the output-service
  vocabulary and collision check, profiles, and dev facts (dev-stage
  services ∩ selection; dev-toggle parsing — the `WorkloadDocument` reads
  move up here). No files, no subprocess.
- `render(root, config, environment, selection, stack, dev) ->
  Invocation` — the per-run root compose file (project name, `include:`,
  edge stanza, port stanzas, embedded Caddyfile), the dev overlay,
  lock-env staging, and the `docker compose config` teeth.
- `Selection` (includes, routes, ports, auth_route, forwarded, profiles,
  output_services, dev_facts) and `Invocation` (compose_files,
  lock_env_file, profiles) are NamedTuples; the `Assembly` class dies — no
  methods, one consumer, three externally-read fields.
- `verbs/up.py` reads `select` → `render` → `compose_command` → handoff,
  consuming only `compose_files`, `lock_env_file`, `profiles`;
  `verbs/down.py` and `score.py` retarget kit imports here, with
  `require_compose_stack` from config.py.

## Steps

1. Extract dockerfiles.py: move `ServiceBuild`, `DOCKERFILE_NAME`, the line
   patterns, `_require_single_top`; write `derive_services` and
   `_effective_axis`; delete `ParsedDockerfile`/`_parse_dockerfiles`/
   `_resolve_axis` from config.py; `load_config` calls `derive_services`;
   retarget the `ServiceBuild` import sites (bake.py, tests). AGENTS.md
   module map gains the dockerfiles.py line; the service-derivation section
   anchors to it.
   Verify: `uv run pytest tests/test_service_builds.py` green, untouched.
2. Split `read_config`/`resolve_config`/`load_config`; move
   `require_compose_stack` in. AGENTS.md load-refusal wording follows.
   Verify: `uv run pytest tests/test_config.py` green.
3. Rename assembly.py → compose.py; unroll the constructor into
   `select`/`render` with `Selection`/`Invocation`; move dev-toggle parsing
   into `select`; retarget up.py/down.py/score.py. AGENTS.md: module map
   line for compose.py plus the membership law; "`select()` owns the law"
   replaces "the `Assembly` constructor owns the law"; the
   `merged_environment` home updates.
   Verify: `uv run pytest tests/test_assembly.py tests/test_compose.py`
   green after mechanical retargets (monkeypatch modules, construction
   swaps).
4. Sibling plan anchors, one prose commit: plan-consumer-contract.md
   (`service_builds.py` → `dockerfiles.py`, `assembly.py` → `compose.py`,
   the one-way law spelling); grep plan-apt-hermeticity.md — it cites only
   the `derive_services` name, which is unchanged.

## Verify

`uv run --locked scripts/preflight.py` (the full gate) green at the end of
the session; the per-step batteries above green between steps.

## Out of scope — absorbed by Plan B phase 8

- The bare-identifier `COPY --from=` hole: naming a sibling service with no
  corresponding `FROM` edge passes silently today (load accepts what bake
  then breaks on).
- The independent-axis refusal message skew: fires for any parented
  service, text says "variant-bearing base".
- Renaming `derive_services` — Plan B phases 8/9 and plan-apt-hermeticity
  anchor the name.
