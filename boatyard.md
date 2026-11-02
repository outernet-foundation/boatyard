# boatyard — the docker-devkit rename and CLI refactor

Execution doc for the rename of `docker-devkit` to `boatyard`, plus the four CLI refactors
decided with it. Decisions are locked (session 2026-11-02); the PyPI name is registered
(phase 0), no code has been touched yet.
A single session executes this file top to bottom; phase order is load-bearing. Consumers
were verified pinned at `docker-devkit==0.4.9.dev165` (the last published PyPI version)
across placeframe, Make-it-Sing, lbe-toolkit, infra-rathole, and placeframe-capture-tool —
all five are already stale against the refactor-era code, so the sweep below is one
coordinated migration, not a disruption of working consumers.

## Decisions (locked)

| Surface | Old | New |
| --- | --- | --- |
| CLI command | `up`, `down`, `build`, `lock`, `tree-tags`, `mirror`, `publish-compose`, `generate-score`, `docker-build-matrix` (9 console scripts) | single `boatyard` command, same 9 verbs as subcommands |
| PyPI package | `docker-devkit` | `boatyard` (registered by the operator 2026-11-02 — only a registration attempt proves availability, see phase 0) |
| Import / package dir | `docker_devkit` (`src/docker_devkit/`) | `boatyard` (`src/boatyard/`) |
| Config file | `docker-devkit.yaml` | `boatyard.yml` |
| Lock file | `docker-devkit.lock` | `boatyard.lock.yml` |
| Version | 0.5.9 | 0.6.0 (continues the line through the rename) |
| GitHub repo | `outernet-foundation/docker-devkit` | `outernet-foundation/boatyard` (GitHub auto-redirects old URLs) |

The four refactors riding the rename:

1. **Delete `@entry`** (`src/docker_devkit/verbs/entry.py`). The verb boundary that converted
   `ValueError` to one-line stderr + exit 1 is gone: during development the author wants full
   tracebacks always. Library code KEEPS the `ValueError`-with-byte-stable-text convention
   (tests assert on `str(result.exception)`, which yields the message either way). A
   presentation layer may return if external users ever appear.
2. **Remove both typer flag suppressions.** `typer.Typer(add_completion=False,
   pretty_exceptions_show_locals=False)` becomes bare `typer.Typer()` — completion installs
   once for all subcommands; tracebacks show locals (revisit if CI logs ever go public:
   `REGISTRY_TOKEN`/`REGISTRY_USERNAME` sit in scope in the publish verb).
3. **Split `--push`.** `--push <addr>` (optional-value) was implemented by
   `optional_value_option.py`, a private-API seam into typer's vendored click — delete the
   whole module. Replacement: `--push` (bool, push to the config's `repository`) and
   `--push-to <addr>` (push to that registry), mutually exclusive, public typer API only.
4. **Single app.** One `typer.Typer()` in a new `src/boatyard/cli.py`; each verb module
   exports a plain function (no per-module `app` object, no decorators); `[project.scripts]`
   collapses to `boatyard = "boatyard.cli:app"` (the `:app` typer law).

Clean break: no backward-compat shims for the old bare-command console scripts. Naming
rationale: the container ecosystem already speaks nautical (`docker`, `helm`, `harbor`,
`kubernetes` = Greek helmsman); a boatyard assembles vessels from parts and launches
them — the tool's own framing is "a deploy-files definition language and compiler, plus
a runtime for the compose target." `boatyard.yml`/`boatyard.lock.yml` use `.yml` (syntax
highlighting on the extensionless lock; org spelling is `.yml` elsewhere —
`publish.yml`). Rejected names: `shipyard` and `dockyard` (PyPI-prohibited — phase 0);
`drydock`, `slipway`, `berth`, `gantry`, `mooring`, `marina`, `wharf`, `quayside` (all
live on PyPI).

## Phase 0 — secure the name (operator-assisted)

Sandbox cannot push or publish; hand branch/SHA to the operator for everything below.

1. `boatyard` registered on PyPI by the operator, 2026-11-02 — done. The registration
   attempt is the only real availability check: a simple-index 404 means "no published
   release", not "registrable". First choice `shipyard` 404s yet refuses registration
   ("This project name isn't allowed") — a 2008 project of that name existed, its
   releases were later removed, and PyPI prohibits such names without publishing a list
   or a reason. `dockyard` has the same existed-then-removed profile (releases vanished
   between July 2025 and November 2026) plus active container-ecosystem namesakes.
   `boatyard` passed the actual registration.
2. Rename the GitHub repo `docker-devkit` → `boatyard` (Settings → Rename). Old blob URLs
   (referenced from several consumer AGENTS.md files) redirect; consumers still update
   their links during the sweep.
3. Known namesake: docker-archive/boatyard, a Docker build web UI deprecated 2014,
   archived 2021. Defunct, never on PyPI, no trademark found — a footnote, not a
   blocker.

## Phase 1 — devkit code change

Execute on the current branch — no new branch. One commit per numbered step; AGENTS.md updates ride
their own prose commits, never mixed with code (see "Commit discipline").

### 1.1 Rename the package skeleton

- `git mv src/docker_devkit src/boatyard`.
- Every intra-package import `from docker_devkit...` → `from boatyard...` across `src/` and
  `tests/` (absolute-import law unchanged).
- Test package-data references: `files("docker_devkit")` → `files("boatyard")`
  (tests/test_score_generate.py, three sites around the patch_templates reads).
- `pyproject.toml`: `name = "boatyard"`, `version = "0.6.0"`, description updated to the
  compiler framing; `[tool.hatch.build.targets.wheel] packages = ["src/boatyard"]`;
   `[project.scripts]` collapses to the single line `boatyard = "boatyard.cli:app"`.
- `uv sync` to regenerate `uv.lock` under the new name.

### 1.2 Delete `@entry`

- Delete `src/boatyard/verbs/entry.py`.
- Drop `@entry` + its import from all 9 verb modules (the decorator stack
  `@app.command()` + `@entry` collapses with the single-app step anyway).
- Test audit — the 36 `assert isinstance(result.exception, SystemExit)` sites split into:
  - `ValueError` paths (were converted by `@entry` → `SystemExit`): retarget to
    `isinstance(result.exception, ValueError)`. `str(result.exception)` message assertions
    keep passing unchanged.
  - Explicit `raise SystemExit(...)` sites remain `SystemExit` — exactly two:
    `verbs/publish.py` ("REGISTRY_USERNAME and REGISTRY_TOKEN must be set...") and
    `verbs/score.py` ("score artifacts are stale..."). Tests covering those two keep their
    current assertion shape.
  - Verb-level `RuntimeError` raises (no-config, not-mirrored, tools-missing) already
    propagate uncaught today; their tests stay as-is.

### 1.3 Single app + flag defaults

- New `src/boatyard/cli.py`: bare `app = typer.Typer()` (no kwargs — completion on, locals
  on), importing each verb function from `boatyard.verbs.*` and registering
  `app.command()(up)` etc. Typer's default name-mangling keeps command names:
  `publish_compose` → `publish-compose`, `tree_tags` → `tree-tags`,
  `docker_build_matrix` → `docker-build-matrix`.
- Each verb module: delete its `app = typer.Typer(add_completion=False,
  pretty_exceptions_show_locals=False)` line and its `@app.command()` decorator; the module
  exports the plain function (Annotated signatures unchanged).
- Tests currently import the per-verb apps (e.g. `from docker_devkit.verbs.tree_tags import
  app, tree_tags`; `from docker_devkit.verbs import down, up`). Retarget every
  `runner.invoke(<verb>.app, args)` to `runner.invoke(app, ["<verb>", *args])` against
  `boatyard.cli`'s single app.

### 1.4 `--push` split

- Delete `src/boatyard/optional_value_option.py` (the `# noqa-wrapper:` seam module) and the
  `cls=DestinationCommand` + import from `verbs/build.py`.
- `build` signature becomes:
  - `push: Annotated[bool, typer.Option("--push", ...)] = False`
  - `push_to: Annotated[str | None, typer.Option("--push-to", ...)] = None`
- Flag-law refusals (all `typer.BadParameter`, message text updated to name both flags):
  - `--push` and `--push-to` together refuse (mutually exclusive).
  - `--dev` with either push flag refuses (dev builds are local-only).
  - `--push-to` address validation: identical rules to today's `--push <addr>` — no scheme,
    no trailing slash, host carries `.` or `:`.
  - `--push` (bool) with no declared `repository` refuses (today's
    "repository is required" refusal, retargeted).
- `bake.py` `run_build`: `push: str | None = None` (three-state) becomes
  `push: bool = False, push_to: str | None = None`; `pushing = push or push_to is not None`;
  `destination = push_to`. Everything downstream keys off `pushing`/`destination` — no
  further bake changes.
- The toolkit's CI shape is `build --push` (bool form) — unaffected by the split; only
  local `--push <addr>` invocations become `--push-to`.

### 1.5 Config/lock filenames, messages, self-check

Exact string replacements (byte-stable refusal text is a greppable contract):

| File | Old | New |
| --- | --- | --- |
| config.py | `DEFAULT_CONFIG_PATH = Path("docker-devkit.yaml")` | `Path("boatyard.yml")` |
| config_lock.py | `LOCK_PATH = Path("docker-devkit.lock")` | `Path("boatyard.lock.yml")` |
| config.py | `importlib.metadata.version("docker-devkit")` | `version("boatyard")` |
| config.py | `"docker-devkit {installed} does not satisfy requires={self.requires!r}"` | `"boatyard {installed} ..."` (rest identical) |
| config.py | `"docker-devkit.yaml must exist and declare services"` | `"boatyard.yml must exist and declare services"` |
| lock.py | `"no docker-devkit.yaml; no lock declarations to relock"` | `"no boatyard.yml; no lock declarations to relock"` |
| lock.py | `"...but docker-devkit.yaml declares no platforms"` | `"...but boatyard.yml declares no platforms"` |
| mirror.py | `"a docker-devkit.yaml with base_images (or k8s.charts) is required to mirror"` | `"a boatyard.yml with ..."` |
| tree_tags.py | `"no docker-devkit.yaml at the repo root"` | `"no boatyard.yml at the repo root"` |
| build.py | `"No docker-devkit.yaml at the repo root"` | `"No boatyard.yml at the repo root"` |
| up.py | `"--build requires build declarations (repository or base_images) in docker-devkit.yaml"` | `"... in boatyard.yml"` |
| publish.py | `"publishing requires local entries (workloads:) in docker-devkit.yaml"` | `"... in boatyard.yml"` |
| score.py | `"docker-devkit.yaml declares no local service entries and no k8s section"` | `"boatyard.yml declares ..."` |
| score.py | `"repository or base_images is required in docker-devkit.yaml"` | `"... in boatyard.yml"` |

`DEV_SENTINEL = "0.0.0.dev0"` bypass mechanics unchanged. Sweep for any remaining literal
with `rg -n "docker-devkit|docker_devkit" src tests` — expected zero after this step.

### 1.6 Tests + gate

- All fixture filenames: `(tmp_path / "docker-devkit.yaml")` → `"boatyard.yml"`;
  `(tmp_path / "docker-devkit.lock")` → `"boatyard.lock.yml"` (test_config, test_compose,
  test_mirror, test_service_builds, test_score_generate, test_tree_tags).
- Message assertions updated to the new refusal text (e.g. test_tree_tags.py's
  `"no docker-devkit.yaml"` → `"no boatyard.yml"`).
- Full gate: `uv run --locked scripts/preflight.py` (wraps ruff check + format --check,
  basedpyright, deptry src, pytest, lock/ruff-sync checks). Quick loop: `uv run pytest`,
  `uv run ruff check`.

## Phase 2 — repo docs pass

- **AGENTS.md rewrite** (prose commits, separate from code): error-channel law replaced —
  no verb boundary, library `ValueError` byte-stable convention stands, CLI tracebacks
  include locals, `typer.BadParameter` still owns flag errors, the two explicit
  `SystemExit` sites stay; verb table respelled `boatyard <verb>` with `build`'s new
  `--push`/`--push-to` flags; module map drops `verbs/entry.py` and `optional_value_option.py`,
  adds `cli.py`; every `docker-devkit.yaml`/`docker-devkit.lock` mention becomes
  `boatyard.yml`/`boatyard.lock.yml`; completion note (`--install-completion` once);
  keep the 200-line/26KB budget law.
- **Plan-doc prune**: `refactor-plan.md` is executed (delete). Review and delete or prune
  `plan-apt-hermeticity.md`, `plan-mirror-digest-retention.md`, `score-refactor-plan.md`,
  `score-refactor.md`, `plan-artifact-identity.md`, `plan-artifact-identity-implementation.md`
  against git history — anything already implemented goes; what remains must match the
  actual world. `plan-consumer-contract.md` (ADR D1: no Python API) stays load-bearing.
- **`plan-consumer-contract-implementation.md`**: fold this rename into its remaining
  release + catch-up wave — the sweep below IS that wave's checklist, superseding the
  per-phase consumer lists there where they overlap (its standing constraints still bind:
   CLI/declarations/artifacts only, nothing consumer-side imports `boatyard`).
- README.md: name, framing ("a deploy-files definition language and compiler, plus a
  runtime for the compose target"), commands.

## Phase 3 — release 0.6.0

Operator: push branch, merge, tag through the normal release flow (the toolkit's binding
owns it); PyPI publish of `boatyard` 0.6.0 rides the normal flow — the name is already
registered, so there is no placeholder race. One release, one consumer pin — nothing
consumer-facing ships separately.

## Phase 4 — github-actions-toolkit

- `src/github_actions_toolkit/config.py`: `DOCKER_DEVKIT_CONFIG_PATH =
  Path("docker-devkit.yaml")` → `Path("boatyard.yml")` (plain-YAML presence probe; the
  resolver-cycle law keeps it import-free).
- Workflow YAMLs: every `uv run --locked <verb>` → `uv run --locked boatyard <verb>`
  (`build --push`, `mirror`, `publish-compose --plan`, `generate-score --check`,
  `docker-build-matrix`).
- **Fix the `--lock-only` ghost**: `uv run --locked build --lock-only --check` →
  `uv run --locked boatyard lock --check` (flag removed from `build` long ago; the `lock`
  verb owns `--check`).
- Tests: the three fixtures writing `docker-devkit.yaml` (test_prerelease.py, 
  test_run_releases.py, test_config.py) → `boatyard.yml`.
- AGENTS.md: the config-file law line ("...docker-devkit are root `.yaml`") → boatyard,
  root `.yml`; the domain-verb invocation spelling throughout.

## Phase 5 — consumer sweep (per repo)

Common to all five (order within a repo: code → lockfiles → prose):

1. `pyproject.toml` dependency: `docker-devkit==0.4.9.dev165` (or the scripts-package
   variant) → `boatyard==0.6.0`; `uv lock --upgrade-package boatyard`.
2. `git mv docker-devkit.yaml boatyard.yml`; `requires:` floor → `>=0.6.0`.
3. `git mv docker-devkit.lock boatyard.lock.yml` (content unchanged).
4. `.gitattributes`: `docker-devkit.lock merge=ours` → `boatyard.lock.yml merge=ours`.
5. AGENTS.md/README: `docker-devkit` → `boatyard` / `boatyard.yml` / `boatyard.lock.yml`;
   `uv run <verb>` → `uv run boatyard <verb>`; GitHub doc links to the renamed repo.
6. **`--lock-only` is a dead flag everywhere it appears** — rewrite to `boatyard lock`
   (plus `--upgrade` where the old text said `--lock-only --upgrade`).

Per-repo specifics:

- **placeframe** — dep in `scripts/pyproject.toml` (`docker-devkit>=0.4.9.dev165`). Doc
  surfaces: root AGENTS.md (command catalog, uv-run law), `scripts/AGENTS.md` (devkit
  pointer + the `docker_devkit.host.parse_env_file` import note — see the toolkit-side
  import below), `stack/AGENTS.md` (heaviest: config/lock mentions, the `uv run up/down`
  lines, the manual compose spelling that names `docker-devkit.lock` as an env-file),
  `workloads/AGENTS.md` (lock references), `.github/workflows/AGENTS.md` (mirror/build/
  publish pipeline descriptions). `scripts/` imports `docker_devkit.host.parse_env_file`
  in `api_auth.py` — migrate that single import to the CLI/artifact contract (ADR D1: no
  Python API) or to a local parse.
- **Make-it-Sing** — root `pyproject.toml` dep; standard renames.
- **lbe-toolkit** — root `pyproject.toml` dep; standard renames.
- **infra-rathole** — dep in the dev group; README.md carries `uv run build --lock-only`
  (→ `uv run boatyard lock`) and the tree-layout table naming both files; `requires`
  floor in config. AGENTS.md already documents the devkit pin law.
- **placeframe-capture-tool** — the big one. Deps in BOTH root `pyproject.toml`
  (`==0.4.9.dev165`) and `scripts/pyproject.toml` (`>=0.1.15`). The Python-import
  migration: `scripts/src/scripts/install_zed/` imports `docker_devkit.config.
  {DockerDevkitConfig,load_config,LOCK_PATH}`, `docker_devkit.documents.
  {compute_service_shas,render_bake}`, `docker_devkit.host.{host_platform,
  parse_env_file}` (orchestrator.py, box_install.py, constants.py) — all against the
  0.4.9-era API, already broken. Migrate to the CLI contract per ADR D1: bake via
  `boatyard build`, shas via `boatyard tree-tags`, lock reads via the committed
  `boatyard.lock.yml` artifact (env-file form), env parsing local to the consumer.
  AGENTS.md + scripts/AGENTS.md carry many `--lock-only` and `uv run build` spellings;
  the zed-aoa-diagnosis-handoff.md is a status record — update only its still-live
  instructions.

## Phase 6 — optional deprecation signpost

Precedent: `stack-toolkit` ≤0.1.1 deprecation signposts. Optionally publish a final
`docker-devkit` release whose description points at `boatyard`. Skip if unwanted — the
name goes dormant on its own.

## Verification

- After 1.6: full preflight green; `rg -n "docker-devkit|docker_devkit|@entry|DestinationCommand" src tests`
  returns nothing.
- After each consumer: `uv run --locked boatyard --help` resolves; `uv run pytest` for that
  repo's battery where present; a human `up`/`down` smoke on one repo before declaring the
  sweep done.
- After phase 4: the toolkit's own test suite green.

## Commit discipline

Commit per step boundary; prose (AGENTS.md/README/plan docs) and code never share a
commit; one-line subjects under 72 chars naming the actual change; no trailers; squashing
via `--fixup` + `--autosquash` only against branch-local targets. Sandbox pushes and the
PyPI publish go through the operator.
