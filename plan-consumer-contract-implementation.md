# Consumer-contract implementation plan (Plan A) — release and wave

The consumer-contract initiative's remainder: one devkit release and the
five-consumer catch-up wave. Everything else the plan carried — the
archaeology (phase 1), the lock foundation (phases 2–5), the literal-image
refusals (phase 6), the build surface (phase 7), and the renames plus the
AGENTS docs pass (phase 8, minus its release) — is executed and lives in git
history. The ADR (`plan-consumer-contract.md`) owns decisions and rationale;
this plan owns the ordered work that remains and is the document implementing
sessions build against. Per phase: load the phase section, the files it
names, and the ADR anchor it cites — not the whole ADR. If this plan and the
ADR disagree, reconcile both docs before touching code (spec-first).

The release deliberately waits out an intervening refactor stint in
docker-devkit: it carries the consumer-contract work plus whatever the
refactor lands — one release, one consumer pin. Nothing consumer-facing
ships before it.

Phase completion lives in git history; nothing here tracks status.

## Standing constraints

These rulings bind every remaining phase:

- There is no Python API (ADR D1). The devkit's contracts are exactly the
  verb CLI, the declaration schema (`docker-devkit.yaml` +
  `docker-devkit.lock`), and the committed artifact formats. Nothing lands
  that a consumer could only use by importing `docker_devkit`; every consumer
  touch is CLI, declarations, or artifacts.
- The three-class principle: authored facts live in the config,
  registry-resolved facts in the lock, tree-derived facts are computed on
  demand and never committed. Classes never mix files.
- Fail closed: an unknown name, missing lock entry, template composing a
  nonexistent tag, or literal image reference refuses loudly, never silently.
- Platform facts come from declarations, never from the touching machine:
  `platforms` is required exactly when `base_images` is declared (the rule
  guards lock resolution; charts are arch-neutral and exempt); the lock is
  structured YAML recording, per image, the index digest plus one concrete
  manifest digest per declared platform; env consumption is synthesized
  per-run at the edges. Variant axes follow the same law (ADR D12): value
  sets and defaults are declared, never probed.
- Construction over policing — the session's axiom: no cross-file invariant
  check survives anywhere. The one place detection remains is the literal-
  image refusal (D5), a validity rule of the input format at parse boundaries
  the devkit already owns.
- Vocabulary stays concrete: a per-platform digest is a platform manifest, a
  read location is a read source. The mental-model names that got blessed as
  identifiers — `door`, `shelf`, `PublishLeg`, `build_legs`, `fan`/`own_fan`,
  and the gpu detection machinery — are retired; nothing reintroduces them or
  mints new metaphor names in code, tests, refusal strings, or docs.
- The domain-record refactor is frozen (ADR D2); a straggler derivation that
  surfaces during this work folds as ordinary work, not a reopened arc.
- Digests live exclusively in `docker-devkit.lock`, written only by the
  `lock` verb and `build`'s incremental freeze — never hand-authored.
  Pre-1.0 breaking changes ride the patch flow; from 1.0
  on, schema/CLI/artifact-breaking changes ship with a manually bumped
  `major_minor`.

## Sequence

```
release   phase 8   one docker-devkit release carrying the
                    consumer-contract work plus the intervening
                    refactor; the wave's pin target
wave      phase 9   all five consumers repin once, relock, and
                    ride the toolkit SHA bump
```

The release precedes the wave; nothing consumer-facing ships in any earlier
work.

## Session-start checkout state

A session opening on a fresh machine verifies every repo it touches against
this table before any phase work runs and refuses loudly on any mismatch —
wrong branch or wrong commit. The rows record the pre-initiative state the
archaeology diffed against — `compose-bundles` is the checkout state those
repos were left stale on, not a deliverable branch, and the wave re-derives
each row where its repo actually lands. A repo's HEAD advances past its
recorded sha by design; only the branch keeps needing to match, with each
row's sha an ancestor of HEAD.

| Repo | Branch | Commit |
|---|---|---|
| docker-devkit | refactor | 149f3579fc0ec7b65d0d52feccc05e44f30553f7 |
| placeframe | compose-bundles | 71994eace2dabb17389ccb9e2ead1dd75f062eed |
| placeframe-capture-tool | compose-bundles | 6f9430bee373bdd1b47998b913ed70cb0b8b5eb5 |
| Make-it-Sing | compose-bundles | a73e8070f7385765614e28e28c3fde1c8231b89a |
| infra-rathole | compose-bundles | b2e8da3226a272329cf5a9a9bc1f6b47f793d867 |
| lbe-toolkit | compose-bundles | 3bd3bfadd6c4f5e8bbe4a839cf571cb0c572549a |
| github-actions-toolkit | dev | 44bcf9be6f4456ba978ae4e1438be9e8fbf35a32 |

## Session protocol

One phase per session. Verify with the target repo's battery (`uv run pytest`,
`uv run ruff check`, `uv run basedpyright` — per each repo's AGENTS.md). Commit
as you go; prose and code in separate commits. A phase is done when its end
state holds and its verify list passes green.

## Phase map

| # | Phase | Repo | ADR anchor |
|---|---|---|---|
| 8 | Initiative release | docker-devkit | D1 |
| 9 | Consumer catch-up wave | all five consumers | D8, D9 |

## Phase 8 — Initiative release

Cluster release · docker-devkit · ADR D1 and D6.

End state:

- docker-devkit ships one release carrying the consumer-contract work (the
  structured lock, the `lock`/`lock-env`/`tree-tags` verbs, templated
  `base_images`, the destination axis, declared variant axes, the shelf
  extraction, the literal-image refusals, the vocabulary renames) plus the
  refactor work that lands before this phase runs (pre-1.0; breaking rides
  the patch flow). This release is the pin target of the wave.

Verify: the battery green; the release published; `AGENTS.md` matches the
shipped surface with no stale references.

## Phase 9 — Consumer catch-up wave

Cluster wave · all five consumers · ADR D8, D9, and the folded archaeology
findings. Load: each consumer's `docker-devkit.yaml`, `docker-devkit.lock`,
`.env`, `uv.lock`, CI workflows. Open by re-running the archaeology greps
(zero `docker_devkit` imports outside the sites named below) to re-derive
the edit list against wherever the release actually landed.

End state:

- Every consumer repins once, onto the phase 8 release, and relocks — the
   env-format lock is rewritten as structured YAML by the `lock` verb,
   which re-resolves every entry, so no per-entry migration exists.
   `platforms` completes where the config refusal forces it (every
   `base_images`-declaring consumer): placeframe declares amd64,
   Make-it-Sing, infra-rathole, and lbe-toolkit declare theirs in the same
   relock. placeframe declares its variant axis (`TORCH_DEVICE: [cuda, rocm]`, cuda
   the default cell) in the same relock.
   The reserved front-door service name is `edge` from this release —
   devkit-derived, so consumers edit nothing; the renamed
   container at next bring-up is expected. Riding the same wave: every
   consumer's github-actions-toolkit `uses:` SHAs bump to the toolkit
   commit carrying the workflow updates below (one SHA per repo, per the
   toolkit's lint; Make-it-Sing starts the wave a SHA behind the others and
   jumps from further back).
- placeframe: `scripts/preflight.py` drops its four library imports and the
   `VERSION_COUPLINGS` list (the coupling CI step dies with it, and so does
   any `unpinned_references` step — D5 moved the teeth into the devkit);
   its postgres-wrapper leg rewrites onto the CLI — `uv run build --targets
   postgres` for the local build, `uv run tree-tags postgres` for the
   `POSTGRES_SHA` the `docker run` invocation spells;
   `scripts/src/scripts/{api_auth,sweep_postprocess}.py` read `.env` through
   a consumer-local module implementing compose's env-file grammar
   (`KEY=VALUE`, `#` comments, no interpolation — compose is the file's
   authoritative reader, so its rules are the spec; no dotenv dependency, no
   shared micro-package, the filename hardcoded exactly as the scripts
   already spell it), and sweep_postprocess's lock read
   (`INITIALIZE_S3_IMAGE`) becomes a plain YAML platform selection
   (`images.INITIALIZE_S3_IMAGE.platforms[linux/amd64]`); the two score test modules
   (`scripts/tests/test_score_{compose,k8s}_generation.py`) rewrite to assert
   against committed artifacts (freshness is already gated by
   `generate-score --check` in CI); the docs pass covers
   `scripts/AGENTS.md` (module references, preflight prose), the root
   `AGENTS.md` preflight prose ("image refs, version couplings") and its
   sandbox-notes devkit-floor sentence (the `0.4.9.dev165` floor restates
   the release pin), `stack/AGENTS.md` (`--gpu none` and `build --lock-only`
   mentions), and the stale `compute_service_shas` comments in
   `workloads/{reconstructor,localizer}/Dockerfile`. After the relock: one deliberate
   `stack/generated/**` regeneration cycle — the structured lock changes every
   marker-resolved image reference in the artifacts (compose-side resolution
   now selects platform manifests) — commit the devkit bump, run
   `generate-score`, commit the artifacts in a second commit (placeframe's
   CI disables the score-staleness gate; operator discipline covers this
   cycle).
- placeframe-capture-tool: install-zed (`orchestrator.py`, `box_install.py`,
   `constants.py`) drops its library imports and rewrites onto the read
   surface — arm64 manifests by reading `images.<NAME>.platforms[linux/arm64]`
   from the committed YAML lock with plain `yaml.safe_load`, service tags
   through `uv run tree-tags <service>…`, and its `--build` mode through
   `uv run build --push localhost:5000` (its `REGISTRY_PORT`
   constant; the forked bake pipeline dissolves). Its relock lands arm64
   platform-manifest digests for loki/alloy — the box ships executable
   images and the ZED-box AOA deployment unblocks (its
   `zed-aoa-diagnosis-handoff.md` gates the deploy on exactly this). The
   repin touches both declarations (the root exact pin and the `scripts/`
   member floor); the docs pass covers `AGENTS.md` and `scripts/AGENTS.md`
   (`build --lock-only` mentions, the env-format lock description, and the
   stock-image index-digest defect paragraph — its stated correction path
   is this wave) plus the AOA handoff's own `--lock-only` references.
- infra-rathole and lbe-toolkit: repin, relock, and declare `platforms`
   (the platforms-with-base_images refusal forces both — each declares
   `base_images`); no imports to drop. lbe-toolkit's floating uv tag (`ghcr.io/astral-sh/uv:python3.13-…`)
   becomes an `{uv}`-templated value in the same relock: intent stays
   authored, components derive. infra-rathole's `AGENTS.md`/`README.md`
   `build --lock-only` mentions become `lock` and its AGENTS devkit row's
   exact-pin prose restates the release pin, in the same docs pass.
- github-actions-toolkit: never a dependency (the resolver-cycle law); its
   reusable workflows invoke the CLI and bind `docker-build-matrix` and
   `publish-compose --plan` — frozen CI contracts that remain as-is. Riding
   the repin, the build workflow's freshness line becomes
   `uv run --locked lock --check`, the build line drops its `--gpu` argument
   and `GPU:` env translation (matrix targets carry variant cells), and the
   build job gains the builds_ref step
   (ADR D11): a toolkit verb beside the release-side pull, invoked through
   the toolkit project runner like `free-disk-space`, distilling each matrix
   cell's shard from bake's `metadata.json` under the workflow's existing
   `REGISTRY` env. The naming is `builds_ref` throughout — `verbs/release.py`
   and the new step alike (the `shelf` vocabulary is retired).

Verify: consumer CI green end-to-end; grep over the five consumer repos
returns zero `docker_devkit` imports; the placeframe regeneration diff is
committed and reviewed; capture-tool's lock carries arm64 platform
manifests; `up` probes pass against the mirror; every consumer's toolkit
`uses:` refs carry one SHA and every `--lock-only`/`--gpu` mention is gone
from consumer docs and workflows.
