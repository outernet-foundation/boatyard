# score.py refactor (decision session)
The 2026-11-02 audit session found score.py carrying five unrelated
concerns in one 828-line module. This file records the audit, the
obviously-correct change set, and the locked decisions D1–D7. The next
session implements this file and does not redecide.

## Standing constraints

- The one-way import law is unchanged (`dockerfiles` imports nothing from
  `config`; `config_lock` ← `config` one-way); new modules join the module
  map without weakening it.
- Refusal message texts stay byte-identical unless a locked decision below
  says otherwise; AGENTS.md greps these strings.
- Authored key order controls dump order for provisioner compilation — a
  test pins it, and no decision may break it.
- The `optional_value_option.py` `Any`s stay: it is the declared
  `# noqa-wrapper:` boundary mirroring click's own API.
- Commit as you go; prose and code in separate commits; an AGENTS.md line a
  change invalidates updates in the same change.

## Audit findings

- Five concerns in one module: rendering pipelines (compose + k8s),
  provisioner/marker compilation, secrets, annotation validation, YAML
  dump plumbing. The test suite already partitions along these seams
  (`test_provisioners.py`, `test_secrets.py`, `test_score_generate.py`).
- Dead code: the two `write_generated_file(state_path, …)` calls in
  `generate_k8s` feed the `.score-k8s/state.yaml` that `shutil.rmtree`
  deletes unconditionally two lines later; the path-reduction pass is
  entirely dead. The suffix *read* is load-bearing (feeds the manifest
  rewrite); D3 owns its fate.
- String surgery: `manifest_text.replace(f"{name}{current}", …)` rewrites
  suffix occurrences by bare substring across the whole manifest — works by
  luck of naming. Three read→parse→mutate→write round-trips over the k8s
  output in one function.
- Hidden env side channel: `generate_artifacts` poisons `os.environ` with
  service shas so `_placeholder_value` can read them; `verbs/score.py`
  does the same with `PATH`.
- The `$`-doubling configs-inliner is implemented three times
  (compose.py edge Caddyfile, publish.py file-sourced configs, score.py
  bind inlining); only the score copy has basename-collision teeth.
- The workload/compose lenses were a halfway lens (documents.py, since
  dissolved by parallel work: `WorkloadDocument` → config.py, compose
  lenses → score.py): `WorkloadDocument` exposes one property, so every
  nontrivial consumer drops to raw `Any` dicts; the
  `consumer-interpolated` target-path invariant lives in the reader's
  head.
- Refusal taxonomy chaos: `SystemExit` vs `RuntimeError` with no law, mixed
  within one bundle loop.
- Two YAML dump spellings (`dump_compose` width 1M vs `yaml.safe_dump`
  width 1000 in `_rendered_workloads`); the placeholder grammar is spelled
  three times (`PLACEHOLDER`, `COMPOSE_HOLE`, config's `ENV_NAME_PATTERN`).
- `assert config.root is not None` ×3; `verbs/score.py` drops curl tarballs
  into the repo-root cwd; target/k8s fan-out logic duplicated between verb
  and library.

## The obviously-correct set

1. No new modules (D7): score.py stays one file — markers, provisioner
   compiler, secrets, compose lenses, both rendering pipelines, the
   template registry, and the D3 seeding all remain in it. config.py
   gains only the two cycle-forced primitives (below).
2. Delete the dead state.yaml writes and the path-reduction pass (D3 may
   delete the read too).
3. The `Yaml` alias lives in config.py beside `is_mapping`/`is_list`
   (score.py imports config; the alias cannot live score-side), and the
   predicates generalize in place to `TypeIs[dict[str, Yaml]]`/
   `TypeIs[list[Yaml]]`; every tree walker's `Any`/`object` becomes
   `Yaml`; one dump spelling.
4. Grow the lenses at their homes: config.py's `WorkloadDocument` gains
   typed containers (files, probes, annotations) with the D5 strict types
   and the D6 all-reader validators; the annotation vocabulary constants
   move from score.py to config.py beside the model; score.py's compose
   lenses grow into the D2 round-trip model; the ladder in
   `generate_compose` collapses to template registration.
5. `double_dollars` in config.py is the one home of the `$`-doubling law
   (the compose Caddyfile, publish's file-sourced configs, and score's
   bind inlining all call it); the surrounding configs-entry assembly
   stays per-consumer — the three input shapes differ (authored lines,
   `file:` conversion, bind-volume references) and a shared inliner
   would parameterize everything and fit nothing.
6. Kill the env side channel: thread service shas and
   `STORAGE_CLASS_VAR` explicitly; `bash(..., env=…)` carries the PATH
   prepend.
7. Grammar consolidation: `PLACEHOLDER`/`COMPOSE_HOLE` move beside
   `ENV_NAME_PATTERN` in config.py.
8. `model_validate(..., context={"source": …})` plumbing dies: a small
   `validate(model, value, source)` wrapper catches `ValidationError` and
   raises source-prefixed `ValueError` (D4's channel).
9. `root` explicit on pipeline signatures; the asserts die.
10. Smalls: curl tarballs into the per-run temp bin; target/k8s fan-out
    decided in one place; lock.py's crane manifests become typed OCI
    index/manifest models; config.py's `tool.uv` read gets a pyproject
    model; `verbs/score.py` drops its duplicated fan-out.
11. D2/D6 may widen or reorder this set; nothing in it is blocked by an
    open decision.

## Decision points

### D1 — Typing depth at third-party boundaries

Ruling: (a) — models where the devkit interprets or authors; a recursive
`type Yaml = str | int | float | bool | None | list[Yaml] | dict[str, Yaml]`
alias (PEP 695) types the passthrough boundary. Every literal `Any`/
`object` outside `optional_value_option.py` disappears; the passthrough is
typed as what it is (arbitrary YAML), never modeled. Rationale: full
vendor-schema modeling drags in surfaces we never read and cannot control,
and pydantic dumps declared fields before extras, so round-tripping
third-party docs reorders keys — at the provisioner boundary that breaks
the pinned authored-order law regardless. The `Yaml` alias enables the
type checker where `Any` disabled it; it validates nothing at runtime.

### D2 — Committed-artifact reordering

Ruling: (a) — typed round-trip. The generated compose document parses into
the typed model, all post-passes run as typed transformations, and the
model dumps exactly once per document. Pydantic's declared-fields-first
dump order becomes the artifacts' deterministic key order; every consumer
takes a one-time `stack/generated/**` reordering diff, regenerated and
committed in the same wave as the devkit version bump (the Plan A
catch-up-wave playbook). The lens-plus-raw-mutation alternative was
rejected: it preserves bytes only by keeping mutations outside the type
system permanently. k8s artifacts are unaffected (documents ride verbatim;
D3 pre-seeding would keep it that way).

### D3 — k8s instance-suffix determinism

Ruling: (a) — pre-seed. Between `init` and `generate`, write a state.yaml
recording each workload with its deterministic `InstanceSuffix` (`-` +
first 10 hex of sha256(name)); names come out deterministic by
construction and the entire correction tail dies (substring replace, state
reads, `ScoreState`/`WorkloadState`, the dead path-reduction pass). The
coupling to score-k8s's state format is accepted because the tool version
is pinned per config; the implementing session opens with a spike (init →
hand-seed state.yaml → generate → inspect names) and falls back to (b)
— parse-mutate-dump over string scalars of the manifest tree — only if the
pinned version ignores pre-seeded extras. (b) outright was rejected: it
retains the state read and suffix machinery and adds a heuristic walker
over fields the devkit does not own; (c) status-quo text replace is the
audited smell.

### D4 — Error-channel law

Ruling: bare `ValueError` everywhere. Every authored-input failure is
ValueError-family — the stdlib semantics of "a pure function says nope,
not what I take" (and `pydantic.ValidationError` already subclasses it).
All explicit `SystemExit`/`RuntimeError` raise sites in library code
become `raise ValueError(...)` with byte-identical message texts; the
`validate()` wrapper (#8) converts `ValidationError` and `yaml.YAMLError`
at read boundaries into source-prefixed `ValueError` so malformed YAML
gets the same clean channel; the verb boundary catches `ValueError` →
one-line stderr + exit 1. `typer.BadParameter` keeps the CLI-flag layer
(`--target k8s` with no k8s section). Bug-class exceptions keep
tracebacks; the boundary cannot distinguish a stray bug-raised
`ValueError` from an input one — accepted tradeoff. Tests retarget their
`pytest.raises` spellings mechanically; AGENTS.md refusal lines re-anchor
in the same commits. A named `ValueError` subclass was considered and
declined.

### D5 — Strictness at the lens edge

Ruling: (a) — strict at parse. `WorkloadMetadata.name: str | None`,
`annotations: dict[str, str]` (the Score spec's own grammar); spec-illegal
values refuse loudly at the lens with file and field named, regardless of
whether the devkit interprets the field. Same ruling the config schema
already applies to itself (`extra="forbid"`). Strict-on-use was rejected:
it keeps gratuitous `object` in the lens and makes lens validation
inconsistent per field.

### D6 — Validation-scope ripple

Ruling: (a) — one validity, all readers. The annotation validators live on
`WorkloadDocument` and fire at every parse: a workload file that violates
a deployment-surface law refuses `up` as well as `generate-score`, with
the same message. A file valid for one verb and invalid for another is a
distinction nobody can explain; the ripple only lands on repos whose
committed artifacts are already un-generatable. Carve-out: the
dev-toggles-on-k8s-only-workload refusal is a rendering-placement law,
not intrinsic document validity — it stays at its consumer in the k8s
path. Generation-scoped validation was rejected: it keeps the ladder in a
function and invents a lens-valid vs surface-valid split.

### D7 — Module homes

Ruling: zero new modules — score.py stays one file. Everything
score-flavored remains in it: the marker grammar and resolvers, the
provisioner compiler, secrets, the compose lenses (the D2 round-trip
model grows here), both rendering pipelines, the patch-template
registry, and the D3 seeding; internal order follows the repo's existing
laws (classes at top root → leaves, callers-before-callees). config.py —
already the package's shared leaf (`is_mapping`/`is_list`,
`ENV_NAME_PATTERN`, `chart_lock_key`, `EDGE_SERVICE_NAME`) — gains
exactly the two primitives the import graph forces out of score.py (it
is a cycle for compose.py or config.py to import score.py): the `Yaml`
alias (needed by config's own predicates) and `double_dollars` (needed
by compose's Caddyfile edge stanza). A concern-per-module split
(provisioners.py/secrets.py/markers.py/yamlio.py) was considered and
declined: the test seams import score.py fine, AGENTS.md sections
describe laws rather than files, and the repo's own recent direction is
consolidation by ownership (documents.py was dissolved, not multiplied).
Recorded trigger: if score.py remains massive after the work lands, the
split discussion reopens.
