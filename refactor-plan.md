# Refactor plan — src/ consolidation

Executes the settled audit (this file replaces `audit.md`; all ten judgement
calls were decided in that session and are binding below, not suggestions).
Six modules dissolve, no new module is created; the package root goes from 16
modules to 10. Work phase by phase — each phase is sized for one agent
session; if the context window fills, stop at a phase boundary and hand off
to the next session with this file. Never merge commits across phases.
Yield between phases: once a phase's final commit lands and the verification
trio passes, tick the phase in Progress below (its own prose commit), report,
and stop — the next phase starts only on the operator's go-ahead, never on
the agent's own momentum.

## Progress

Tick a phase only after its final commit and a green verification trio;
`git log` carries the commit-level detail.

- [x] Phase 0 — AGENTS.md to target shape
- [x] Phase 1 — score.py folds
- [x] Phase 2 — service_builds → config
- [x] Phase 3 — image_refs → lock + 2.3
- [x] Phase 4 — daemon_gc → bake
- [ ] Phase 5 — mirror flatten, 2.6
- [ ] Phase 6 — dedup: record members + lock/assembly/verbs
- [ ] Phase 7 — dedup: score/publish/config + 2.5 + 2.7
- [ ] Phase 8 — inline wave + 2.9
- [ ] Phase 9 — dead code + validation timing
- [ ] Phase 10 — ordering, naming, micro

The import graph after all folds is verified cycle-free: `service_builds.py`,
`image_refs.py`, `markers.py` have zero internal imports today;
`provisioners.py` imports only `markers.py` (same fold destination);
`secrets.py` imports only config predicates; `daemon_gc.py` imports nothing
internal. No fold creates a new module edge.

## Ground rules (bind every phase)

- Axioms: never split into more functions/files; consolidation may lengthen
  files; single-src-call helpers inline; blast radius never demotes a change.
- Spec-first: Phase 0 rewrites AGENTS.md to the target shape first, as a
  prose commit. Every later code commit implements a spec already written. If
  an implementation must deviate, update AGENTS.md in its own prose commit
  before deviating.
- Prose and code never share a commit. Subjects: one line, under 72 chars,
  no body, no trailers.
- Verify after every commit: `uv run pytest`, `uv run ruff check`,
  `uv run basedpyright`.
- Repo conventions (AGENTS-SHARED.md) bind: no comments unless short and
  factual, no docstrings, callers-before-callees, classes at top, no
  closures, no inline imports, bashrun not subprocess, no new shared helpers
  beyond the ones this plan names.
- Refuted audit claims — do not act: `PYPROJECT_PATH`/`PYTHON_VERSION_PATH`
  are 3-reference each (confined to one function); they stay.

## Decided judgement calls (binding)

| # | Decision | Phase |
|---|---|---|
| 2.1 | Lock read projections stay in `lock.py` | — |
| 2.2 | `merged_environment` stays in `assembly.py` | — |
| 2.3 | `select_platform_manifests` inlines into `resolve_remote_manifests` | 3 |
| 2.4 | score.py's four state-lens classes stay | — |
| 2.5 | `WorkloadDocument` dev-toggles member in documents.py | 7 |
| 2.6 | `MirrorConfig` flattens to root `mirror_prefix` | 5 |
| 2.7 | `PublishEntry` deleted; verb loops over bundles | 7 |
| 2.8 | crane/score-tool installers stay separate | — |
| 2.9 | Dev-overlay silent skip branches dropped | 8 |
| 2.10 | `$`-doubling stays spelled three times; law stated in AGENTS.md | 0 |

Session stance behind these: the record law ("every derivation over the
declared world rides the record") is a transitional organizing principle;
nothing in this plan is justified by record-law arguments alone.

## Target module map

| File | Fate |
|---|---|
| `config.py` | keeps the schema + record; absorbs `service_builds.py` whole; keeps `ImageLock` + lock reads; `mirror:` section flattens to root `mirror_prefix`; `lock_entries()` renames to `image_lock()` |
| `lock.py` | keeps the relock engine and the read projections (`env_entries`, `compose_env_entries`, `render_env_entries`); absorbs `image_refs.py` whole (remote resolution) |
| `score.py` | the rendering library; absorbs `secrets.py`, `provisioners.py`, `markers.py` whole; sheds `ensure_score_tools` to its verb; keeps the state-lens classes |
| `assembly.py` | the `Assembly` record with a linear constructor (renders inlined); keeps `.env` reading (`merged_environment`) |
| `bake.py` | `run_build` + metadata lens; absorbs `daemon_gc.py` whole |
| `documents.py` | keeps; gains the dev-toggles `WorkloadDocument` member |
| `publish.py` | keeps the bake/finalize library; loses `PublishEntry` |
| `crane.py`, `optional_value_option.py` | keep unchanged |
| `secrets.py`, `service_builds.py`, `provisioners.py`, `markers.py`, `image_refs.py`, `daemon_gc.py` | deleted |

## Do not touch (verified non-findings — do not re-audit)

- `documents.py` earns its file: `ComposeDocument` has two src consumers
  (score.py:239, publish.py:52), `WorkloadDocument` two modules (score.py,
  assembly.py). The publish-only `.configs` half of the lens stays.
- `crane.py` earns its file: two consumers (mirror, publish), 17 lines,
  single home of the version pin; the x86_64-only archive spelling is the CI
  runner reality.
- `optional_value_option.py` stays: pinned typer 0.27.3 vendors click without
  `flag_value`; the module is the sanctioned `# noqa-wrapper:` boundary.
  (Optional cosmetic only if convenient: the generic `Destination*` names
  advertise a generality that doesn't exist.)
- The verb preamble (`root = Path.cwd(); config = load_config(root)` plus
  divergent None-handling, ×10 verbs) is irreducible; each verb's
  None-behavior is documented intent.
- `up --build`'s `repository or base_images` guard is live (a services-only
  repo legally loads) — unlike publish's dead twin (Phase 9).
- The "unknown names; the vocabulary is …" refusal shape ×5 and the
  `app = typer.Typer(...)` line ×10 are message/CLI style laws.
- `up`/`down` tests drive only the CLIs plus one direct `parse_env_file`
  call; keep it that way while inlining.

## Do not inline (single-src-call non-candidates)

The record members (`select_platform`, `render_bake`, `build_partitions`,
`finalize`, `generate_compose`, `generate_k8s`) — documented derivation
surface with test-pinned signatures. `host_platform` (13 test references).
`_committed_lock`/`_declaration_frozen` (try-block / guard-predicate
exemptions). `parse_env_file` inlines into `merged_environment` (Phase 8,
assembly-local) — its external-contract line is dead per the phase-9 wave.

---

# Phase 0 — AGENTS.md to target shape (prose, one commit)

Rewrite AGENTS.md's Shape section to describe the end state this plan builds.
It will temporarily disagree with the code — that is the spec-first pattern.
Contents of the rewrite:

- Delete rows: `secrets.py`, `service_builds.py`, `provisioners.py`,
  `markers.py`, `image_refs.py`, `daemon_gc.py`. Fold each row's laws into
  the destination row (provisioners/markers/secrets law → `score.py` row;
  service_builds law, including the `{SERVICE}_SHA` naming and the git
  hashing machinery, → `config.py` row; remote-resolution law → `lock.py`
  row; the daemon lens → `bake.py` row; fix the `documents.py` row's "the
  daemon lens in daemon_gc.py" phrase). Delete the service_builds one-way-law
  sentence.
- `config.py` row: `mirror:` section becomes root `mirror_prefix` (optional;
  absent ⇒ `DEFAULT_MIRROR_PREFIX`); `lock_entries()` renamed `image_lock()`;
  `ensure_score_tools` removed from the row (moves to the `verbs/score.py`
  row); `SECRETS_FILE`/`SECRETS_OUTPUT` move out of the row into `score.py`'s.
- `lock.py` row: names the folded remote resolution (`resolve_remote_digest`,
  `resolve_remote_manifests`, `UpstreamManifests`) and states that the read
  projections are authored placement (2.1).
- `score.py` row: absorbs the secrets contract, provisioner authoring format,
  and marker resolvers whole; keeps the state-lens classes (2.4).
- `assembly.py` row: `merged_environment` placement stated as authored (2.2);
  constructor described as one linear resolve→render→validate body;
  `keep_lock_env_file` named as the shared failure dance.
- `documents.py` row: gains the dev-toggles member (2.5) — returns the raw
  annotation value + service name; consumers own refuse/skip policy.
- `verbs/score.py` row: owns the pinned-tool fetch (inlined `ensure_score_tools`)
  and the `git status` over `generate_artifacts`' returned paths.
- `publish.py` row: `PublishEntry` gone; `bake_sources` singular.
- State the `$`-doubling law once (2.10): literal `$` doubles whenever file
  text rides inside a compose document (edge Caddyfile, publish's file-sourced
  configs, score's bind inlining); the single exception is
  `outernet.internal/consumer-interpolated` files at score, which ride
  un-doubled.

Commit: `Rewrite AGENTS.md to the refactor target shape`.

# Phase 1 — score.py folds (two commits)

**Commit 1 — secrets fold (1.1.1).** Delete `secrets.py`; fold into
`score.py`: `SecretsDeclaration` (consumed at score.py:502),
`collect_secret_keys` (:520), `validate_consumed` (:514/:521). Move
`SECRETS_FILE`/`SECRETS_OUTPUT` from config.py:56-57 into score.py;
`verbs/score.py` imports follow. During the move: fix the stale `k8s_secret`
comment (secrets.py:17-18); delete `DECLARED_KEY` (byte-identical to
`config.ENV_NAME_PATTERN`; score imports `ENV_NAME_PATTERN` instead — the
1.5 dedup dissolves here). Tests: `test_secrets.py` import paths repoint (or
fold its four tests into `test_score_generate.py`).
Commit: `Fold secrets.py into score.py`.

**Commit 2 — provisioners + markers fold (1.1.2 + 1.1.3).** Delete both;
fold into `score.py`. Symbols: `compiled_provisioners` (score.py:164, :410),
`dump_compose` (:268, :449), `STRING_FORM_FIELDS`/`ComposeDumper`/
`AuthoredProvisioner`/`CompiledProvisioners`, and the marker resolvers
(`resolve_includes`, `resolve_lock_refs`, `contains_marker`; consumers at
score.py:444-445, :579). Markers' only internal import is config's
`is_mapping`/`is_list`, which score.py already imports — no new edge.
Fold-local inlines: markers' `_image_spec` (:97) and `_refuse_literal_images`
(:120 — the law text lives in the refusal message, which survives);
provisioners' `AuthoredProvisioners` (:104) inlines as a shape check plus a
per-entry `AuthoredProvisioner.model_validate` loop (two test assertions
matching pydantic's stock list message change with it). Place moved code
per classes-at-top / callers-before-callees. Tests: `test_provisioners.py`
and `test_markers.py:8` import paths.
Commit: `Fold provisioners and markers into score.py`.

# Phase 2 — service_builds → config (one commit)

Delete `service_builds.py`; fold into `config.py` (1.1.4). The module has
zero `docker_devkit` imports — the fold cannot cycle. Symbols move as
top-level names: `derive_services`, `compute_service_shas`,
`service_sha_env`, `ServiceBuild`, `ParsedDockerfile`, `require_single_top`
(underscore it), and the five Dockerfile regexes. Put `ServiceBuild`/
`ParsedDockerfile` into the class block at the top.

Critical seam: the 22 test sites that monkeypatch
`config_module.compute_service_shas` keep working verbatim — the name stays a
config-module global, late-bound. `service_shas()` keeps delegating to the
module-global `compute_service_shas` by name; do not inline the member body.
`test_service_builds.py` retargets its imports and its
`service_builds.bash`/`bash_output` patches to the config module.

Fold-local dead-code collapses (1.2):
- Dead guards in `compute_service_shas` (service_builds.py:222-224, :239,
  :244) — the early return at :215-218 guarantees non-empty `service_dirs`;
  collapse to `require_single_top(tops, "services")` then unconditional
  `workload_prefix = f"{next(iter(tops))}/"`.
- `ServiceBuild.targets` (service_builds.py:47-49) — zero src references, 6
  test sites; delete and rewrite the tests against `target_variants`.

Commit: `Fold service_builds.py into config.py`.

# Phase 3 — image_refs → lock + 2.3 (one commit)

Delete `image_refs.py`; fold into `lock.py` (1.1.5). Symbols:
`resolve_remote_digest` (callers lock.py:65, publish.py:80 — publish switches
its import to lock, direction already established),
`resolve_remote_manifests` (lock.py:48), `UpstreamManifests` (test-imported —
tests import it from lock after the fold).

2.3 (binding): `select_platform_manifests` inlines into
`resolve_remote_manifests` — the four laws (attestation skip, exact os/arch
match, multiple-match refusal, no-match listing with available platforms)
survive as logic. The five direct tests (test_image_refs.py:61-96) rewrite
through the `bash_output` seam (fabricate raw inspect output).

Fold-local cleanups: `_is_attestation` (:54) and its two single-use constants
inline during the fold; unify the `TypeError` at image_refs.py:101 to
`RuntimeError` (sibling branch spelling; no test pins the TypeError).
Digest-grammar dedup (1.5): one shared `sha256:[a-f0-9]{64}` fragment
constant in config.py, interpolated into config.py:37-39, config.py:67, and
lock.py:24 (`DIGEST_TAIL_PATTERN`).

Reorder lock.py wholesale while editing (1.7): relock/render_lock above their
callers; callers-before-callees throughout.

Tests: `test_lock.py` zero churn (its monkeypatches already target
`docker_devkit.lock.` names); `test_build_docker.py` one import line;
`test_publish_compose.py` zero (patches the publish module's name);
`test_image_refs.py` folds into `test_lock.py` with retargeted patches.
Commit: `Fold image_refs.py into lock.py and inline platform selection`.

# Phase 4 — daemon_gc → bake (one commit)

Delete `daemon_gc.py`; fold into `bake.py` (1.1.6). `check_gc_limits` (its
`candidates` test seam preserved) and `MIN_KEEP_STORAGE_GB` move; the three
lens classes join the class block at the top of bake.py. The 9
`bake_module.check_gc_limits` monkeypatches in `test_build_docker.py` are
untouched; `test_daemon_gc.py` retargets imports.

Fold-local cleanups: inline `_KEEP_STORAGE_PATTERN`/`_GB_MULTIPLIERS`
(single-reference); drop the redundant `path.exists()` conjunct at
daemon_gc.py:57 (`os.access(path, os.R_OK)` is already False for missing
paths); unify the `ValueError`/`RuntimeError` inconsistency (:68 vs :72) on
`RuntimeError` and adjust the two pinned test expectations.
Commit: `Fold daemon_gc.py into bake.py`.

# Phase 5 — mirror flatten, 2.6 (one commit)

Delete `MirrorConfig` (config.py:113-123); root field
`mirror_prefix: str | None = None` carries the same validator (non-empty, no
trailing slash). Add the `mirror_prefix` record member (1.5):
`self.mirror_prefix if self.mirror_prefix is not None else
DEFAULT_MIRROR_PREFIX`; both consumers switch to it (lock.py:35,
verbs/mirror.py:25).

Tests: config/mirror tests update the YAML spelling (`mirror: {prefix: …}` →
`mirror_prefix: …`). The five consumer repos' config edits + relocks are
phase-9-wave scope in those repos, deliberately out of this plan; old-spelling
configs refuse loudly at load (extra="forbid") — the breaking change rides
the patch flow pre-1.0.
Commit: `Flatten the mirror section to a root mirror_prefix field`.

# Phase 6 — dedup: record members + lock/assembly/verbs (one commit)

- `declared_charts` member (1.5): `config.k8s.charts if config.k8s is not
  None else {}` appears at lock.py:55, lock.py:153, config.py:700,
  verbs/mirror.py:33 — one member, four consumers.
- `ImageLockEntry` recorded-digest projection (1.5): `[entry.index,
  *entry.platforms.values()]` at verbs/up.py:90 and verbs/mirror.py:31 — a
  member on `ImageLockEntry` beside `require`; mirror keeps its local
  `None → []` guard.
- Keep-lock-env failure dance (1.5): print-path-and-content, null the
  reference, unlink the rest — assembly.py:162-166, verbs/up.py:50-55,
  verbs/down.py:42-49. An `Assembly.keep_lock_env_file` method consumed by
  the constructor's `except` and by `up` (which kills up.py:52's reach-in
  mutation `assembly.lock_env_file = None`); `down`, which has no `Assembly`,
  keeps its local two-liner against the free function.
- Lenient cutover-lock read (1.5): the same `except (TypeError, ValueError)`
  fallback exists as config.py:706-712 (finalize's CADDY check) and
  lock.py:179-190 (`_committed_lock`). One spelling in config.py (lock
  imports config, not the reverse); `_committed_lock` delegates, each caller
  keeps its distinct fallback action.
- `run_lock` write lane (1.5): the write lane re-implements `write_lock`
  (lock.py:158-176 vs :134-138). Compute the rendered text only in the
  `--check` lane (it alone needs the diff); the write lane becomes
  `write_lock(config, upgrade=upgrade)`. In the same edit drop `run_lock`'s
  `root`/`config` injection parameters (1.2 — sole caller verbs/lock.py:23
  never supplies them); open the body with `config = load_config(Path.cwd())`.
- `compose_bundles()` derived twice at call sites (1.5): verbs/score.py:39+62
  and verbs/publish.py:48+57 — hoist one local.
Commit: `Dedup shared members and the lock write lane`.

# Phase 7 — dedup: score/publish/config + 2.5 + 2.7 (three commits)

**Commit 1 — mechanical dedup.**
- finditer+parts/cursor loops → `pattern.sub` callbacks (1.5): publish.py:74-83
  (`finalize_baked`), score.py:581-594 (`_rendered_workloads`), config.py:780-787
  (template substitution) — the compact form publish.py:33-35 already shows.
  (The score.py callback raises `SystemExit` on a missing value — raising
  from a `re.sub` callback is fine.)
- `STATE_PREFIX` vs the bare `".score-compose"` literal (score.py:46 vs :151)
  — derive both from one constant.
- score.py:447 recomputes `config.chart_values_path(chart_name)` that :439
  already holds in `path` — reuse via `values_dir / path.relative_to(root)`.
- assembly.py derives the auth selection twice (:101 and :148-152) — derive
  `auth_option = choices.get(AUTH_GROUP_NAME)` once.
- assembly.py:283 re-derives `score_dir_path()` by hand — use the member.
Commit: `Fold substitution loops into pattern.sub callbacks`.

**Commit 2 — publish reshape (2.7).** Delete `PublishEntry` (publish.py:16-18)
— a pure 1:1 re-wrap of the bundle's `(name, output)`. verbs/publish.py:67-69
loops over the filtered `compose_bundles()` list directly; `bake_sources`
takes a single source path and returns a single baked path; the
unresolved-holes scan and `-f` join drop their loops; `finalize_baked` takes
the single baked path. `PublishedEntry` stays (computed facts, feeds the
summary). Test: test_publish_compose.py:132 `bake_sources` call signature.
Commit: `Delete PublishEntry and singularize the publish path`.

**Commit 3 — dev-toggles member (2.5).** A `WorkloadDocument` member in
documents.py returns the raw `DEV_TOGGLES_ANNOTATION` value + service name;
score.py's `_validate_dev_toggles` (:342-357) and assembly's dev overlay
(:280-293) consume it, each keeping its own refuse/skip policy.
`parse_dev_toggles` stays in config (11 references).
Commit: `Add the dev-toggles annotation member to WorkloadDocument`.

# Phase 8 — inline wave + 2.9 (two commits)

**Commit 1 — assembly + up + bake.**
- up.py `_bring_up` (:49) becomes `up`'s body.
- assembly.py: `_materialize_and_validate` (:161), `_render_root_document`
  (:169), `_render_caddyfile` (:236), `_render_dev_overlay` (:171) inline —
  the constructor becomes one linear resolve→render→validate body;
  `forwarded_pair` (:101), `assert_known_profiles` (:192), `_service_names`
  (:134) inline; `parse_env_file` inlines into `merged_environment`.
- 2.9 (binding): in the dev overlay, drop the `not isinstance(raw, str)` and
  `not service_name` skip branches (generation-refused shapes; reachable only
  via hand-edited committed artifacts, where drift should surface loudly);
  the absent-annotation and `not in declared` skips stay (the latter
  deliberate and test-pinned).
- bake.py: `_resolve_target` (:125), `_run_bake` (:168 — it owns a `try` but
  `run_build` has none, so no nesting).
Commit: `Inline the assembly and verb single-call helpers`.

**Commit 2 — config/score/lock + 1.4.**
- config.py: `_any_local_entries` (:656 — inline as a plain loop, killing the
  double-`any` expression); `_uv_version_pin`/`_python_version_pin`
  (:771/:773); `_resolve_base_image_templates` (:690).
- score.py: `_validate_workload_surface` (:159) and `_validate_dev_toggles`
  (:160) inline — collapsing the double `WorkloadDocument.model_validate` of
  the same document (:286, :345); `enforce_secret_contract` (:139) inline —
  the whole contract (declaration model, collector, enforcement, mint list)
  reads in one place post-fold.
- lock.py: `entry_for` (:113) inlines into `env_entries` (the lacks-name tail
  at :104-106 is unreachable from src since `env_entries` iterates the lock's
  own keys; the platform-refusal branch is load-bearing and stays); the five
  direct `entry_for` tests (test_lock.py:539-562) rewrite against
  `env_entries`.
- 1.4: `ensure_score_tools` (score.py:532-538) relocates into verbs/score.py's
  `--check` lane `TemporaryDirectory` block (single caller verbs/score.py:59);
  the dead `target="both"` default dies.
Commit: `Inline the config, score, and lock single-call helpers`.

# Phase 9 — dead code + validation timing (one commit)

Dead code (1.2 remainder):
- `BakedImage.digest`/`ref` (bake.py:28-29, :40) — populated, never read;
  delete both; the validator tail simplifies.
- `K8sConfig.rendering` (config.py:241-243) — one production reference (its
  own validator, :266) plus one test assert; inline `if not (workloads or
  charts)` into `_require_a_rendering`; adjust the test.
- verbs/publish.py:75-76 dead guard — unreachable through `load_config`
  (test_publish_compose.py:108-115 pins the load-time refusal); replace with
  the repo's narrowing idiom (`assert config.repository is not None`) or
  delete.
- config.py:374 redundant conjunct — `axis is not None and variant is not
  None` tests one fact twice; keep `if variant is not None:`.

Validation timing and guards (1.6):
- down.py writes a temp file before a step that can fail, outside its `try`
  (:33-34 vs :40) — `write_lock_env_file` can refuse and strand the name-only
  yaml in `/tmp`. Swap the two writes (the `Assembly` constructor already
  orders this correctly: assembly.py:158 before :169).
- `bake.run_build` writes the lock before refusing bad input (write_lock at
  :102; refusals at :117, :125-131 fire after) — hoist target selection above
  `write_lock` so failing invocations leave the tree untouched.
- `tree_tags` computes the git tree-SHA machinery before argument validation
  (verbs/tree_tags.py:27 vs :29-33) — validate names first, compute `shas`
  just before the print loop.
- `--check` output-set drift hazard (verbs/score.py:62-66 re-derives what
  `generate_artifacts` derives internally at score.py:128-139): have
  `generate_artifacts` return the list of paths it wrote (currently returns
  `None`; additive, behavior-preserving) and the check lane `git status` over
  that.
Commit: `Fix dead code and validation-ordering guards`.

# Phase 10 — ordering, naming, micro (one commit)

- config.py classes-at-top / entry-point placement (1.7): `ImageLockEntry`
  (:828), `ImageLock` (:850), `ComposeBundle` (:890), `ToolRequirement`
  (:897) move above functions; `load_config` (:961) moves near the top as the
  entry point. Mechanical — cross-references are runtime lookups.
- Callers-before-callees swap in assembly.py: `_compose_config` (:375) above
  `compose_command` (:337).
- `ImageLock()` over `ImageLock.model_validate({})` (lock.py:185/:190,
  config.py:307).
- `lock_entries()` → `image_lock()` rename (config.py:303; ~6 callers).
- bake.py:78 `host` shadowing — rename the registry host to `registry_host`.
- score.py:391 `output_path = output` — delete the alias, rename the
  parameter.
- verbs/build.py:33 — comprehension variable `service` mislabels target
  addresses; rename to `address`.
- Single-reference module constants inline at their one use (1.7) — config.py:
  `SCORE_DIR`, `K8S_OUTPUT`, `K8S_LOCAL_OUTPUT`, `SCORE_COMPOSE_VERSION`,
  `SCORE_K8S_VERSION`, `HELM_VERSION`, `BUNDLE_OUTPUT_DIR`, `PLAIN_VARIANT`,
  `VARIANT_VALUE_PATTERN`, `UV_EXACT_PIN_PATTERN`, `ROUTE_PATH_PATTERN`,
  `ROUTE_UPSTREAM_PATTERN`, `REMOTE_REF_PATTERN`; assembly.py:
  `PROFILES_VARIABLE`, `DEV_MOUNT_TARGET`, `EDGE_CONFIG_TARGET`,
  `CADDY_IMAGE_HOLE`; up.py: `_MANIFEST_ABSENT_PATTERN`. Multi-reference
  constants stay. Verb single-use locals: `partitions` (verbs/matrix.py:16),
  `entries` (verbs/lock_env.py:24), `names` (verbs/publish.py:64 — if Phase 7
  left it), `selected` (bake.py:138), the `artifact` aliases (score.py:510/517).
- verbs/build.py:28 `no_cache` — convert from the default-value
  `typer.Option` form to `Annotated` (B008-avoidance convention).
Commit: `Reorder config and apply naming and micro cleanups`.

# Completion

After Phase 10: delete this file (its work is done; `git log` is the audit
trail), run the full verification trio once more, and confirm the tree holds
exactly ten package-root modules. AGENTS.md already describes the shipped
shape from Phase 0 — reconcile any drift in a prose commit.
