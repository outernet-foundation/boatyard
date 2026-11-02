# score.py refactor (decision session)

The 2026-11-02 audit session found score.py carrying five unrelated concerns
in one 828-line module. This file records the audit, the obviously-correct
change set, and the decision points D1–D7. This session locks the decisions;
the next session implements this file and does not redecide.

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
- documents.py is a halfway lens: `WorkloadDocument` exposes one property,
  so every nontrivial consumer drops to raw `Any` dicts; the
  `consumer-interpolated` target-path invariant lives in the reader's head.
- Refusal taxonomy chaos: `SystemExit` vs `RuntimeError` with no law, mixed
  within one bundle loop.
- Two YAML dump spellings (`dump_compose` width 1M vs `yaml.safe_dump`
  width 1000 in `_rendered_workloads`); the placeholder grammar is spelled
  three times (`PLACEHOLDER`, `COMPOSE_HOLE`, config's `ENV_NAME_PATTERN`).
- `assert config.root is not None` ×3; `verbs/score.py` drops curl tarballs
  into the repo-root cwd; target/k8s fan-out logic duplicated between verb
  and library.

## The obviously-correct set

1. Split score.py along the tested seams: `provisioners.py` (markers +
   compiler + `AuthoredProvisioner`/`LockImageSpec`), `secrets.py`, a YAML
   layer module (D7 names it), score.py keeps both rendering pipelines.
2. Delete the dead state.yaml writes and the path-reduction pass (D3 may
   delete the read too).
3. One YAML layer: a recursive `type Yaml` alias replaces `Any`/`object` in
   every tree walker; one dump spelling; `is_mapping`/`is_list` generalize
   to `TypeIs[dict[str, Yaml]]`/`TypeIs[list[Yaml]]`.
4. Grow the lenses: `WorkloadDocument` gains typed `containers` (files,
   probes, annotations); the five annotation checks become validators on
   the model; the 60-line ladder in `generate_compose` collapses to
   template registration.
5. One shared `$`-doubling configs-inliner, one home, three call sites.
6. Kill the env side channel: thread service shas and
   `STORAGE_CLASS_VAR` explicitly; `bash(..., env=…)` carries the PATH
   prepend.
7. Grammar consolidation: `PLACEHOLDER`/`COMPOSE_HOLE` move beside
   `ENV_NAME_PATTERN` in config.py.
8. `model_validate(..., context={"source": …})` plumbing dies: a small
   `validate(model, value, source)` wrapper catches `ValidationError` and
   prefixes the source.
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

Status: OPEN.

Verified: score-k8s has no CLI flag for deterministic suffixes; the suffix
lives in `state.yaml` (`WorkloadExtras.InstanceSuffix`). Options:
(a) pre-seed state.yaml with deterministic suffixes between `init` and
`generate` — determinism by construction, the rewrite tail dies; couples
to score-k8s's state format (pinned tool version makes it testable);
(b) keep post-rewrite but parse-mutate-dump typed; (c) status quo string
surgery. Lean: (a), (b) as fallback if the pinned version ignores
pre-seeded extras.

### D4 — Error-channel law

Status: OPEN.

`SystemExit` vs `RuntimeError` is unmotivated noise. Candidate law: a
typed refusal exception raised by the library, caught at the verb boundary
→ one-line message + exit 1. Open sub-questions: does every library
refusal convert (config.py's pydantic ValueErrors too — repo-wide), and do
message texts stay byte-identical? Lean: typed exception everywhere,
message texts unchanged.

### D5 — Strictness at the lens edge

Status: OPEN.

`metadata.name: str | None`, `annotations: dict[str, str]`: parse-refuse
spec-forbidden garbage (non-string names/annotations) vs passthrough of
uninterpreted fields. Lean: strict.

### D6 — Validation-scope ripple

Status: OPEN.

Moving annotation validation onto `WorkloadDocument` makes it fire for
every reader: `up`/`select` parse workload documents too, so a bad probe
annotation that today only fails `generate-score` would fail `up` as
well. Lean: accept the wider, earlier refusal.

### D7 — Module homes

Status: OPEN.

`provisioners.py` + `secrets.py` are obvious. Open: annotations as their
own module vs validators on `documents.WorkloadDocument` with the template
registry in score.py; the YAML layer as `yamlio.py` vs growing
`documents.py`; whether score.py splits per pipeline. Lean: validators on
WorkloadDocument, registry in score.py, `yamlio.py`, pipelines stay
together.
