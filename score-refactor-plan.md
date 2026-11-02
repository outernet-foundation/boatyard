# Score generate-function unification (plan; toward one unified spine)

The 2026-11-02 sessions found an accreted duplicated invocation spine
across `generate_compose` (score_compose.py) and `generate_k8s`
(score_k8s.py). `init_score_tool` (commit 7793a07) folded the flags+init
half into score.py; the gate-pin image pin moved from a patch-template to
the compose post-pass (commit 09603db). This plan drives toward one
unified spine function and supersedes the earlier O1/S1/shape-A/shape-B
framing — the operator diverged from the parallel-skeleton/hook shapes
toward a single function, and that is the direction below.

## End state (the operator's vision)

One function in score.py owns the whole rendering spine for both
substrates — compose calls it per-bundle, k8s calls it once:

`as_file(package templates) + ExitStack` → compile provisioners → init →
seed (k8s only) → generate.

Minimal parameters (config + is_k8s + per-rendering scalars; root,
score_dir, tool, rendering, state-prefix derived, not passed). Every
helper inline in the one function — init command-building, generate
invocation, the seed logic, the package-resource handle (`as_file`
directly in the spine's `with` tuple), and provisioner selection+compile
(the ExitStack holds the conditional compiled-provisioners tempdir; no
tempdir when no files match). `rendered_workloads` (tempdir) is the one
remaining context manager; the operator overruled the earlier "lifecycle
context managers cannot inline" stance. The one remaining substrate
difference (the GPU template selection) may also be eliminated; the
agent has reservations (see the GPU move).

## Landed

- **init_score_tool lift** (7793a07): flags+init half folded into
  score.py.
- **gate-pin → post-pass** (09603db): the `wait-for-resources` gate's
  image is pinned to `${ALPINE_IMAGE:?err}` as a pre-model raw-dict
  mutation in `generate_compose`, replacing the
  `wait-for-resources-image.tpl` patch-template (deleted). Removed the
  `declares_services`-gated templates conditional from `_compose_templates`
  (the ALPINE_IMAGE check still consumes `declares_services`, unchanged).
  Verified safe — see "Settled facts".
- **unified spine** (e82403e): `render_score` in score.py owns
  `builtin_patch_templates()` + `compiled_provisioners(...)` → gate →
  init → seed (`if is_k8s:`) → generate; compose calls it per-bundle,
  k8s once. `init_score_tool`, `_seed_instance_suffixes`, and
  `_compose_templates` dissolved — templates pass as names and the spine
  joins them against the builtin dir; the three context managers stay as
  the only helpers. The ALPINE_IMAGE check stayed at the compose consumer
  as `_require_alpine_when_declaring_services`, bound via
  `functools.partial` into a `gate` param — the one param beyond the
  minimal set, the seam that keeps the compose refusal text out of
  score.py. Test retarget landed as predicted: score_compose drops `bash`,
  score_k8s narrows to `bash_output`, per-module `bash` patches retired.
  The seed landed as the `if is_k8s:` branch (the move-2 disagreement
  resolved in the line's favor unless overruled).
- **context-manager inline** (d0039da): `builtin_patch_templates` and
  `compiled_provisioners` dissolved into `render_score` — `as_file`
  directly in the `with` tuple, selection+compile inline, an ExitStack
  holding the conditional tempdir (no tempdir when nothing matches).
  Their seams' tests deleted on the operator's direction ("test coverage
  is irrelevant"): the 22 compile-law tests in test_provisioners.py (the
  five AuthoredProvisioner model tests stay) and the order-independence
  test; AGENTS.md's "pinned by a test" parenthetical dropped in the same
  change. `rendered_workloads` is the last context manager standing.

## Moves (ordered by the agent's certainty the move is correct)

### 1. GPU collapse drill [LOW — open, operator's lead]

The cuda/rocm pair: one annotation (`outernet.internal/gpu`), two compose
templates (`gpu-cuda.tpl`, `gpu-rocm.tpl`), one registered, selected by
the `GPU_VARIANT` build axis. The k8s analog (`gpu-pod.tpl`) is one
template — proof the one-template shape is natural. The operator believes
a consolidation key is unfound.

Verify-bits constrain it hard (see "Settled facts"): the score-compose
patch-template context is only `.Compose` + `.Workloads` — no env, no
rendering vars. `GPU_VARIANT` is a rendering-level variable that never
enters the template context. The only channel to thread it in is the
annotation *value* at render time (compose-only; k8s has no
`GPU_VARIANT`), which couples the shared `rendered_workloads` to the gpu
annotation — a real cost. The agent does not believe this collapses
cleanly; the operator does. Drill continues.

If the key is found, the compose templates list becomes a fixed
`[healthcheck, restart, gpu]` and the templates param is a plain
per-substrate constant (no computation at the call site) — eliminating
the templates difference. If not found, templates stay computed at the
call site, which is the landed shape; the unified function is correct
either way.

## Settled facts (do not re-derive)

- **score-compose patch-template context** = `.Compose` (the generated
  compose project) + `.Workloads` (map of workload name → Score spec).
  No env, no rendering vars. Sprig helpers available. (Official docs,
  docs.score.dev/score-compose/patch-templates.)
- **score-k8s patch-template context** = `.Manifests` + `.Workloads` +
  `.Namespace`.
- **Patch templates fire after conversion/provisioning, before the final
  file is written** — nothing post-patch touches the patched fields.
- **score-compose synthesizes `wait-for-resources` with bare `image:
  alpine`** (real generated output confirms it; synthesis in
  internal/compose/convert.go). The gate emits only where a provisioner
  carries services.
- **The gate-pin move is byte-identical**: the post-pass already re-dumps
  the whole ComposeDocument; rewriting the gate's image pre-model
  produces the same artifact as the old patch-template rewrite.

## Standing constraints

- Behavior-preserving: refusal messages byte-identical, artifact formats
  and verb CLI contracts unchanged. Test edits are import retargets and
  construction swaps only — never fixtures or expectations. (The gate-pin
  collapse was a mechanism change, not a unification step; its test edits
  deleted the obsolete registration test and added an image assertion.)
- One-way import law unchanged (`config_lock`/`dockerfiles` ← `config`;
  score.py ← config; the pipeline modules ← score.py).
- score.py purity is WAIVED (decided 2026-11-02): `bash` may live in
  score.py; `render_score` carries both its calls. Do not re-litigate.
- Bash-patch law: the tests monkeypatch `bash` per-module. Every
  score-generation patch site (init + generate) carries only the
  `score_module.bash` patch; `score_k8s_module.bash_output` covers helm
  (helm stays in score_k8s). The per-module pipeline `bash` patches are
  retired — those attrs no longer exist and `monkeypatch.setattr` raises
  on an absent attr by default.
- Commit as you go; prose and code in separate commits; an AGENTS.md line
  a change invalidates updates in the same change.

## Out of scope

- The pre-validation loops (compose probe-adapter/dev-toggles; k8s
  dev-toggles-on-k8s-only) stay at their consumers — different placement
  laws, not accretion.
- The post-passes (compose ComposeDocument transforms — service-name
  collapse, gate rename, edge-reserved check, bind inlining, gate image
  pin; k8s chart rendering + document sort) stay at their consumers —
  different substrates.
- The bundle loop (compose N, k8s 1) stays — it is the spec.
- The ALPINE_IMAGE check (`_require_alpine_when_declaring_services` in
  score_compose.py, keyed on `declares_services`, bound into the spine's
  `gate` param) stays at the compose consumer.

## Verify

`uv run pytest tests/test_score_generate.py` green between steps; `uv run
ruff check`, `uv run ruff format --check`, `uv run basedpyright`, `uv run
deptry src` green. `uv run --locked scripts/preflight.py` currently fails
on a pre-existing ruff config issue (`lint.per-file-ignores` should be
`lint.extend-per-file-ignores` in the repo's ruff extend layer) —
unrelated to this work, reproduces on clean HEAD 5e5494d.

## Cross-repo note

`placeframe/stack/AGENTS.md:289` references "the devkit's built-in
gate-pin template." It describes the released devkit placeframe currently
pins, so it is accurate for placeframe today and goes stale only when
placeframe upgrades its devkit pin past 09603db. Update then, not now.
