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
repository prefix was hand-spelled inside every ref (17 sites in placeframe).

## Design rulings

- The consumer-to-image edge is authored at the consuming document, structurally. One
  grammar, two syntaxes: workload containers carry `outernet.internal/dockerfile: <path>`
  or `outernet.internal/lock: <base_images key>` annotations; provisioner manifests and
  chart values carry `dockerfile:` or `lock:` markers on the image field (`lock` and
  `lock-image` exist today; `dockerfile` is new).
- `image:` is never authored in a workload file. The lens rejects the key; the render pass
  injects the composed reference before score sees the document. `${NAME_SHA}` slots
  survive only as data uses of minted values (`SERVICE_VERSION`).
- The build set is the closure over declared edges, extended by FROM lines inside reached
  Dockerfiles. The directory walk dies. There is no reverse check: an unreferenced
  Dockerfile is silently not built, the same as an unlisted score file. The forward check
  errors on a declared path that does not exist.
- A fan is declared by the leaf Dockerfile: an image fans on an axis iff its own ARG names
  a declared axis. FROM-chain propagation dies. Group `axes:` keep their option-expansion
  role and value vocabulary; they no longer imply per-workload variance — an image without
  the ARG receives the same reference in every rendering of the group.
- One composer serves annotations and markers: repository (config) + directory stem +
  selected variant (only when the image fans) + tag. Compose renderings emit the
  per-machine slot spelling through the existing sentinel pass; k8s renderings emit
  literals (tree tags locally, pins when published).
- `requires` stays `>=0.7` (operator ruling: this line ships no version bump).

## Loud errors

- A declared dockerfile path that does not exist.
- A lock annotation or marker naming no base_images key.
- A group axis no reached image fans on.
- `FROM <bare-name>` resolving to zero or multiple declared image stems.
- A workload container without exactly one source annotation, or carrying an authored
  `image:` key.

## Moves

1. `markers.py`: the reference composer and the `dockerfile:` marker arm.
2. `models/workload.py` + `score.py`: source annotations, `image:` rejected, injection at
   the per-run copy pass.
3. `models/image.py`: the Dockerfile facts parse stays; the walk is replaced by closure
   derivation from declared edges; `_effective_axis` deleted; leaf-ARG fan.
4. `models/config_file.py`: `_derive` walks declared edges (workload annotations, selected
   provisioner markers) for the closure and the consumer surfaces; `_refuse_unknown_axis_refs`
   deleted; the axis-coverage error added.
5. `build.py` and the verbs (`tree-tags`, `matrix`, `publish`, `bump`): consume the
   closure; no behavior change expected beyond inputs.
6. Tests: walk-derived set tests become closure-derived; lens grammar tests for the
   annotation rules; composer tests per substrate; placeframe-shaped integration fixtures.

## Placeframe transition

- Workloads: api, lease-server, reconstructor, localizer gain
  `outernet.internal/dockerfile` annotations; keycloak and ngrok gain
  `outernet.internal/lock` annotations. The image lines are deleted.
- Provisioners: postgres, seaweedfs, oidc-issuer manifests (both renderings) replace
  `{{ env "X_SHA" }}` image lines with `dockerfile:` markers; compose observability
  replaces `${X_IMAGE:?err}` slots with `lock:` markers (the k8s side already locks).
- Dockerfiles: `ARG TORCH_DEVICE` added to the reconstructor and localizer leaves (the ARG
  currently lives only on neural-networks-base).
- boatyard.yml, values files, and consumer `ref:`s: no change.

## What dies

The directory walk and the one-top-dir rule, `_effective_axis`, the stem surgery in
`_refuse_unknown_axis_refs`, hand-composed image refs, `{{ env "X_SHA" }}` image lines,
the slot-in-image grammar, and the repository prefix inside score files.

## This file

This file exists to be executed and deleted. Sections are fixed, in order: identity,
problem, rulings, errors, moves, transition, deaths, this section.
