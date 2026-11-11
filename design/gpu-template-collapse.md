# Open drill: GPU template collapse [LOW, operator's lead]

The cuda/rocm pair: one annotation (`boatyard/gpu`), two compose
templates (`gpu-cuda.tpl`, `gpu-rocm.tpl`), one registered, selected by the
`GPU_VARIANT` build axis; the k8s analog (`gpu-pod.tpl`) is one template —
proof the one-template shape is natural. The operator believes a consolidation
key is unfound; the agent does not believe this collapses cleanly. Drill
continues.

Settled facts (do not re-derive): the score-compose patch-template context is
`.Compose` + `.Workloads` only — no env, no rendering vars (sprig helpers
available); the score-k8s context adds `.Manifests` + `.Namespace`; patch
templates fire after conversion/provisioning, before the final file is
written. `GPU_VARIANT` is a rendering-level variable that never enters the
template context; the only channel to thread it in is the annotation value at
render time (compose-only; k8s has no `GPU_VARIANT`), which couples the
shared `rendered_workloads` to the gpu annotation — a real cost. If the key
is found, the compose templates list becomes a fixed `[healthcheck, restart,
gpu]` and the templates param a plain per-substrate constant; if not,
templates stay computed at the call site, which is the landed shape — the
unified `render_score` is correct either way.
