# Score generate-function unification (plan; O1 + S1 open)

The 2026-11-02 sessions audited `generate_compose` (score_compose.py) and
`generate_k8s` (score_k8s.py) and found an accreted duplicated invocation
spine. `init_score_tool` (commit 7793a07) folded the flags+init half into
score.py; this plan closes the rest. It follows on from the landed
score-refactor.md work (D1–D7 in flight or done); it does not revisit
them. Two items are OPEN and must be worked through with the operator
before implementation begins — a consolidation drill on the two
non-absorbing bits (O1) and the shape decision (S1) whose options O1 may
revise. Both are below. The executing session does not implement until
they are closed.

## Standing constraints

- Behavior-preserving: refusal messages byte-identical, artifact formats
  and verb CLI contracts unchanged. Test edits are import retargets and
  construction swaps only — never fixtures or expectations.
- One-way import law unchanged (`config_lock`/`dockerfiles` ← `config`;
  score.py ← config; the pipeline modules ← score.py).
- score.py purity is WAIVED (decided 2026-11-02): `bash` may live in
  score.py; `init_score_tool` already imports it. Do not re-litigate.
- Bash-patch law: the tests monkeypatch `bash` per-module. `bash` calls
  in score.py are intercepted via `score_module.bash` patches. As of
  commit 7793a07, every score-generation patch site carries both the
  pipeline-module `bash` patch AND a `score_module.bash` patch (same
  recorder). `monkeypatch.setattr` raises on an absent attr by default,
  so lifting any further `bash` call out of the pipeline modules forces
  retiring the now-dead pipeline-module patches (see Settled step).
- Commit as you go; prose and code in separate commits; an AGENTS.md line
  a change invalidates updates in the same change.

## Open — work through these first

Two items are open and must be worked through with the operator before
implementation. O1 is a consolidation drill on the two bits this session
could not absorb; S1 is the shape decision, whose options O1's outcome
may revise. Do not implement until both are closed.

### O1 — Drill the two non-absorbing bits (templates, seed)

The operator's read: these two resist every consolidation tried this
session (run-in-both, hoist, plain input), but the operator believes a
consolidation opportunity is still unfound and wants to keep drilling
before accepting them as irreducible. They are NOT closed. The next
session picks each up with the operator:

**Templates.** A per-tool value, not a runnable step: compose computes
`[healthcheck, restart, gate-pin?, gpu-cuda/rocm?]`; k8s is
`[gpu-pod, scale-to-zero]`. Blockers found so far:
- run-in-both fails: the union breaks one-annotation-one-template —
  `outernet.internal/gpu` maps to gpu-cuda/rocm in compose AND gpu-pod
  in k8s, so one annotation → two templates in a single tool.
- hoist fails: compose's list needs `provisioners.declares_services`
  (known only inside `compiled_provisioners`) and `bundle`.
- plain input fails: same `declares_services` dependency.
Only-known shapes: inline per-consumer (A) or a `templates_for` hook (B).
Drill: is there a restructure (splitting always-registered vs conditional
templates; deferring registration; rethinking what each tool registers)
that lets one line serve both?

**Seed.** `_seed_instance_suffixes` reads `<state_dir>/state.yaml` and
writes score-k8s's `workloads.<name>.instance_suffix` schema, between
init (creates state.yaml) and generate (reads it). Blockers found so far:
- run-in-both fails: in compose, `<state_dir>` is `.score-compose/` —
  `.score-compose/state.yaml` may not exist (read crashes) or has a
  different schema (injects dead data / corrupts). Semantically void
  (compose has no instances).
- hoist fails: must sit between init and generate.
- k8s-only by construction: instance suffixes are a score-k8s concept.
Only-known shapes: inline k8s-only (A) or a `post_init` hook (B). Drill:
is there a restructure (achieving instance-suffix determinism without the
between-init-generate state write — recall D3's rejected fallback (b)
parse-mutate-dump over manifest scalars; or making the state write a
guarded no-op in compose; or folding the seed into init_score_tool
conditionally) that lets it share a home?

### S1 — Unification shape (decide after O1)

The generate half (`with rendered_workloads(...) + bash generate`) is
byte-identical apart from scalars and is agreed to lift (settled, below).
The open question is how much further to push:

- **A — parallel skeleton.** Lift `generate_score_tool` into score.py;
  run the absorbable in-between bits (ALPINE check, rmtree, mkdir) in
  both cases — they no-op in the other. Templates computation and the
  seed remain as per-consumer lines (templates pre-init; seed post-init,
  k8s-only). The two functions read as parallel skeletons with honest
  differing lines. No callbacks. Carrying cost: the differing lines (two,
  assuming O1 yields nothing and the ALPINE invariant holds — audit bit
  1); `generate_score_tool` carries ~9 params (config, workloads,
  rendering_vars, images, service_shas, tool, cwd, output,
  tool_environment).

- **B — full identity.** A `render_score_tool` in score.py owns
  `compiled_provisioners` + init + generate and takes two hooks: a
  `templates_for(builtin_templates, provisioners) -> list[Path]` (folding
  the ALPINE check + templates for compose; k8s returns its fixed list)
  and a `post_init` hook (k8s's seed). Call sites become byte-identical.
  Carrying cost: a ~14-param function plus two callbacks; the no-closures
  rule forces module-level callback functions reached via `partial`
  (e.g. `templates_for=partial(_compose_prepare, bundle=bundle,
  images=images)`), which re-scatters the differences into partials.
  Partial hooking (one hook, one differing line) is possible mid-spectrum.

The decision turns on: is "basically identical" (differing lines, no
callbacks) acceptable, or must the call sites be byte-identical (hooks)?

**A's differing-line count depends on O1 and on the ALPINE invariant
(audit bit 1): if O1 finds a consolidation for templates or seed, A
improves or full identity becomes achievable hooklessly — reweigh B
then. If the ALPINE invariant fails, A gains a third differing line.**

The next session works O1 and S1 with the operator, takes the rulings,
then implements the corresponding branch. Do not implement until both
are closed.

## Audit (settled findings — do not re-derive)

### Common spine (both functions)

`with builtin_patch_templates() + compiled_provisioners(...)` →
`init_score_tool(...)` → (in-between bits) → `with rendered_workloads(...)
+ bash(f"{tool} generate ...")`. The wrapping, init, and generate are
shared; the in-between bits and the per-bundle loop (compose, N
renderings) vs single (k8s, 1 monolith) differ.

### In-between bits — absorb status (run-in-both analysis)

1. **ALPINE check** — absorbs, conditionally. In k8s,
   `provisioners.declares_services` is False (the `services` field is the
   compose-gate concept; k8s provisioner files don't carry it), so the
   `and` short-circuits. Assumption: k8s provisioner files never author
   `services` — unenforced. A k8s provisioner misusing `services` would
   falsely demand ALPINE_IMAGE. Verify before relying on it; else the
   check stays compose-only (a third differing line in A).
2. **rmtree score_state** — absorbs. `.score-compose/` is absent in k8s,
   `.score-k8s/` absent in compose; `ignore_errors=True` no-ops both ways.
3. **output.parent.mkdir** — absorbs. `exist_ok=True` is idempotent.

The two bits that did NOT absorb (templates, seed) are open for further
drilling — see O1, not settled.

### Generate half — settled: lift

The `with rendered_workloads(...) + bash generate` is byte-identical
apart from scalars. Lifting `generate_score_tool` into score.py is agreed
(the init_score_tool lift under-delivered by leaving it inline).

## Implementation

### Settled step (either shape): lift generate_score_tool

Add `generate_score_tool(tool, cwd, output, config, workloads,
rendering_vars, images, service_shas, tool_environment)` to score.py,
owning `with rendered_workloads(...) + bash(generate)`. Both pipeline
files call it.

Test retarget (consequence of moving the generate `bash` call into
score.py): after this lift, score_compose calls `bash` directly nowhere,
and score_k8s calls only `bash_output` (helm). So:

- Drop the `bashrun.bash` import from score_compose entirely; narrow
  score_k8s's to `from bashrun.bash import bash_output`.
- Retire the `score_compose_module.bash` and `score_k8s_module.bash`
  patches at every site — those attrs no longer exist and
  `monkeypatch.setattr` would raise. The `score_module.bash` patches
  (already present from the init lift) now intercept both init and
  generate through the same recorder.
- Keep `score_k8s_module.bash_output` patches (helm stays in score_k8s).

Verify: `uv run pytest tests/test_score_generate.py` green.

### If S1 = A (parallel skeleton)

1. In each pipeline function, structure the `with compiled_provisioners`
   block as: ALPINE check (both, no-op k8s) → templates (per-consumer) →
   rmtree (both) → `init_score_tool` → seed (k8s only; compose omits) →
   `output.parent.mkdir` (both) → `generate_score_tool`.
2. Factor each consumer's templates into a `_<x>_templates(...)` helper
   so the line reads `templates = _<x>_templates(...)` in both (parallel,
   not identical).
3. The two functions are now parallel skeletons differing on the
   templates line and the seed line (three if the ALPINE invariant fails).

### If S1 = B (full identity)

1. Add `render_score_tool(tool, cwd, project_name, rendering, workloads,
   rendering_vars, output, config, images, service_shas, tool_environment,
   templates_for, post_init=None)` to score.py. It owns
   `with builtin_patch_templates() + compiled_provisioners(...)`:
   `templates = templates_for(builtin_templates, provisioners)` →
   `init_score_tool(...)` → `post_init(...)` if set →
   `generate_score_tool(...)`.
2. compose call site (per bundle):
   `render_score_tool(..., templates_for=partial(_compose_prepare,
   bundle=bundle, images=images))`. `_compose_prepare` does the ALPINE
   check and returns `_compose_templates(...)`.
3. k8s call site:
   `render_score_tool(..., templates_for=_k8s_templates,
   post_init=partial(_seed_instance_suffixes, state_path, score_dir,
   k8s.workloads))`.
4. Module-level callback functions; `partial` bakes the consumer-specific
   args (no closures).

## Verify

`uv run --locked scripts/preflight.py` (the full gate) green at session
end; `uv run pytest tests/test_score_generate.py` green between steps.

## Out of scope

- The pre-validation loops (compose probe-adapter/dev-toggles; k8s
  dev-toggles-on-k8s-only) stay at their consumers — different placement
  laws, not accretion.
- The post-passes (compose ComposeDocument transforms; k8s chart
  rendering + document sort) stay at their consumers — different
  substrates.
- The bundle loop (compose N, k8s 1) stays — it is the spec.
