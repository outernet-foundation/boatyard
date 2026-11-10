# boatyard

Docker-stack lifecycle verbs for any repo shipping a compose graph: `up` derives the whole
assembly from the root `boatyard.yml`. No Python API — the contracts are the verb CLI, the
declaration schema (`boatyard.yml` + `boatyard.lock.yml`), and the committed artifact formats.

## Design treaties

Design intent and silent-failure traps, one fact per bullet.

- Three classes of fact: authored in the config, registry-resolved in the lock, tree-derived
  (`tree-<hash>`) computed on demand. Nothing derived is committed except
  `boatyard.k8s-pins.yml`, written only by the `bump` verb — or seeded once with placeholder
  pins by operator ruling where a pre-merge gate needs the k8s leg; the first bump overwrites
  the file wholesale (rollback = `git revert`, never hand-edit). The lock is written only by
  the `lock` verb and `build`'s incremental freeze — never hand-authored.
- Fresh-repo assumption: no code handles an obsolete format. An obsolete artifact gets a plain
  parse failure, never a migration path.
- github-actions-toolkit owns the consumer workflow contract and is never a project dependency.
  From 1.0 on, breaking changes ship with a manually bumped `major_minor`.
- Third-party images and charts are consumed through the org mirror namespace (upstream
  manifests get GC'd). CI is the single writer — engineers never push, and error text never
  teaches them to. The one exception is boatyard's own machinery: the barrier and gateway
  caddy pins live in boatyard's code as upstream tags, never in a consumer lock or mirror
  (the barrier rides the global `BOATYARD_BARRIER_IMAGE` slot, minted at resolve; the
  gateway document renders its pin literally); boatyard hosting its own mirror retires the
  exception.
- Substrate split: compose renderings bake the lock's per-platform manifests (the exact bytes
  one machine runs); k8s renderings bake the index form (kubelet resolves per node).
  `helm template` is a renderer, never a deployer.
- `boatyard.values.yaml` (consumer root, gitignored) is the only machine-values source; the
  ambient shell fills nothing. `up` requires it; `down` and score generation run without it.
- `down` reads config only and derives every candidate project name from the config's full
  entry set plus the bare project name, removing the shared network last.
- The `boatyard-gateway` service does no token validation; re-protection is a bundle-shipped
  oauth2-proxy sidecar treaty.
- `$`-doubling: a literal `$` in file text placed inside a compose document (edge Caddyfile,
  file-sourced configs, score's bind inlining) is doubled via `double_dollars` in
  `models/config_file.py` — the one home. Text outside a compose document (k8s manifests) never
  doubles.
- Consumed facts are walked from generated output, never re-derived from sources: the mint
  list (`secrets.txt`) is the k8s rendering's consumed secret refs (no secret vocabulary is
  authored anywhere; a compose-only rendering mints an empty list), and a local entry's values
  demand is its rendered bundle artifact's own `${NAME}` slots.
- A service group derives its option set from the single axis its workloads' image closure
  declares (group-level `workloads:`; a closure declaring no axis or two axes refuses) or authors
  it (`options:`); the axis lives on the local `base_images` entry — the one spelling across
  the Dockerfile ARG and the k8s selection, never re-described by hand.
- Plain score-compose silently drops `httpGet` probes; boatyard's lens makes the omission loud
  instead via the workload's `outernet.internal/probe-adapter` annotation.
- Substrate needs live in workload files as `outernet.internal/*` annotations — `boatyard.yml`
  holds no per-workload runtime fact. The shipped `score_patch_templates` are the reader; the
  lens is `WorkloadDocument.parse(raw, file_name)`.

## Commands

Quick loop: `uv run pytest`, `uv run ruff check`. Full gate:
`uv run --locked scripts/preflight.py`.

## Placement conventions

- One module per verb under `verbs/`, each with a local bare `app = typer.Typer()`; `cli.py`
  composes them with nameless `app.add_typer(...)` (a named `add_typer` nests a group).
- `extra="forbid"` everywhere in pydantic models; per-field shape in `field_validator`s,
  cross-field shape in one `model_validator(mode="after")` per model.
- Every `load()` takes a full path. YAML kinds go through `load_yaml_model` (`yaml.py` strict load);
  the image set derives inside `ConfigFile.load` over the declared dockerfile edges (workload
  source annotations, provisioner markers, local entries); `LockFile.load(path, config)` reads a
  missing lock as the empty lock.
- Every pydantic-modeled file kind owns exactly one module in `models/` (boatyard.yml, the lock,
  values, pins, workloads, the pyproject lens). Each module's types run root down, root
  record first, quoting forward refs where evaluation allows (`RootModel` subscripts;
  `default_factory` leaves go through a module-level factory). Modules run one-way:
  `image`/`workload`/`pyproject` ← `config_file` ← `lock_file` ← `lock`.
- Nothing is caught only to be re-raised as a different exception; guards and third-party
  exceptions surface raw with full traceback; `typer.BadParameter` owns the CLI-flag layer.
- The build set is the closure over declared edges — workload source annotations, provisioner
  `dockerfile:` markers, local `base_images` entries — computed inside `ConfigFile` load, never
  authored in config and never walked from the tree; a Dockerfile nothing declares is silently
  not built, like an unlisted score file. Every workload file loads exactly once per
  `ConfigFile.load` — one lens serves the declared edges, the derived variants, and both renderings;
  the per-entry reload is dead, and the compose-only probe-adapter check runs at the compose
  rendering over the once-loaded documents.
- Authored provisioner files carry real YAML mappings and lists; the string form is generated. A
  field already in string form is a validation error.

## Modules

- `models/config_file.py` — schema + load (`ConfigFile.load`); shared leaf for slot markers, name
  joins, `double_dollars`.
- `models/lock_file.py` — lock schema; `LockFile.load(path, config)` is the only parser consumers use.
- `models/values_file.py` — the values file schema: typed overrides, one flat values section, the
  key and reference grammar.
- `models/k8s_pins.py` — pins parsing.
- `models/pyproject.py` — the toolchain-pin lens over the consumer repo's pyproject.toml.
- `models/workload.py` — the workload-file lens (`WorkloadDocument`), the workload loader and
  the compose-surface probe-adapter check, plus provisioner-file selection.
- `models/image.py` — the image and local-base models, the Dockerfile parse, and the tree-tag
  sha machinery.
- `resolve.py` — the `resolve(config, lock, values)` fold answering the assembly plan `up` renders.
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
- `yaml.py` — strict YAML load + model load; `Yaml` and the `is_mapping`/`is_list` guards.
- `verbs/up.py` — per-bundle compose projects plus the gateway project over the shared
  network, the override documents, the per-run dotenv, unwind on failure.

## This file

The code is the source of truth for mechanism; this file exists to prevent specific mistakes,
and every bullet names the mistake it prevents — a line whose mistake cannot be named does not
belong here.

- Nothing else lives here: mechanism belongs in the code it describes, a one-off procedure in
  `design/`, another repo's fact in that repo's own doc, and refusal behavior nowhere — a loud
  refusal's error text is its documentation. The string `refus` appears nowhere outside this
  section.
- The shape is fixed, in order: identity paragraph, `Design treaties`, `Commands`,
  `Placement conventions`, `Modules`, this section. One fact per bullet, plain sentences —
  compression and shorthand are how wrong lines survive review. Lines wrap at or before column
  100. `Modules` mirrors `src/boatyard/` exactly: one line per module, no function inventories,
  no parenthetical laws.
- Treaties and conventions trade one for one: a new bullet replaces a weaker one, it never
  merely appends. Growing this file is a human decision, not a session's.
