# refactor-redux.md — the image-declaration line

Plan file for the image-declaration refactor. It holds this line's design rulings,
the move list, and the consumer transition. Present truth lives in the code and AGENTS.md;
at line close this file is deleted and the closeout takes the record.

A first execution attempt grew the code (+383 lines) by implementing generality beyond
evidence; it was reverted whole. The rulings below pin the observed universe — every
refusal that exists because a case is not real is a refusal, not a general mechanism.

Execution now runs one commit per session-step, never the whole plan. Landed baseline
(boatyard `28e43ed`/`69a92a3`, placeframe `c9039a15`): the base-registry increment —
dual-shape `base_images`, the one `FROM ${KEY}` grammar resolving against pulled ∪ owned ∪
built keys, the fan declared on the built entry and inherited one hop, pulled-only lock
pairing with built keys refused, bake's base wiring re-keyed on declared names (arg value =
context key = directory stem; the `additional_contexts` redirect unchanged), and the
placeframe transition for all of that. The increment cost +107 net in src/ — the price of
`BuiltBaseEntry`'s validators and the refusal blocks; the moves below carry their state.
compaction.md's "Current state" section predates this baseline and is superseded by this
paragraph. Sandbox gaps this baseline hit: placeframe's venv holds a stale non-editable
boatyard copy (verify through boatyard's own project env until placeframe runs `uv sync`),
and `scripts/preflight.py` shells to a `preflight-python` entry point absent here (pytest +
ruff are the runnable gate).

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
  `outernet.internal/lock: <base_images key>` annotations (a container-name → source map on
  metadata, since score annotations live at the workload level); provisioner manifests
  carry `dockerfile:` or `lock:` markers on the image field. Chart values keep
  `lock:`/`lock-image:` only — no chart declares a built image and none may.
- `image:` is never authored in a workload file. The lens rejects the key; the render pass
  injects the composed reference before score sees the document. `${NAME_SHA}` slots
  survive only as data uses of minted values (`SERVICE_VERSION`).
- `base_images` is the single registry of things builds are founded on, with two entry
  shapes. Pulled: `KEY: "host/path:tag"`, lock-resolved. Built:
  `KEY: {dockerfile: <path>, axis: {AXIS: [values]}}`, closure-resolved. A built base's
  published name is its dockerfile directory stem; the map key is only the ARG/FROM name —
  two declared facts, no convention joining them. A built entry declares at most one axis.
- One FROM grammar: every base reference is `ARG KEY` plus `FROM ${KEY}` (the shape
  api/Dockerfile already uses). Bare-name FROMs and their stem-matching resolution die.
  Fan inheritance is one hop: a Dockerfile FROMs at most one built entry, and a built
  entry's Dockerfile FROMs pulled entries only (built-on-built chains refuse). There is no
  recursion and no per-image unanimity machinery; one global check holds that equal axis
  names carry equal value sets across entries.
- The build set is the closure over declared edges (annotations, markers, built entries)
  extended through those FROM edges into built bases. No directory walk. No reverse
  check: an unreferenced Dockerfile is silently not built, like an unlisted score file.
- The member grammar is lexical, not derived: `ref:` spells an entry; `options:` spells an
  authored group; group-level `workloads:` spells a fan group whose image closure must fan
  (a fan group whose closure fans nothing refuses; a non-fanning local set spells one
  authored option). There are no provisional members, no post-parse reclassification, and
  no group-level vars.
- The group fan is derived: a fan group publishes one rendering per value of the single
  axis its closure fans (core-cuda/core-rocm); a closure fanning two axes refuses. Bake's
  per-variant base wiring is re-keyed on the declared names (arg value or
  additional_contexts — the executor decides and records it here).
- One composer serves annotations and markers: repository (config) + directory stem +
  selected variant (only when the image fans) + tag. Compose renderings emit the
  per-machine slot spelling through the existing sentinel pass; k8s renderings emit
  literals (tree tags locally, pins when published).
- Every workload file loads once per `ConfigFile.load` — one lens, one pass, reused for
  declared edges, the derived fan, claims, and surfaces. The per-entry reload dies.
- `requires` stays `>=0.7` (operator ruling: this line ships no version bump).

## Loud errors

- A declared dockerfile path (annotation, marker, or built entry) that does not exist.
- A lock annotation or marker naming no pulled base_images key, or naming a built entry.
- `FROM ${KEY}` naming no base_images key.
- A built entry's declared axis with no matching ARG in its Dockerfile.
- A built base entry no reached Dockerfile consumes (dead declaration).
- A built entry present in the lock (the lock pairs pulled entries only).
- A built entry declaring two axes; two entries declaring one axis name with different
  value sets; a built entry whose Dockerfile FROMs a built entry; a Dockerfile FROMing two
  built entries.
- A fan group whose image closure fans nothing, or fans two axes.
- A workload container without exactly one source annotation, or with an authored
  `image:` key.

## Moves

Each move carries its landed state; unmarked moves are not started.

1. `markers.py`: the reference composer and the `dockerfile:` marker arm (provisioner
   manifests only).
2. `models/workload.py` + `score.py`: source annotations, `image:` rejected, injection at
   the per-run copy pass.
3. `models/image.py`: PARTIALLY LANDED — FROM resolution against `base_images`, the
   declared one-hop fan, and the ARG-sniffing deaths (`own_axes`, `_effective_axis`) are
   in; the build set still derives from the directory walk, whose replacement needs the
   declared edges of moves 1–2.
4. `models/config_file.py`: PARTIALLY LANDED — dual-shape `base_images` and
   `pulled_base_images` are in; the lexical member grammar and the closure-walking
   `_derive` wait (group `axes:` blocks still expand options).
5. `build.py` and the verbs (`tree-tags`, `matrix`, `publish`, `bump`): PARTIALLY LANDED —
   the per-variant base wiring is re-keyed on declared names; the verbs still consume the
   walk-derived set until move 4's closure replaces it.
6. `models/lock_file.py` + `lock.py`: LANDED — pairing skips built entries; built entries
   in the lock refuse (both at `LockFile.load` and at relock).
7. Tests: ONGOING — the landed increment carries its own (the image-grammar and
   built-entry refusal suites, the reworked fan/matrix/build fixtures).

## Placeframe transition

- LANDED — boatyard.yml: the built entry and the flow-style routes/ports are in:

      base_images:
        NEURAL_NETWORKS_BASE:
          dockerfile: workloads/neural-networks-base/Dockerfile
          axis:
            TORCH_DEVICE: [cuda, rocm]

  The core `axes:` block stays until move 4/D6; it and the built entry both spell
  TORCH_DEVICE with no cross-check in this baseline — agreement becomes structural only
  when the fan derives the group.
- LANDED — Dockerfiles: reconstructor and localizer carry `ARG NEURAL_NETWORKS_BASE` and
  `FROM ${NEURAL_NETWORKS_BASE}`. neural-networks-base is unchanged — its `ARG
  TORCH_DEVICE` stays as the mechanism the declared fan is checked against.
- Workloads: api, lease-server, reconstructor, localizer gain
  `outernet.internal/dockerfile` annotations; keycloak and ngrok gain
  `outernet.internal/lock` annotations. The image lines are deleted.
- Provisioners: postgres, seaweedfs, oidc-issuer manifests (both renderings) replace
  `{{ env "X_SHA" }}` image lines with `dockerfile:` markers; compose observability
  replaces `${X_IMAGE:?err}` slots with `lock:` markers (the k8s side already locks).
- Values files, bundle names, and consumer `ref:`s: no change (core-cuda/core-rocm now
  derive from the fan).

## What dies

Dead so far: the ARG-sniffing fan derivation, and the bare-name FROM grammar with its
stem resolution. Still to die: the directory walk and the one-top-dir rule, the stem
surgery in `_refuse_unknown_axis_refs`, group `axes:` blocks, hand-composed image refs,
`{{ env "X_SHA" }}` image lines, the slot-in-image grammar, the repository prefix inside
score files, and the per-entry workload reload.

## This file

This file exists to be executed and deleted. Sections are fixed, in order: identity,
problem, rulings, errors, moves, transition, deaths, this section.
