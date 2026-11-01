# Contract archaeology: dev165 → refactor head

The phase 1 record for `plan-consumer-contract-implementation.md` (ADR O1):
the full `dev-165..HEAD` contract diff across the three surfaces, the
re-verified library-import inventory, and the complete consumer-edit list
phase 9 executes. Findings beyond D9's enumeration are folded into that
plan's phase 9 text; this file holds the enumeration itself.

Diff span: `dev-165` (3406e1e, the 0.4.9.dev165 publication point) to
refactor HEAD (0.5.9, unpublished) — 112 commits, 47 files. Every consumer
resolves 0.4.9.dev165: placeframe through the `scripts/` workspace member's
`docker-devkit>=0.4.9.dev165` floor, the other four through exact
`docker-devkit==0.4.9.dev165` root pins (capture-tool additionally carries a
`docker-devkit>=0.1.15` floor in its `scripts/` member, satisfied by the root
pin).

## Surface 1 — the CLI

The verb set and every flag are unchanged. Seven entry points at both ends —
`up`, `down`, `build`, `mirror`, `publish-compose`, `generate-score`,
`docker-build-matrix` — with identical flags per verb: `up`
(`--attached/-a`, `--quiet-pull/-q`, `--build`, `--dev`), `down`
(`--volumes/-v`), `build` (`--push`, `--targets/-t`, `--gpu`, `--upgrade/-u`,
`--lock-only`, `--gpu-only`, `--no-cache`, `--dev`, `--check`), `mirror`
(none), `publish-compose` (`--bundle`, `--branch`, `--plan`),
`generate-score` (`--target`, `--local`, `--check`), `docker-build-matrix`
(none). Entry-point module homes moved only (`compose.py` → `verbs/{up,down}`
+ `assembly.py`, `build_docker.py` → `bake.py` + `verbs/build.py`,
`score_generate.py` → `score.py` + `verbs/score.py`, `publish_compose.py` →
`publish.py` + `verbs/publish.py`, `matrix.py` → `config.build_legs()` +
`verbs/matrix.py`, `mirror.py` → `verbs/mirror.py`). The matrix JSON schema
(`[{"targets", "variant"}]`), the `--plan` JSON, the `Digest …` publish
lines, and the `up`/`down` compose handoff shapes (`--pull missing`, profile
forwarding, lock as second `--env-file`) are byte-identical.

Behavioral deltas the refactor landed:

- **The compose.yml fallback died.** dev-165 `up`/`down` on a config-less or
  `services:`-less repo fell back to a root `compose.yml`; both verbs now
  refuse loudly (`assembly.require_compose_stack`). No consumer runs the
  verbs in a services-less repo today — zero consumer edits.
- **`up` prints nothing.** dev-165 printed the assembly selection (riding
  entries, per-group choices, door routes); the prints are gone. No consumer
  parses `up` stdout — zero consumer edits.
- **The CADDY_IMAGE law moved to load time.** dev-165 refused a routes
  -declared repo missing the lock's `CADDY_IMAGE` at door-stanza render
  (bring-up only); `load_config` now refuses (every verb). Every
  route-declaring consumer already carries the lock entry — zero consumer
  edits.
- **Mirror-probe and selection refusal texts are unchanged**, including the
  not-mirrored/probe-failure classification.

## Surface 2 — the declaration schema

The authored surface did not move. Root fields identical at both ends
(`requires`, `repository`, `base_images`, `platforms`, `project_name`,
`gateway`, `services`, `k8s`, `mirror`, plus the invisible defaults), and
every load-time law dev-165 already carried survives verbatim: the flat
entry/option vocabulary and collision refusal, the auth shape, route/ports
laws (mapping-form group ports included), the lock-pairing law, the
leftover-manifest guard, chart values files, the `requires` self-check.
Invisible pins unchanged (score-compose 0.42.0, score-k8s 0.15.0, helm
3.22.1; `stack/generated/…` output paths). The lock stays env-format at HEAD
— the structured-YAML flip is Plan A phases 2–5, not landed.

`requires: ">=0.4.9.dev165"` (all five consumers) is satisfied by 0.5.9 and
by the phase 8 release — no forced stamp bump at the wave; the repin raises
the stamps deliberately, not under compulsion.

## Surface 3 — artifact shapes

Committed artifacts are shape-stable: same output paths, same tool pins,
same post-passes (service-name collapse, per-entry gate rename, bind
inlining with `$` doubling and the consumer-interpolated exception, the
instance-suffix hash rewrite, the secrets contract, provisioner closure,
shipped patch templates). The rendered bake file is the same render
(`config.render_bake` is `documents.render_bake` moved; tags, args, cache
refs, dev-tag spelling identical). Per-run materializations (assembly root
file, dev overlay, per-run env) keep their shapes.

Two generation-time refusals moved earlier — the only artifact-surface
teeth the refactor added:

- A bundle rendering defining the reserved `door` service now refuses at
  `generate-score` (dev-165 refused at `up` selection).
- dev-toggles spelling and multi-workload laws (NAME=default pairs,
  ALL_CAPS, one workload per service name, `metadata.name` required) now
  refuse at `generate-score` (dev-165 refused at `up --dev`).

## The library surface the refactor broke

The module moves behind the ~15 broken import sites:

| dev-165 home | HEAD home | Status at HEAD |
|---|---|---|
| `docker_devkit.host` (`parse_env_file`, `host_platform`, `Gpu`, `GPU_TYPES`, `detect_gpu`) | dissolved | `parse_env_file` → `assembly.py`; `host_platform`/`Gpu`/`GPU_TYPES`/`detect_gpu` → `bake.py` |
| `docker_devkit.documents.compute_service_shas` | `service_builds.compute_service_shas` | moved |
| `docker_devkit.documents.parse_image_lock` / `ImageLock` | `config.py` | moved |
| `docker_devkit.documents.render_bake` | `config.render_bake` member | moved (no free function) |
| `docker_devkit.build_docker.run_build` | `bake.run_build` | moved (signature: takes root/config) |
| `docker_devkit.score_generate.{generate_compose,generate_k8s}` | `score.py` | moved |
| `docker_devkit.config.compose_bundles` | `config.compose_bundles()` member | moved (no free function) |
| `docker_devkit.image_refs` coupling quartet | `image_refs.py` | unmoved — dies in phases 3/6 |
| `docker_devkit.config.{LOCK_PATH, load_config, DockerDevkitConfig}` | `config.py` | unmoved, still importable |

## The consumer-edit list (phase 9)

Import inventory re-verified by grep — exactly the ADR's eight files, zero
`docker_devkit` imports anywhere else in the five consumer repos:

- placeframe: `scripts/preflight.py` (the four imports), `scripts/src/scripts/api_auth.py`,
  `scripts/src/scripts/sweep_postprocess.py`,
  `scripts/tests/test_score_{compose,k8s}_generation.py`
- capture-tool: `scripts/src/scripts/install_zed/{orchestrator,box_install,constants}.py`

Per repo, the complete edit list:

**placeframe**

1. `scripts/preflight.py` — drop the four imports and `VERSION_COUPLINGS`;
   the unpinned-refs and coupling checks die with their machinery (D3–D5);
   the postgres-wrapper leg rewrites onto the CLI: `uv run build --targets
   postgres` (the building run injects its own SHAs) and `uv run tree-tags
   postgres` for the `POSTGRES_SHA` the `docker run` invocation spells.
2. `scripts/src/scripts/api_auth.py` — `host.parse_env_file` → the
   consumer-local env-file parser (D8).
3. `scripts/src/scripts/sweep_postprocess.py` — the `.env` read → the
   consumer-local parser (D8); the lock read (`parse_env_file(LOCK_PATH)` for
   `INITIALIZE_S3_IMAGE`) → a plain YAML read selecting
   `images.INITIALIZE_S3_IMAGE.platforms[linux/amd64]` (the env-format lock
   reader dies with Part 0).
4. `scripts/tests/test_score_{compose,k8s}_generation.py` — rewrite to
   assert against the committed `stack/generated/**` artifacts; the
   `docker-devkit >= 0.5.5` skipif guards die with the library imports.
5. `scripts/AGENTS.md` — drop the `docker_devkit` module references (the
   See-also row's `docker_devkit.host.parse_env_file` and the preflight
   prose's "image refs, image version couplings").
6. `AGENTS.md` — the preflight prose drops "image refs, version couplings";
   the sandbox-notes devkit-floor prose (the `0.4.9.dev165` floor sentence)
   restates the release pin.
7. `stack/AGENTS.md` — `build -t api --gpu none` loses `--gpu none`
   (phase 7 kills the flag); the `build --lock-only` mentions become the
   `lock` verb.
8. `workloads/{reconstructor,localizer}/Dockerfile` — the stale
   `docker_devkit.documents.compute_service_shas` comments (reconstructor
   line 31, localizer line 30) reword onto `tree-tags` (the module anchor
   dies with D1).
9. `docker-devkit.yaml` — declare `platforms: [linux/amd64]`; declare
   `variants: {TORCH_DEVICE: [cuda, rocm]}` (cuda first — the default cell).
10. `scripts/pyproject.toml` floor + `uv.lock` — repin onto the phase 8
    release.
11. After the relock: one deliberate `stack/generated/**` regeneration cycle
    (own commit, `Run generate-score`).
12. `.github/workflows/*.yml` — bump the github-actions-toolkit `uses:` SHAs
    to the commit carrying the build-workflow updates (one SHA everywhere,
    per the toolkit lint).

**placeframe-capture-tool**

1. `scripts/src/scripts/install_zed/constants.py` — the `LOCK_PATH` import
   dies; `ENV_LOCK_FILE` hardcodes `docker-devkit.lock`.
2. `scripts/src/scripts/install_zed/orchestrator.py` — drop `load_config`,
   `compute_service_shas`, `host.parse_env_file`; the lock read becomes a
   plain `yaml.safe_load` over the structured lock.
3. `scripts/src/scripts/install_zed/box_install.py` — drop
   `DockerDevkitConfig`, `documents.render_bake`, `host.host_platform`; the
   `--build` fork (own bake render, own env assembly, `--set` retargets)
   dissolves into `uv run build --push localhost:{REGISTRY_PORT}` (D7);
   stock-image reads become
   `images.{LOKI,ALLOY}_IMAGE.platforms[linux/arm64]` selections.
4. `AGENTS.md` + `scripts/AGENTS.md` — the `build --lock-only` mentions
   become `lock`; the env-format lock description (`NAME=mirror-ref@sha256:…`)
   rewrites for the structured lock; the scripts/AGENTS stock-image
   index-digest defect paragraph (the live per-arch defect) resolves — its
   "correction path is a docker-devkit resolver fix + repin + regeneration"
   is exactly this wave.
5. `zed-aoa-diagnosis-handoff.md` — the `build --lock-only` references
   become `lock` (the document gates the AOA deploy on this wave's arm64
   platform manifests).
6. `pyproject.toml` exact pin + `scripts/pyproject.toml` floor + `uv.lock` —
   repin onto the release.
7. `docker-devkit.lock` — relock; the arm64 platform-manifest digests for
   LOKI/ALLOY land (the box ships executable images).
8. `.github/workflows/*.yml` — toolkit SHA bumps.

**infra-rathole**

1. `docker-devkit.yaml` — declare `platforms: [linux/amd64]` (the phase 2
   refusal forces it: `base_images` is declared).
2. `pyproject.toml` pin + `uv.lock` — repin.
3. `docker-devkit.lock` — relock.
4. `AGENTS.md` + `README.md` — `build --lock-only` mentions become `lock`
   (the how-to row and the file-tree legend line alike); the AGENTS devkit
   row's exact-pin prose restates the release pin.
5. `.github/workflows/*.yml` — toolkit SHA bumps.

**lbe-toolkit**

1. `docker-devkit.yaml` — declare `platforms: [linux/amd64]` (forced);
   `UV_BASE_IMAGE`'s floating tag (`ghcr.io/astral-sh/uv:python3.13-bookworm-slim`)
   becomes `{uv}`/`{python}`-templated (D3).
2. `pyproject.toml` pin + `uv.lock` — repin.
3. `docker-devkit.lock` — relock (the template resolves at lock; the
   composed tag's existence is the live check).
4. `.github/workflows/*.yml` — toolkit SHA bumps.

**Make-it-Sing**

1. `docker-devkit.yaml` — declare its platform (forced; `CADDY_IMAGE` is
   declared).
2. `pyproject.toml` pin + `uv.lock` — repin.
3. `docker-devkit.lock` — relock.
4. `.github/workflows/*.yml` — toolkit SHA bumps; Make-it-Sing currently
   pins the older `da905243` while the other four pin `44bcf9be` — the wave
   carries it to the new SHA from further behind.

**github-actions-toolkit** (rides the wave; never a dependency)

1. `.github/workflows/build-docker.yml` — the freshness line
   `build --lock-only --check` becomes `lock --check`; the build line drops
   `--gpu "$GPU"` and the `GPU:` env translation; the build job gains the
   D11 shelf step (a `builds_ref` verb invoked like `free-disk-space`,
   shelving each matrix cell's shard from bake's `metadata.json` under the
   existing `REGISTRY` env).
2. `src/github_actions_toolkit/verbs/release.py` — the `shelf` naming
   retires as `builds_ref` (phase 8's rename, landed with the wave's toolkit
   commit).

## Findings beyond D9's enumeration

Folded into the implementation plan's phase 9 text; recorded here for the
audit trail:

1. The phase 2 platforms refusal forces `platforms` in **infra-rathole and
   lbe-toolkit** too — D9's prose named only placeframe and Make-it-Sing.
2. `sweep_postprocess.py` reads the **lock**, not just `.env` — its
   `INITIALIZE_S3_IMAGE` lookup is a third lock consumer (after install-zed
   and the compose/bake valves) that Part 0's format flip touches.
3. **Toolkit SHA bumps** in every consumer's workflow files are part of the
   wave (the toolkit's workflow updates reach consumers only through their
   `uses:` pins); Make-it-Sing starts a SHA behind.
4. **capture-tool carries two devkit declarations** (root exact pin +
   `scripts/` member floor) — the repin touches both.
5. The consumer **AGENTS/README docs pass is broader than
   `scripts/AGENTS.md`**: `--lock-only` and `--gpu` mentions, env-format lock
   descriptions, the stock-image defect paragraph, and preflight-prose
   coupling references live in six consumer docs across four repos.
6. `workloads/reconstructor/Dockerfile` carries a stale
   `docker_devkit.documents.compute_service_shas` comment — a library
   reference outside the import inventory.
7. dev165→HEAD itself forces **no consumer edit on the CLI/schema/artifact
   surfaces** — every behavioral delta the refactor landed is either
   already satisfied by consumer state or invisible to consumers. The
   consumer-side work at the wave is entirely the ADR's own dissolutions
   (D3–D12) plus the Part 0 lock flip, not refactor breakage.
