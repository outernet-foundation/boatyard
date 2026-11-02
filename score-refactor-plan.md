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

`with builtin_patch_templates() + compiled_provisioners(...)` → init →
seed (k8s only) → generate.

Minimal parameters (config + is_k8s + per-rendering scalars; root,
score_dir, tool, rendering, state-prefix derived, not passed). Every
non-lifecycle helper eliminated — init command-building, generate
invocation, and the seed logic inline in the one function. The three
context managers that own resource lifecycles stay as the only helpers:
`builtin_patch_templates` (package-resource handle), `compiled_provisioners`
(tempdir), `rendered_workloads` (tempdir) — they cannot inline
(try/finally lifecycle). The operator believes the two remaining substrate
differences (the GPU template selection and the k8s seed) can also be
eliminated; the agent has reservations on both (see moves 3 and 4).

## Landed

- **init_score_tool lift** (7793a07): flags+init half folded into
  score.py.
- **gate-pin → post-pass** (09603db): the `wait-for-resources` gate's
  image is pinned to `${ALPINE_IMAGE:?err}` as a pre-model raw-dict
  mutation in `generate_compose`, replacing the
  `wait-for-resources-image.tpl` patch-template (deleted). Removed the
  `declares_services`-gated templates conditional from `_compose_templates`
  (the ALPINE_IMAGE check at score_compose.py:165 still consumes
  `declares_services`, unchanged). Verified safe — see "Settled facts".

## Moves (ordered by the agent's certainty the move is correct)

### 1. Build the unified spine function [HIGH]

One function in score.py owning `with builtin_patch_templates() +
compiled_provisioners(...)` → init (inlined) → seed (`if is_k8s:`,
inlined) → generate (inlined). Both pipelines call it. Templates passed
in as a param — compose computes `[healthcheck, restart,
gpu-cuda|gpu-rocm]` per bundle's `GPU_VARIANT`, k8s passes its fixed
`[gpu-pod, scale-to-zero]`. The init command-building (currently
`init_score_tool`), the generate invocation (currently inline in each
pipeline), and the seed (currently `_seed_instance_suffixes`) become
inline logic in the one function — no `init_score_tool` /
`generate_score_tool` / `_seed_instance_suffixes` helpers. The context
managers stay.

This supersedes the earlier "settled step: lift generate_score_tool" —
the generate code goes directly into the unified function inline, not via
a transient helper that the unified function would then absorb.

Test retarget: after this, all `bash` calls (init + generate) live in
score.py. score_compose drops the `bash` import entirely; score_k8s
narrows to `bash_output` (helm stays). The per-module `bash` patches
(`score_compose_module.bash`, `score_k8s_module.bash`) retire at every
site — those attrs no longer exist and `monkeypatch.setattr` would raise.
The `score_module.bash` patches (already present from the init lift)
intercept both init and generate through the same recorder. Keep
`score_k8s_module.bash_output` patches (helm stays in score_k8s).

This realizes the vision with two honest remaining differences
(templates-param, seed-if-line). Correct regardless of whether those
differences are later eliminated by moves 3–4.

### 2. Seed: line, not fold [HIGH — open disagreement]

The seed (`_seed_instance_suffixes`) writes deterministic instance
suffixes to score-k8s's `state.yaml` between init (creates it) and
generate (reads it) — pinning `InstanceSuffix` to `-` + first 10 hex of
sha256(metadata.name) so regeneration is byte-stable. k8s-only by
construction (compose has no instances).

**Agent recommendation: keep it as one `if is_k8s:` line in the unified
function.** Do NOT fold it into `init_score_tool` via a boolean (the
operator's proposal). Reasons: `init_score_tool`'s job is "run `<tool>
init`"; the seed is a devkit determinism concern (different
reason-to-change); folding makes the name a lie and grows the signature
with three k8s-only params (`state_dir`, `score_dir`, `workloads`) that
are null/ignored in compose. The boolean-for-string swap is only
motivated by the fold; keep the string. The one `if is_k8s:` line is
honest — k8s genuinely has a determinism step compose doesn't, and the
unified function is exactly where that substrate divergence should be
visible.

Open disagreement — the operator proposed the fold. Resolved in the
line's favor in move 1's shape unless overruled.

### 3. GPU collapse drill [LOW — open, operator's lead]

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
call site, which is already the move 1 shape; the unified function is
correct either way.

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
  score.py; `init_score_tool` already imports it. Do not re-litigate.
- Bash-patch law: the tests monkeypatch `bash` per-module. `bash` calls
  in score.py are intercepted via `score_module.bash` patches. After move
  1, every score-generation patch site carries only the `score_module.bash`
  patch (the per-module pipeline patches retire). `monkeypatch.setattr`
  raises on an absent attr by default.
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
- The ALPINE_IMAGE check (score_compose.py:165, keyed on
  `declares_services`) stays pre-generate at the compose consumer.

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
