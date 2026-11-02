# Score generate-function unification — the open remainder

The unified spine landed whole: `render_score` in score.py owns builtin
templates → gate → init → seed (`if is_k8s:`) → generate; compose calls it
per-bundle, k8s once; the template and provisioner-compile context managers
dissolved into the spine's `with` tuple, `rendered_workloads` is the last
context manager standing, and the test retargets landed as planned. Git history
carries the execution; one drill remains open.

## GPU collapse drill [LOW — open, operator's lead]

The cuda/rocm pair: one annotation (`outernet.internal/gpu`), two compose
templates (`gpu-cuda.tpl`, `gpu-rocm.tpl`), one registered, selected by the
`GPU_VARIANT` build axis. The k8s analog (`gpu-pod.tpl`) is one template —
proof the one-template shape is natural. The operator believes a consolidation
key is unfound.

Verify-bits constrain it hard (see "Settled facts"): the score-compose
patch-template context is only `.Compose` + `.Workloads` — no env, no rendering
vars. `GPU_VARIANT` is a rendering-level variable that never enters the
template context. The only channel to thread it in is the annotation *value* at
render time (compose-only; k8s has no `GPU_VARIANT`), which couples the shared
`rendered_workloads` to the gpu annotation — a real cost. The agent does not
believe this collapses cleanly; the operator does. Drill continues.

If the key is found, the compose templates list becomes a fixed
`[healthcheck, restart, gpu]` and the templates param is a plain per-substrate
constant (no computation at the call site). If not found, templates stay
computed at the call site, which is the landed shape; the unified function is
correct either way.

## Settled facts (do not re-derive)

- **score-compose patch-template context** = `.Compose` (the generated compose
  project) + `.Workloads` (map of workload name → Score spec). No env, no
  rendering vars. Sprig helpers available. (Official docs,
  docs.score.dev/score-compose/patch-templates.)
- **score-k8s patch-template context** = `.Manifests` + `.Workloads` +
  `.Namespace`.
- **Patch templates fire after conversion/provisioning, before the final file
  is written** — nothing post-patch touches the patched fields.
- **score-compose synthesizes `wait-for-resources` with bare `image:
  alpine`** (real generated output confirms it; synthesis in
  internal/compose/convert.go). The gate emits only where a provisioner
  carries services.
- **The gate-pin move is byte-identical**: the post-pass already re-dumps the
  whole ComposeDocument; rewriting the gate's image pre-model produces the
  same artifact as the old patch-template rewrite.

## Cross-repo note

`placeframe/stack/AGENTS.md:289` references "the devkit's built-in gate-pin
template." It describes the released devkit placeframe currently pins, so it is
accurate for placeframe today and goes stale when placeframe upgrades its
devkit pin past 09603db — the boatyard catch-up wave (boatyard.md phase 5) is
that upgrade: fix the line in the wave's placeframe docs pass.
