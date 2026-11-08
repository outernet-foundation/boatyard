# boatyard

Docker-stack lifecycle verbs for any repo shipping a compose graph: `up` derives the whole
assembly from the root `boatyard.yml`. No Python API — the contracts are the verb CLI, the
declaration schema (`boatyard.yml` + `boatyard.lock.yml`), and the committed artifact formats.

## Design treaties

Authored intent and silent-failure traps. Everything that fails loudly (a refusal with a named
remedy) is documented by the code and deliberately not restated here.

- Three classes of fact: authored in the config, registry-resolved in the lock, tree-derived
  (`tree-<hash>`) computed on demand. Nothing derived is committed except
  `boatyard.k8s-pins.yml`, written only by the `bump` verb (rollback = `git revert`, never
  hand-edit). The lock is written only by the `lock` verb and `build`'s incremental freeze —
  never hand-authored.
- Fresh-repo assumption: no code handles an obsolete format. A past artifact gets the plain
  parse refusal, never a migration path.
- github-actions-toolkit owns the consumer workflow contract and is never a project dependency.
  From 1.0 on, breaking changes ship with a manually bumped `major_minor`.
- Third-party images and charts are consumed through the org mirror namespace (upstream
  manifests get GC'd). CI is the single writer — engineers never push, and error text never
  teaches them to.
- Substrate split: compose renderings bake the lock's per-platform manifests (the exact bytes
  one machine runs); k8s renderings bake the index form (kubelet resolves per node).
  `helm template` is a renderer, never a deployer.
- `boatyard.values.yaml` (consumer root, gitignored) is the only machine-values source; the
  ambient shell fills nothing. `up` refuses without it; `down` and score generation run without
  it. `down` reads no values, no lock, no selection — a name-only root plus
  `--remove-orphans`.
- The edge does no token validation; re-protection is a bundle-shipped oauth2-proxy sidecar
  treaty.
- `$`-doubling: a literal `$` in file text placed inside a compose document (edge Caddyfile,
  file-sourced configs, score's bind inlining) is doubled via `double_dollars` in
  `config/config.py` — the one home. Text outside a compose document (k8s manifests) never
  doubles.
- `secrets:` absent refuses at generate-score; `secrets: {}` states zero — absence and emptiness
  carry different meaning throughout.
- Silent trap: plain score-compose silently drops `httpGet` probes; boatyard's lens
  (`outernet.internal/probe-adapter` on the workload) refuses loudly instead. Substrate needs
  live in workload files as `outernet.internal/*` annotations — `boatyard.yml` never carries a
  per-workload runtime fact; the shipped `score_patch_templates` are the reader, and the lens is
  `WorkloadDocument.parse(raw, file_name)`.

## Commands

Quick loop: `uv run pytest`, `uv run ruff check`. Full gate:
`uv run --locked scripts/preflight.py`.

## Placement conventions

- One module per verb under `verbs/`, each with a local bare `app = typer.Typer()`; `cli.py`
  composes them with nameless `app.add_typer(...)` (a named `add_typer` nests a group).
- `extra="forbid"` everywhere in pydantic models; per-field shape in `field_validator`s,
  cross-field shape in one `model_validator(mode="after")` per model.
- Every `load()` takes a full path and goes through `load_yaml_model` (`yaml.py` strict load).
  One exception: `LockFile.load(path, config)` reads a missing lock as the empty lock.
- Model modules order the root record first (`RootModel` subscripts quote forward refs) and run
  one-way: `dockerfiles` ← `config/config` ← `config/lock` ← `lock`.
- Nothing is caught only to be re-raised as a different exception; guards and third-party
  exceptions surface raw with full traceback; `typer.BadParameter` owns the CLI-flag layer.
- The build set is derived from the Dockerfile tree (`derive_services` in `dockerfiles.py`),
  never authored in config.
- Authored provisioner files carry real YAML mappings and lists; the string form is generated. A
  field already in string form is a validation error.

## Modules

- `config/config.py` — schema + load (`ConfigFile.load`, `ConfigFile.resolve_services(choices)`);
  shared leaf for slot markers, name joins, `double_dollars`, workload lens.
- `config/lock.py` — lock schema; `LockFile.load(path, config)` is the only parser consumers use.
- `config/values.py` — values file, selection and override validation, the `assembly()` plan
  `up` renders from.
- `config/k8s_pins.py` — pins parsing.
- `dockerfiles.py` — Dockerfile-derived build set, tree-tag sha machinery.
- `lock.py` — relock engine + crane resolution.
- `compose.py` — the compose invocation spelling.
- `build.py` — build pipeline.
- `markers.py` — provisioner and chart-value marker resolution.
- `score.py` / `score_compose.py` / `score_k8s.py` / `score_generate.py` — shared base / compose
  rendering / k8s rendering / generation verb.
- `publish.py` — publish preparation.
- `crane.py` — pinned crane installer.
- `paths.py` — consumer-repo document paths.
- `temp.py` — shared digest/platform/tree-tag regex spellings.
- `yaml.py` — strict YAML load + model load.
- `verbs/up.py` — assembly rendering, the per-run dotenv, unwind on failure.

## This file

The code is the source of truth for mechanism. Admit a line only if removing it would cause a
specific mistake — a loud refusal documents itself, so it is never restated here. Search before
adding; one fact, one home; identifiers and refusal phrasings stay exact.
