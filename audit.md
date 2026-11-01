# Refactor audit — src/ code quality

Method: three waves — six per-file audit agents over balanced file groups, one
cross-file duplication sweep, one mechanical dead-code/single-reference sweep —
followed by direct verification of every load-bearing claim against the source.
All reference counts were grep-verified across `src/` and `tests/` separately;
test-only usage is called out wherever it applies. The audit honors the
session's axioms: never split into more functions/files; consolidation may
lengthen files; inlining single-call helpers and single-reference variables is
wanted; blast radius is never a reason to demote a change.

Two agent claims were refuted during verification and are excluded:
`PYPROJECT_PATH`/`PYTHON_VERSION_PATH` are 3-reference each (confined to one
function, not single-reference); they stay.

Every item below that moves or deletes code carries a spec-first AGENTS.md
obligation: update the affected module-table rows (and the Shape-section laws
for the service_builds fold and the lock-read relocation) in a prose commit
before or with the code change. Prose and code never share a commit.

## Target module map (after Section 1)

| File | Fate |
|---|---|
| `config.py` | keeps the schema + record; absorbs `service_builds.py` whole; keeps `ImageLock` + lock reads |
| `lock.py` | keeps the relock engine; absorbs `image_refs.py` whole (remote resolution); keeps the read projections (2.1 decided: stay) |
| `score.py` | the rendering library; absorbs `secrets.py`, `provisioners.py`, `markers.py` whole; sheds `ensure_score_tools` to its verb |
| `assembly.py` | the `Assembly` record, consolidated (renders inlined into the constructor); `.env` reading possibly to config (judgement 2) |
| `bake.py` | `run_build` + metadata lens; absorbs `daemon_gc.py` whole |
| `documents.py` | keeps — both lenses are genuinely two-consumer (verified) |
| `crane.py`, `optional_value_option.py` | keep — justified (see Section 3) |
| `secrets.py`, `service_builds.py`, `provisioners.py`, `markers.py`, `image_refs.py`, `daemon_gc.py` | deleted |

Import graph after the folds (verified cycle-free): `service_builds.py` and
`image_refs.py` and `markers.py` have zero internal imports today;
`provisioners.py` imports only `markers.py` (which folds into the same
destination); `secrets.py` imports only config predicates; `daemon_gc.py`
imports nothing internal. No new module edge is created by any deletion.

---

# Section 1 — Obviously correct

## 1.1 File dissolutions

**1.1.1 Delete `secrets.py`; fold into `score.py`.** Its sole production
consumer is `score.py` (`SecretsDeclaration` at score.py:502,
`collect_secret_keys` at :520, `validate_consumed` via the declaration at
:514/:521); `SECRETS_FILE`/`SECRETS_OUTPUT` already live in score-land's
vocabulary but sit in config.py (:56-57) where nothing else uses them — move
them to score.py with the fold (verbs/score.py imports follow). The file's own
header comment apologizes for the split ("the declaration model lives here and
its enforcement stage in score"). Tests: `tests/test_secrets.py` import path
repoints (or the four tests fold into `test_score_generate.py`). Fix the stale
comment naming nonexistent `k8s_secret` (secrets.py:17-18) during the move.

**1.1.2 Delete `provisioners.py`; fold into `score.py`.** Every symbol's sole
production consumer is score.py: `compiled_provisioners` (:164, :410),
`dump_compose` (:268, :449 plus internal), `STRING_FORM_FIELDS`/`ComposeDumper`/
`AuthoredProvisioner`/`CompiledProvisioners` internal. Import deltas are all
stdlib/pydantic names score.py lacks. Tests: `test_provisioners.py` import
paths.

**1.1.3 Delete `markers.py`; fold into `score.py` together with 1.1.2.** After
the provisioners fold, all three documented consumers live in score.py
(provisioner compile pre-pass, chart-values pre-pass :444-445, workload
rendering pre-pass :579). Its only internal import is config's
`is_mapping`/`is_list`, which score.py already imports — no new edge. Tests:
`test_markers.py:8` import path.

**1.1.4 Delete `service_builds.py`; fold into `config.py`.** The module has
zero `docker_devkit` imports (verified, lines 1-10) — the fold cannot cycle.
The documented "one-way law" (AGENTS.md Shape section) is an artifact, not a
load-bearing constraint: nothing needs it (the root-injection pattern lives in
the `root` field, not the module split), while the Shape law itself ("every
derivation over the declared world rides the record as a member function")
makes config.py the lawful home. Symbols: `derive_services`,
`compute_service_shas`, `service_sha_env`, `ServiceBuild`, `ParsedDockerfile`,
`require_single_top` (underscore it), and the five Dockerfile regexes move as
top-level names in config.py. Critically, the 22 test sites that monkeypatch
`config_module.compute_service_shas` keep working verbatim (the name stays a
config-module global, late-bound); `test_service_builds.py` retargets its
imports and its `service_builds.bash`/`bash_output` patches to the config
module. `service_shas()` keeps delegating to the module-global
`compute_service_shas` by name — do not inline the member body, the seam
depends on the global lookup. AGENTS.md: delete the one-way-law sentence and
the `service_builds.py` row; fold its laws into the config.py row.

**1.1.5 Delete `image_refs.py`; fold into `lock.py`.** `resolve_remote_manifests`
has exactly one src caller (lock.py:48); `resolve_remote_digest` has two (lock.py:65,
publish.py:80) and `verbs/publish.py` already imports from lock, so the
direction is established; `select_platform_manifests` is internal-plus-tests;
`UpstreamManifests` is test-imported. Test churn: `test_lock.py` zero (its
monkeypatches already target `docker_devkit.lock.` names), `test_build_docker.py`
one import line, `test_publish_compose.py` zero (patches the publish module's
name), `test_image_refs.py` folds into `test_lock.py` with retargeted patches.
`publish.py` switches its `resolve_remote_digest` import to lock. Also during
the move: unify the `TypeError` at image_refs.py:101 to `RuntimeError`
(sibling branch spells it that way; no test pins the TypeError).

**1.1.6 Delete `daemon_gc.py`; fold into `bake.py`.** Single src consumer
(bake.py:98). `check_gc_limits` (with its `candidates` test seam) and
`MIN_KEEP_STORAGE_GB` move; the three lens classes join the class block at the
top of bake.py; the 9 `bake_module.check_gc_limits` monkeypatches in
`test_build_docker.py` are untouched; `test_daemon_gc.py` retargets imports.
Inline `_KEEP_STORAGE_PATTERN`/`_GB_MULTIPLIERS` (single-reference) during the
move; drop the redundant `path.exists()` conjunct at daemon_gc.py:57
(`os.access(path, os.R_OK)` is already False for missing paths); unify the
`ValueError`/`RuntimeError` inconsistency (:68 vs :72) on `RuntimeError` and
adjust the two pinned test expectations. AGENTS.md: delete the row, fix the
`documents.py` row's "the daemon lens in daemon_gc.py" phrase.

## 1.2 Dead code

- **`BakedImage.digest` and `BakedImage.ref`** (bake.py:28-29, 40) — populated,
  never read anywhere in src or tests. Delete both fields; the validator tail
  simplifies.
- **`ServiceBuild.targets`** (service_builds.py:47-49) — zero src references;
  6 test sites use it. A one-comprehension projection of `target_variants`.
  Delete; rewrite the tests against `target_variants`.
- **`K8sConfig.rendering`** (config.py:241-243) — one production reference
  (its own validator, :266) plus one test assert. Inline `if not (workloads or
  charts)` into `_require_a_rendering`; adjust the test.
- **`run_lock`'s `root`/`config` injection parameters** (lock.py:141-149) — the
  sole caller (verbs/lock.py:23) never supplies them; tests go through the
  CLI. Drop both; open the body with `config = load_config(Path.cwd())`.
- **`ensure_score_tools`'s `target="both"` default** (score.py:532) — the only
  caller passes `target` explicitly. Dies with the relocation in 1.4.
- **Dead guards in `compute_service_shas`** (service_builds.py:222-224, 239,
  244) — the early return at :215-218 guarantees non-empty `service_dirs`, so
  `if PurePosixPath(service_dir).parts`, the `if tops else None`, and
  `if workload_prefix is not None` branches can never fire. Collapse to
  `require_single_top(tops, "services")` then an unconditional
  `workload_prefix = f"{next(iter(tops))}/"`.
- **Dead guard in `verbs/publish.py:75-76`** — unreachable through
  `load_config` (non-empty `compose_bundles()` implies local entries implies
  `_require_project_pairings` already refused `repository is None`;
  `tests/test_publish_compose.py:108-115` pins the load-time refusal). Replace
  with the repo's narrowing idiom (`assert config.repository is not None`) or
  delete.
- **Redundant conjunct** (config.py:374) — `axis is not None and variant is
  not None` tests one fact twice; `ServiceBuild._axis_pairs_with_variants`
  guarantees `axis ⟺ variants` and `target_variants` yields `variant=None`
  exactly then. Keep `if variant is not None:`.

## 1.3 Single-call inlining (each verified: exactly one src call site, zero
direct test references, no owning `try` unless the caller has none either)

`up.py`: `_bring_up` (:49) — becomes `up`'s body. `assembly.py`:
`_materialize_and_validate` (:161), `_render_root_document` (:169),
`_render_caddyfile` (:236), `_render_dev_overlay` (:171) — the constructor
becomes one linear resolve→render→validate body, which is what AGENTS.md
already says it is; `forwarded_pair` (:101), `assert_known_profiles` (:192),
`_service_names` (:134). `bake.py`: `_resolve_target` (:125),
`_run_bake` (:168; it owns a `try` but `run_build` has none, so no nesting).
`config.py`: `_any_local_entries` (:656 — inline as a plain loop, which also
kills the file's most convoluted double-`any` expression);
`_uv_version_pin`/`_python_version_pin` (:771/:773); `_resolve_base_image_templates`
(:690) rides the same rule. `score.py`: `_validate_workload_surface` (:159)
and `_validate_dev_toggles` (:160) — inlining also collapses the double
`WorkloadDocument.model_validate` of the same document (:286, :345);
`enforce_secret_contract` (:139) — with secrets.py folded in, the whole
contract (declaration model, collector, enforcement, mint list) reads in one
place. `markers.py`: `_image_spec` (:97), `_refuse_literal_images` (:120 — the
law text lives in the refusal message, which survives). `provisioners.py`:
`AuthoredProvisioners` (:104) — inline as a shape check plus a per-entry
`AuthoredProvisioner.model_validate` loop; two test assertions matching
pydantic's stock list message change with it. `image_refs.py`: `_is_attestation`
(:54) plus the two single-use constants, inlined during the fold.
`lock.py`: `entry_for` (:113) — inline the selection into `env_entries` (the
lacks-name tail at :104-106 is unreachable from src since `env_entries`
iterates the lock's own keys; the platform-refusal branch is load-bearing and
stays); the five direct `entry_for` tests rewrite against `env_entries`; the
`require`/`entry_for` duplication dissolves with it.

Not inline candidates despite single-src-call counts: the record members
(`select_platform`, `render_bake`, `build_partitions`, `finalize`,
`generate_compose`, `generate_k8s`) — they are the documented derivation
surface with test-pinned signatures, the architecture rather than indirection;
`host_platform` (13 test references); `_committed_lock`/`_declaration_frozen`
(the sanctioned try-block / guard-predicate exemptions; keep).

## 1.4 Relocation to the right file

- **`ensure_score_tools` → `verbs/score.py`** (score.py:532-538): CLI-lane
  machinery (fetch pinned binaries into a per-run temp bin) with exactly one
  caller (verbs/score.py:59), inlined into the check lane's
  `TemporaryDirectory` block; the dead default dies. AGENTS.md rows for
  `score.py` and `verbs/score.py` both name it — update both.

## 1.5 Duplication dedup

- **`mirror_prefix` record member** — `config.mirror.prefix if config.mirror
  is not None else DEFAULT_MIRROR_PREFIX` is byte-identical at lock.py:35 and
  verbs/mirror.py:25. A `DockerDevkitConfig` property (the documented home for
  derivations over the declared world); both sites consume it.
- **`declared_charts` record member** — `config.k8s.charts if config.k8s is
  not None else {}` appears four times: lock.py:55, lock.py:153, config.py:700,
  verbs/mirror.py:33. Same treatment.
- **`ImageLockEntry` recorded-digest projection** — `[entry.index,
  *entry.platforms.values()]` spelled at verbs/up.py:90 and verbs/mirror.py:31.
  A member on `ImageLockEntry` (config.py, beside `require`); both probes
  consume it, mirror keeps its local `None → []` guard.
- **The keep-lock-env failure dance** — print-path-and-content, null the
  reference, unlink the rest: assembly.py:162-166, verbs/up.py:50-55,
  verbs/down.py:42-49. Make it an `Assembly` method (`keep_lock_env_file`)
  used by the constructor's `except` and by `up` (which also kills up.py:52's
  reach-in mutation `assembly.lock_env_file = None`); `down`, which has no
  `Assembly`, keeps its local two-liner against the free function.
- **The lenient cutover-lock read** — the same `except (TypeError, ValueError)`
  fallback exists as config.py:706-712 (finalize's CADDY check) and
  lock.py:179-190 (`_committed_lock`). One spelling: give the lenient read a
  single home and have finalize consume it; the two callers keep their
  distinct fallback actions.
- **`compose_bundles()` derived twice at the call site** — verbs/score.py:39+62
  and verbs/publish.py:48+57: hoist one local.
- **`run_lock`'s write lane re-implements `write_lock`** (lock.py:158-176 vs
  :134-138): compute the rendered text only in the `--check` lane (it alone
  needs the diff); the write lane becomes `write_lock(config,
  upgrade=upgrade)` — which also gives `write_lock` its second caller.
- **finditer+parts/cursor substitution loops → `pattern.sub` callbacks** —
  publish.py:74-83 (finalize_baked), score.py:581-594 (`_rendered_workloads`),
  config.py:780-787 (template substitution): all three mechanical rewrites of
  the compact form publish.py:33-35 already shows. (The score.py callback
  raises `SystemExit` on a missing value — raising from a `re.sub` callback is
  fine.)
- **`STATE_PREFIX` vs the bare `".score-compose"` literal** (score.py:46 vs
  :151) — derive both from one constant so the state-dir name cannot drift.
- **score.py:447 recomputes `config.chart_values_path(chart_name)`** that :439
  already holds in `path` — reuse via `values_dir / path.relative_to(root)`.
- **assembly.py derives the auth selection twice** (:101 and :148-152) —
  derive `auth_option = choices.get(AUTH_GROUP_NAME)` once.
- **assembly.py:283 re-derives `score_dir_path()` by hand**
  (`self.root / self.config.score_dir`) — use the member.
- **The digest grammar `sha256:[a-f0-9]{64}`** spelled in config.py:37-39,
  config.py:67, lock.py:24 — one shared fragment constant in config.py,
  interpolated into the three patterns.
- **`secrets.DECLARED_KEY` is byte-identical to `config.ENV_NAME_PATTERN`**
  (secrets.py:21 vs config.py:32) — dissolves into the 1.1.1 fold (score.py
  imports `ENV_NAME_PATTERN`).

## 1.6 Validation timing and guard fixes

- **`down.py` writes a temp file before a step that can fail, outside its
  `try`** (:33-34 vs try at :40): `write_lock_env_file` can raise
  (`select_platform` refusal on a multi-platform repo without `PLATFORM=`;
  malformed lock) and strands the name-only yaml in `/tmp` forever. Swap the
  two writes — the `Assembly` constructor already orders this correctly
  (assembly.py:158 before :169).
- **`bake.run_build` writes the lock before refusing bad input** (write_lock
  at :102; the no-dev-stage refusal at :117 and unknown-target refusals at
  :125-131 fire after it): nothing in target selection reads the fresh lock —
  hoist the target-selection block above `write_lock` so failing invocations
  leave the tree untouched.
- **`tree_tags` computes the git tree-SHA machinery before argument
  validation** (verbs/tree_tags.py:27 vs :29-33): validate names first,
  compute `shas` just before the print loop.
- **`--check` output set drift hazard** (verbs/score.py:62-66 re-derives what
  `generate_artifacts` derives internally at score.py:128-139, gated by the
  same `SECRETS_FILE.is_file()` test): have `generate_artifacts` return the
  list of paths it wrote (currently returns `None`; additive, behavior-
  preserving) and let the check lane `git status` over that — the staleness
  gate can never again silently stop covering a new artifact kind.

## 1.7 Ordering, naming, and micro-cleanups

- **config.py violates classes-at-top / entry-point-near-top**: `ImageLockEntry`
  (:828), `ImageLock` (:850), `ComposeBundle` (:890), `ToolRequirement` (:897)
  sit below functions; `load_config` (:961) is the last definition. Reorder
  (mechanical; cross-references are runtime lookups). Same law in
  service_builds.py:20-24 (class below a def) — moot after the fold.
- **Callers-before-callees swaps**: assembly.py (`_compose_config` :375 above
  `compose_command` :337; `merged_environment`/`parse_env_file` moot if
  inlined), lock.py wholesale (relock/render_lock above their callers;
  reorder as part of the folds), image_refs.py:22 (moot after fold).
- **`ImageLock()` vs `ImageLock.model_validate({})`** (lock.py:185/190 vs
  config.py:307): pick `ImageLock()`.
- **`lock_entries()` misleads** (config.py:303) — it returns an `ImageLock`,
  not entries; rename (`image_lock()` or `lock()`), ~6 callers.
- **bake.py:78 `host` shadowing** — registry host then os/arch (:105): rename
  the first to `registry_host`.
- **score.py:391 `output_path = output`** — pointless parameter alias; the
  parameter is used once. Delete the alias, rename the parameter.
- **verbs/build.py:33** — comprehension variable `service` mislabels target
  addresses; rename to `address`.
- **Single-reference module constants → inline at their one use**: config.py —
  `SCORE_DIR`, `K8S_OUTPUT`, `K8S_LOCAL_OUTPUT`, `SCORE_COMPOSE_VERSION`,
  `SCORE_K8S_VERSION`, `HELM_VERSION` (each exists only as its field's
  default), `BUNDLE_OUTPUT_DIR`, `PLAIN_VARIANT`, `VARIANT_VALUE_PATTERN`,
  `UV_EXACT_PIN_PATTERN`, `ROUTE_PATH_PATTERN`, `ROUTE_UPSTREAM_PATTERN`,
  `REMOTE_REF_PATTERN`; assembly.py — `PROFILES_VARIABLE`, `DEV_MOUNT_TARGET`,
  `EDGE_CONFIG_TARGET`, `CADDY_IMAGE_HOLE`; up.py — `_MANIFEST_ABSENT_PATTERN`;
  verbs single-use locals (`partitions` in matrix.py:16, `entries` in
  lock_env.py:24, `names` in verbs/publish.py:64, `selected` in bake.py:138,
  the `artifact` aliases at score.py:510/517). Multi-reference constants stay.
- **verbs/build.py:28** — `no_cache` uses the default-value `typer.Option`
  form while the rest of the file uses `Annotated` (the repo's B008-avoidance
  convention); convert.

---

# Section 2 — Judgement calls

The architecturally correct answer is genuinely ambiguous in each of these;
the lean is stated with reasons. Blast radius played no part in the
classification. All ten were decided this session; each entry records its
decision.

Session stance (recorded after 2.1): the record law — "every derivation over
the declared world rides the record as a member function" — is regarded as a
transitional organizing principle, not a permanent one. It is expected to
yield to plain logical grouping as the repo's cleanup proceeds, but the
outcome is not assumed in advance. Leans resting mainly on the record law are
discounted accordingly; no call is resolved by record-law arguments alone.

**2.1 The lock read projections: stay in `lock.py` or move to `config.py`?**
Today the lock's *state* lives in config.py (`ImageLock`, `parse_image_lock`,
`require`, `lock_entries`) while its *read projections* (`env_entries`,
`compose_env_entries`, `render_env_entries`; `entry_for` inlines per 1.3) live
in lock.py — so every read-side consumer stitches both modules
(assembly.py, score.py, verbs/lock_env.py, verbs/publish.py each import from
both), and only bake (write-then-read) and mirror (state-only) touch one. The
record law ("every derivation over the declared world rides the record")
points one way: `lock.env_entries(platform=)` beside `require`, a
`config.compose_env_entries(lock, environment)` member beside `select_platform`,
`render_env_entries` in config beside the state it renders — after which every
lock reader imports config alone, and lock.py is exactly the compute-and-write
engine plus the folded remote resolution (1.1.5). The coherent alternative —
all lock code in lock.py, state included — loses to a hard constraint: config's
`finalize` CADDY check and `lock_entries` member need `parse_image_lock`, so
moving state to lock.py cycles (config→lock→config) unless load-time
validation is fragmented, which is worse. **Decided (this session): the projections stay in `lock.py`.** The placement is
authored intent, not drift; the AGENTS.md row stands and the record-law pull
toward config members is discounted by the session stance above.

**2.2 Where does `.env` reading live?** `merged_environment` (assembly.py:318)
has four callers — assembly itself, `down`, `lock_env`, and `score.py`, which
imports the whole assembly/bashrun/compose machinery for this one function.
`parse_env_file` (:307) has one src caller (it inlines into
`merged_environment` regardless) plus an AGENTS.md-documented external
contract line ("consumer scripts import it as `docker_devkit.assembly") — a
contract the phase 9 wave is already scheduled to kill (consumer scripts move
to a consumer-local env module; zero `docker_devkit` imports is the wave's
verified end state), so it should not anchor the placement. Options:
(a) move `merged_environment` to config.py beside `select_platform` — both are
"environment selection" laws feeding the same members, score.py drops its
assembly import, config.py gains the os.environ dependency; (b) keep it in
assembly (the `.env` file is the runtime selection surface the assembly
consumes) and accept score.py's cross-import. **Decided (this session): (b) — keep it in `assembly.py`.** The assembly owns
its input surface; score.py's cross-import stands as accepted cost.

**2.3 `select_platform_manifests` after the image_refs fold: named law or
inlined?** It will have one src call site (`resolve_remote_manifests`) — the
axiom says inline — but it encodes four distinct laws (attestation skip, exact
os/arch match, multiple-match refusal, no-match listing with available
platforms) and five direct tests exercise it without the `bash_output` seam;
inlining forces those tests through the subprocess seam. **Decided (this session): inline it** into
`resolve_remote_manifests` as part of the 1.1.5 fold; the five direct tests
rewrite through the `bash_output` seam.

**2.4 score.py's four single-use state-lens classes** (`ManifestMetadata`,
`ManifestDocument`, `WorkloadState`, `ScoreState`, score.py:82-105; zero test
references). All four exist to extract a sort key (:461-465) and one
`instance_suffix` rewrite (:478-486) — replaceable by plain dict access,
deleting ~25 lines and 4 classes. The behavioral delta: pydantic currently
refuses a malformed `workloads:` block in score-k8s's state.yaml loudly; dict
access would silently skip the rewrite. Either keep a minimal shape check
(loud) or accept the silent skip (the state file is tool output we already
trust enough to rewrite). **Decided (this session): keep the classes.** The lenses parse a pinned
third-party tool's output (score-k8s), not devkit-produced data — the pydantic
refusal is the loud signal when the tool's shape drifts across a pin bump.
AGENTS.md stands unchanged.

**2.5 The dev-toggles annotation surface has two consumers.** assembly.py:280-293
(`up --dev` overlay) and score.py:342-357 (generation validator) both do
`WorkloadDocument.model_validate` → `annotations.get(DEV_TOGGLES_ANNOTATION)` →
shape checks → `parse_dev_toggles`. Options: (a) a `WorkloadDocument` member
in documents.py returning the raw annotation + service name, each consumer
keeping its own refuse/skip policy, with `parse_dev_toggles` staying in config
beside `DEV_TOGGLES_ANNOTATION` (11 references); (b) leave the ~8 duplicated
lines. **Decided (this session): (a)** — add the
`WorkloadDocument` member returning the raw annotation + service name; each
consumer keeps its own refuse/skip policy. Rides with the 2.9 outcome on the
skip branches.

**2.6 `MirrorConfig` is a one-field model** (config.py:113-123) — collapsible
to a root `mirror_prefix` field, deleting a class. It changes the authored
YAML shape (`mirror: {prefix: …}`), i.e. the declaration schema: permitted
pre-1.0 via the patch flow, but the phase 9 wave repins five consumers onto
this release without budgeting config edits, and a schema change would tax
every consumer's relock. **Decided (this session): flatten.** Delete the class; root
`mirror_prefix` (optional, absent ⇒ `DEFAULT_MIRROR_PREFIX`) carries the same
validator. The five consumer config edits + relocks ride the phase-9 repin
wave. 1.5's `mirror_prefix` record member is unaffected — it reads the flat
field.

**2.7 `PublishEntry.files` is a never-exercised multiplicity** (publish.py:16-18)
— constructed exactly once with a single-element list (verbs/publish.py:68);
`bake_sources` loops over a list that always holds one name; the one test also
passes one. Dropping the record for a `(name, path)` pair (or shrinking to a
`file: str`) deletes a shape nothing uses. **Decided (this session): delete `PublishEntry` outright.** It is a pure 1:1
re-wrap of the bundle's `(name, output)` with zero test references; the verb
loops over the filtered `compose_bundles()` list directly, `bake_sources`
takes and returns a single path, and `PublishedEntry` (computed facts, not a
re-wrap) stays.

**2.8 The release-binary installer dance** — `crane.install_crane`
(crane.py:15-17) and `ensure_score_tools` (score.py:536-538) spell the same
curl→tar→unlink three-step with different destinations and lifecycles
(system bin with sudo at any verb runtime vs per-run temp bin in the check
lane only). Splicing them couples the two lifecycles and drags a sudo axis
through the check lane; leaving them costs three duplicated shell lines.
**Decided (this session): leave both** — structural similarity, not shared law.

**2.9 `_render_dev_overlay`'s silent skips** (assembly.py:286-290): the
`not isinstance(raw, str)` and `not service_name` branches duplicate
generation-time refusals (score.py:345-357) over committed artifacts, so they
are unreachable absent post-generation drift — and then they silently drop
toggles where the repo's ethos is loud refusal. The `service_name not in
declared` skip (:289) is deliberate and test-pinned. Options: leave (leniency
for hand-edited artifacts), or drop the two unreachable branches so drift
surfaces as a KeyError instead of silence. **Decided (this session): drop the two branches** —
drift surfaces loudly instead of silently dropping toggles; the
absent-annotation and `not in declared` skips stay.

**2.10 The `$`-doubling law is spelled three times** (assembly.py:260,
publish.py:64, score.py:255) — one semantic law, and the exception case
(consumer-interpolated files) exists only at the score site. Consolidating
requires a new shared helper, which the session's no-new-decomposition rule
bans; recorded so the duplication is known-law rather than accidental.
**Decided (this session): leave the spellings.** Ensure AGENTS.md states
the law once, across the sites it names (prose-only commit).

---

# Section 3 — Verified non-findings (do not re-audit)

- **Ruff/imports clean**: no unused imports, no unused locals, no inline
  imports (`ruff --select F401,F811,F841,PLC0415` clean); the 2 `ARG001` hits
  are framework-mandated callback signatures.
- **No dead module-level names**: every def/class/constant in src has at
  least one reference somewhere; the one test-only symbol (`ServiceBuild.targets`)
  and one test-seam-only external (`K8sConfig.rendering`) are in 1.2.
- **`documents.py` genuinely earns its file**: `ComposeDocument` has two src
  consumers (score.py:239, publish.py:52), `WorkloadDocument` two modules
  (score.py, assembly.py). The publish-only `.configs` half of the lens stays
  — it models the document shape, and removing it would force publish.py to
  grow a second partial compose model.
- **`crane.py` earns its file**: two consumers (mirror, publish), 17 lines,
  single home of the version pin; every fold path duplicates the pin or
  creates verb-to-verb coupling. The x86_64-only archive spelling is the CI
  runner reality.
- **`optional_value_option.py` still needed and correctly isolated**: the
  pinned typer 0.27.3 vendors click without `flag_value` (zero matches across
  the installed package); the module is the sanctioned `# noqa-wrapper:`
  suppression boundary and cannot fold into `verbs/build.py` without leaking
  the per-line suppressions into a non-wrapper module. Optional cosmetic:
  the generic `Destination*` names advertise a generality that doesn't exist
  (single consumer).
- **The verb preamble** (`root = Path.cwd(); config = load_config(root)` plus
  divergent None-handling, ×10 verbs) is irreducible without new
  decomposition; each verb's None-behavior is documented intent (self-skip vs
  refuse vs `[]`).
- **`up --build`'s `repository or base_images` guard is live** (a services-only
  repo legally loads) — unlike publish's dead twin (1.2).
- **The "unknown names; the vocabulary is …" refusal shape ×5 and the
  `app = typer.Typer(...)` line ×10** are message/CLI style laws, not
  consolidation targets.
- **Tests' structural facts**: 22 monkeypatch sites patch
  `config_module.compute_service_shas` (the fold preserves them);
  `test_lock.py` already patches resolution via `docker_devkit.lock.` names
  (the image_refs fold preserves them); the `up`/`down` tests drive only the
  CLIs plus one direct `parse_env_file` call.

# Sequencing

Each dissolution is one code commit (tests retargeted in it), preceded by the
AGENTS.md prose commit describing the target shape (spec-first; prose and
code never share a commit): (1) AGENTS.md, (2) secrets fold, (3)
provisioners+markers fold, (4) service_builds fold, (5) image_refs fold,
(6) daemon_gc fold, (7) dedup members + lock/verb cleanups, (8) inline wave,
(9) dead code + guards + ordering + micro-cleanups. The decided judgement
calls distribute into that sequence: 2.10's AGENTS.md law statement rides (1),
2.3 rides (5), 2.5 and 2.7 ride (7), 2.9 rides (8), 2.6 is its own prose+code
pair (a declaration-schema change), and 2.1/2.2/2.4/2.8 need no action.
Verify per session protocol: `uv run pytest`, `uv run
ruff check`, `uv run basedpyright`.
