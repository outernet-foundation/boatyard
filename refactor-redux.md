# refactor-redux.md — the image-declaration line

Plan file for the image-source declaration refactor. It holds this line's design rulings,
the move list, and the consumer transition. Present truth lives in the code and AGENTS.md;
at line close this file is deleted and the closeout takes the record.

## The problem

Four grammars spelled "what image does this container run" across score files:
hand-composed refs with axis and sha blanks (workloads), `{{ env "X_SHA" }}` env templates
(provisioner manifests), `${X_IMAGE}` slots (provisioner services, pulled workload images),
and `lock:`/`lock-image:` markers (chart values, k8s manifests). No document declared its
image; boatyard reconstructed the relationship by walking Dockerfile directories, matching
stems cut from hand-written ref strings, and propagating axis ARGs down FROM chains. The
variant fan was excavated from Dockerfile ARGs while being declared, wrongly, on service
groups. The repository prefix was hand-spelled inside every ref (17 sites in placeframe).

## Design rulings

- Service-image edges are authored at the consuming document, structurally. One grammar,
  two syntaxes: workload containers carry `outernet.internal/dockerfile: <path>` or
  `outernet.internal/lock: <base_images key>` annotations; provisioner manifests and chart
  values carry `dockerfile:` or `lock:` markers on the image field.
- `image:` is never authored in a workload file. The lens rejects the key; the render pass
  injects the composed reference before score sees the document. `${NAME_SHA}` slots
  survive only as data uses of minted values (`SERVICE_VERSION`).
- `base_images` is the single registry of things builds are founded on, with two entry
  shapes. Pulled: `KEY: "host/path:tag"`, lock-resolved. Built:
  `KEY: {dockerfile: <path>, axis: {AXIS: [values]}}`, closure-resolved. A built base's
  published name is its dockerfile directory stem; the map key is only the ARG/FROM name —
  two declared facts, no convention joining them.
- One FROM grammar: every base reference is `ARG KEY` plus `FROM ${KEY}` (the shape
  api/Dockerfile already uses). Bare-name FROMs and their stem-matching resolution die.
  The build set is the closure over declared edges (annotations, markers, built entries)
  extended through FROM edges into built bases. No directory walk. No reverse check: an
  unreferenced Dockerfile is silently not built, like an unlisted score file.
- The fan is declared on the built base entry (axis name and values inline), verified by
  the matching ARG in that Dockerfile, and inherited by every image whose FROM edges reach
  it. There is no top-level axes map and no group `axes:` block. Bake's per-variant base
  wiring is re-keyed on the declared names (arg value or additional_contexts — the
  executor decides and records it here).
- The group fan is derived: a member whose image closure fans publishes one rendering per
  value (core-cuda/core-rocm); authored `options:` groups are unaffected; a closure fanning
  two axes refuses (the cross-product law). The options/axes lexical discriminator dies
  with group axes — the closure decides (fanned closure means a selectable group; no fan
  means an unconditional member). If the pydantic union still needs a lexical marker, the
  executor adds the minimal one and records it here.
- One composer serves annotations and markers: repository (config) + directory stem +
  selected variant (only when the image fans) + tag. Compose renderings emit the
  per-machine slot spelling through the existing sentinel pass; k8s renderings emit
  literals (tree tags locally, pins when published).
- `requires` stays `>=0.7` (operator ruling: this line ships no version bump).

## Loud errors

- A declared dockerfile path (annotation, marker, or built entry) that does not exist.
- A lock annotation or marker naming no pulled base_images key, or naming a built entry.
- `FROM ${KEY}` naming no base_images key.
- A built entry's declared axis with no matching ARG in its Dockerfile.
- A built base entry no reached Dockerfile consumes (dead declaration).
- A built entry present in the lock (the lock pairs pulled entries only).
- Two built entries declaring one axis name with different value sets.
- A member whose image closure fans two axes.
- A workload container without exactly one source annotation, or with an authored
  `image:` key.

## Moves

1. `markers.py`: the reference composer and the `dockerfile:` marker arm.
2. `models/workload.py` + `score.py`: source annotations, `image:` rejected, injection at
   the per-run copy pass.
3. `models/image.py`: the facts parse stays; closure derivation from declared edges;
   FROM resolution against base_images; declared fan plus inheritance; the ARG-sniffing
   derivation (`own_axes`, `_effective_axis`) dies.
4. `models/config_file.py`: dual-shape `base_images`; the derived group fan and the
   discrimination decision; `_derive` walks declared edges for the closure and surfaces;
   `_refuse_unknown_axis_refs` deleted.
5. `build.py` and the verbs (`tree-tags`, `matrix`, `publish`, `bump`): consume the
   closure; re-key the per-variant base wiring on declared names.
6. `models/lock_file.py` + `lock.py`: pairing skips built entries; built entries in the
   lock refuse.
7. Tests: closure-derived sets, dual-shape base_images, FROM grammar, fan inheritance,
   composer per substrate, lens rules, placeframe-shaped fixtures.

## Placeframe transition

- boatyard.yml: a built entry joins base_images; the core `axes:` block is deleted;
  routes and ports are re-spelled in flow style:

      base_images:
        NEURAL_NETWORKS_BASE:
          dockerfile: workloads/neural-networks-base/Dockerfile
          axis:
            TORCH_DEVICE: [cuda, rocm]

- Dockerfiles: reconstructor and localizer add `ARG NEURAL_NETWORKS_BASE` and re-spell
  `FROM neural-networks-base` as `FROM ${NEURAL_NETWORKS_BASE}`. neural-networks-base is
  unchanged — its `ARG TORCH_DEVICE` stays as the branching mechanism the declared fan is
  checked against.
- Workloads: api, lease-server, reconstructor, localizer gain
  `outernet.internal/dockerfile` annotations; keycloak and ngrok gain
  `outernet.internal/lock` annotations. The image lines are deleted.
- Provisioners: postgres, seaweedfs, oidc-issuer manifests (both renderings) replace
  `{{ env "X_SHA" }}` image lines with `dockerfile:` markers; compose observability
  replaces `${X_IMAGE:?err}` slots with `lock:` markers (the k8s side already locks).
- Values files, bundle names, and consumer `ref:`s: no change (core-cuda/core-rocm now
  derive from the fan).

## What dies

The directory walk and the one-top-dir rule, the ARG-sniffing fan derivation, the
bare-name FROM grammar and its stem resolution, the stem surgery in
`_refuse_unknown_axis_refs`, group `axes:` blocks, hand-composed image refs,
`{{ env "X_SHA" }}` image lines, the slot-in-image grammar, and the repository prefix
inside score files.

## This file

This file exists to be executed and deleted. Sections are fixed, in order: identity,
problem, rulings, errors, moves, transition, deaths, this section.
