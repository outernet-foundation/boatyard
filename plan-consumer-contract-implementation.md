# Consumer-contract implementation plan (Plan A)

Procedural delivery plan for the consumer-contract ADR
(`plan-consumer-contract.md`), including the Part 0 material that ADR
absorbed from `plan-artifact-identity.md` — the lifted design detail lives in
phases 2–5 below, so this plan executes without the parent ADR. The Part 1
material (attribution-based tree-tag precision) the ADR also absorbed executes
at the opening of `plan-artifact-identity-implementation.md` (Plan B), whose
change-detection phase hard-depends on it; the parent ADR keeps Parts 2–4
there. The ADR owns decisions and rationale; this plan owns the ordered work
and is the document implementing sessions build against. Per phase: load the
phase section, the files it names, and the ADR anchor it cites — not the whole
ADR. If this plan and the ADR disagree, reconcile both docs before touching
code (spec-first).

This plan is also the naming session the ADR deferred: the tag-lookup verb is
`tree-tags` (phase 5) and the retarget flag is `--registry` (phase 7).

Phase completion lives in git history; nothing here tracks status.

## Standing constraints

These rulings bind every phase:

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
  `platforms` is required wherever images are declared; the lock is
  structured YAML recording, per image, the index digest plus one concrete
  manifest digest per declared platform; env consumption is synthesized
  per-run at the edges.
- Construction over policing — the session's axiom: no cross-file invariant
  check survives anywhere. The one place detection remains is the literal-
  image refusal (D5), a validity rule of the input format at parse boundaries
  the devkit already owns.
- The domain-record refactor is frozen (ADR D2); a straggler derivation that
  surfaces during this work folds as ordinary work, not a reopened arc.
- Digests live exclusively in `docker-devkit.lock`, written only by
  `build --lock-only`. Pre-1.0 breaking changes ride the patch flow; from 1.0
  on, schema/CLI/artifact-breaking changes ship with a manually bumped
  `major_minor`.

## Sequence

```
archaeology        phase 1      O1: the dev165→0.5.9 contract diff;
                                findings fold into phase 9
lock foundation    phases 2-5   structured YAML lock, toolchain-derived
                                base_images, platform-resolving writer,
                                env synthesis, lock-env + tree-tags
declaration teeth  phase 6      literal image references refused at
                                derivation
retarget           phase 7      build --registry
release and wave   phases 8-9   docs pass, one devkit release, the
                                five-consumer catch-up
```

Order within a cluster is strict; cluster order is fixed above. The release
phase is the single wave consumers pin once — nothing consumer-facing ships
in any earlier phase.

## Session-start checkout state

The plan spans seven repos. A session opening on a fresh machine verifies
every repo it touches against this table before any phase work runs and
refuses loudly on any mismatch — wrong branch or wrong commit. Verified at
capture (2026-11-01): docker-devkit at the refactor head (0.5.9, unpublished,
five ADR commits past origin); the consumer rows record the pre-initiative
state the archaeology diffs against — `compose-bundles` is the checkout state
those repos were left stale on, not a deliverable branch, and the catch-up
wave (phase 9) re-derives each row where its repo actually lands. Once phase
sessions begin landing commits on a repo, that repo's HEAD advances past its
recorded sha by design; only the branch keeps needing to match.

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
| 1 | Contract archaeology | docker-devkit + consumers | O1 |
| 2 | Platform declarations and the structured lock model | docker-devkit | D6, absorbed Part 0 |
| 3 | Toolchain-derived base-image templates; coupling registry dies | docker-devkit | D3, D4 |
| 4 | Platform-resolving lock writer and mirror probes | docker-devkit | absorbed Part 0 |
| 5 | Env synthesis, `lock-env`, and `tree-tags` | docker-devkit | D6, absorbed Part 0 |
| 6 | Literal image references refused at derivation | docker-devkit | D5 |
| 7 | Registry-retargeted builds | docker-devkit | D7 |
| 8 | Docs pass and initiative release | docker-devkit | D1 |
| 9 | Consumer catch-up wave | all five consumers | D8, D9 |

"Absorbed Part 0" anchors point at the design the consumer-contract ADR
absorbed from `plan-artifact-identity.md`; the ADR's Absorption section holds
its amendments and the design detail itself is lifted into the phases. The
also-absorbed Part 1 (attribution-based tree-tag precision) executes at the
opening of `plan-artifact-identity-implementation.md`.

## Phase 1 — Contract archaeology

Cluster archaeology · docker-devkit + consumers · ADR O1 and "Archaeology
starting point". Load: the `dev-165` tag (3406e1e — the dev165 publication
point), refactor HEAD, the consumers' pinned usage.

End state:

- The full `dev-165..HEAD` contract diff is enumerated across the three
  contract surfaces: the CLI (verbs, flags, refusal texts), the declaration
  schema (`docker-devkit.yaml` laws, `base_images`, lock), and the artifact
  shapes (`stack/generated/**`, the rendered bake file, per-run env and
  compose materializations).
- The library-import inventory is re-verified with grep before anything edits
  consumer trees: placeframe's `scripts/preflight.py` (its four imports:
  `run_build`, `load_config`, `compute_service_shas`, and the `image_refs`
  quartet), `scripts/src/scripts/{api_auth,sweep_postprocess}.py`,
  `scripts/tests/test_score_{compose,k8s}_generation.py`; capture-tool's
  `scripts/src/scripts/install_zed/{orchestrator,box_install,constants}.py`.
  `placeframe/scripts/AGENTS.md` also names `docker_devkit` modules and joins
  the phase 9 docs pass.
- Output is the complete consumer-edit list phase 9 executes — findings
  beyond D9's enumeration fold back into this plan's phase 9 text (edit the
  plan, not the consumer trees, before the wave runs).

Verify: the edit list names every consumer file the diff touches; grep over
the five consumer repos returns zero `docker_devkit` imports outside the
inventory.

## Phase 2 — Platform declarations and the structured lock model

Cluster lock foundation · docker-devkit · ADR D6 home 2. Load: `config.py`
(`DockerDevkitConfig`, `load_config`, `ImageLock`, `parse_image_lock`, the
filesystem-gated lock laws), `tests/test_config.py` (the lock-parsing
battery).

End state:

- `platforms` is required at `load_config` whenever `base_images` is declared
  — the refusal names the fix; charts are exempt (arch-neutral); repos
  declaring no images keep `platforms` optional.
- `ImageLock` becomes the typed YAML model: `images:` mapping name →
  `{index, platforms: {os/arch: reference}}`; `charts:` mapping name → flat
  digest-pinned reference. `parse_image_lock` swaps wholesale — no dual-read;
  path:line error discipline, duplicate-name and non-digest-pin rejection
  carry over; the empty lock stays legal. Bundle entries never join this
  model — Parts 2–4 remain paused with the parent ADR.
- The writer renders deterministically: sorted names, sorted platform keys,
  byte-stable across machines — `--lock-only --check` stays hermetic on
  any-arch runners.

Verify: config refusal (images declared, `platforms` absent); YAML parse
refusals (malformed, duplicate, non-digest); two consecutive writes are
byte-identical.

## Phase 3 — Toolchain-derived base-image templates; coupling registry dies

Cluster lock foundation · docker-devkit · ADR D3 and D4. Load: `config.py`
(`base_images` validation at load), a consumer `pyproject.toml`
(`tool.uv.required-version`) and `.python-version` as the derivation sources.

End state:

- `base_images` values may be templates over the repo's toolchain pins:
  `{uv}` resolves from `tool.uv.required-version` (operator-stripped; a range
  where an exact pin is required refuses loudly) and `{python}` from
  `.python-version`. The devkit implements only generic `{key}` substitution;
  the vendor's tag grammar stays authored data in the template string. uv
  owns the range-versus-selection consistency between `requires-python` and
  `.python-version` natively — no coupling is reimposed anywhere.
- Plain values stay plain (the golang/caddy/zed class — tags with no second
  home have no drift surface). A literal value in place of a template is the
  deliberate-skew escape hatch, visible in every diff. A template composing a
  nonexistent tag fails at `build --lock-only` — fail-closed, same as a
  mistyped literal today.
- `VersionCoupling`/`VersionSite` and `version_coupling_violations` are gone
  from `image_refs.py`; nothing replaces them — D3 eliminates every coupling
  they covered by construction, and policing an invariant construction makes
  unreachable is dead weight. placeframe's preflight `VERSION_COUPLINGS` list
  dies in phase 9, not here.

Verify: template resolution from fixture pins; range-pin refusal; plain values
unaffected; grep shows the coupling machinery gone.

## Phase 4 — Platform-resolving lock writer and mirror probes

Cluster lock foundation · docker-devkit · ADR Absorption, Part 0 design moves
3 and 8 plus "Migration". Load: `bake.py` (`run_build`'s lock writer,
`--lock-only`, `--check`), `image_refs.py` (the `imagetools inspect`
resolution), `verbs/mirror.py`, `publish.py` (its memoized digest pinning
stays index-shaped — compose `include: oci://@digest` consumes indexes).

End state:

- The resolver records, per declared image, the index digest plus one
  concrete manifest digest per declared platform, resolved against the
  upstream reference. Selection rules: exact `os`/`architecture` match;
  `unknown/unknown` and attestation-annotated entries skipped; multiple
  matches refuse loudly naming the entry; single-manifest refs and `@digest`
  declarations record the manifest uniformly as index and sole platform
  entry. Resolution is a pure function of config — the writing machine's arch
  is never consulted (pin with fixture indexes, never live registries).
- `build --lock-only` writes the structured lock; declaration-incremental
  freeze carries whole structured entries forward; `--upgrade` re-resolves
  everything; `--check` diffs the YAML form. A repo whose committed lock is
  env-format fails `--check` with the full-format diff — that diff is the
  cutover migration mechanism; no dual-read exists. The declaration-freeze/
  `--upgrade` migration question dissolves in the cutover: the YAML rewrite
  re-resolves every entry.
- The `mirror` verb probes every recorded digest (index plus dishes) via
  `crane digest`; the copy itself is unchanged — whole index, dishes ride
  along, `crane copy` byte-identity keeps upstream and mirror digests equal.

Verify: fixture-index selection battery (arm64 child present; attestations
skipped; multi-match refusal; single manifest; `@digest` declaration); freeze
and `--upgrade` semantics over structured entries; `--check` unified diff
including the env→YAML cutover; mirror probe enumeration covers every
recorded digest. Owed from the absorbed investigation and closed here: pull
and `crane digest` a dish digest against the mirror (ghcr serves child
manifests by digest; per-dish retrieval was verified only inside the copied
index), and `imagetools inspect --raw <dish>` answers a manifest, not an
index.

## Phase 5 — Env synthesis, `lock-env`, and `tree-tags`

Cluster lock foundation · docker-devkit · ADR D6 homes 3–4, Part 0 design
moves 4–7. Load: `assembly.py` (`Assembly`, `compose_command`, the
`--env-file` attach), `verbs/up.py` (the fail-closed probe), `markers.py`
(`lock:`/`lock-image:`), `bake.py` (bake environment assembly),
`config.py` (`service_shas`), `pyproject.toml` (entry points).

End state:

- One selection function (an `entry_for(lock, name, platform)` shape) is the
  only selector — the compose edges, bake assembly, and markers call it. It
  is devkit-internal glue (the D1 law over the absorbed phrasing: no consumer
  calls it; consumers read the committed YAML or `lock-env`).
- `up`/`down` synthesize a per-run env file (the selected platform entries)
  attached as the second `--env-file`, with the materialized root file's temp
  lifecycle — printed and kept on failure like the bake file. Platform
  selection is declaration-driven: a config `platforms` singleton selects
  itself; a multi-platform repo's `.env` names `PLATFORM=`; never host
  introspection. Bake receives the same synthesis in-process.
- A `lock-env` verb emits the synthesized env on stdout (the raw
  `docker compose --env-file <(uv run lock-env)` valve).
- The tag-lookup verb `tree-tags`: positional service names, one bare
  `tree-` tag per line on stdout, unknown names refuse loudly listing the
  vocabulary. The whole contract is a string, not a schema — no JSON
  document, no service-list endpoint, no `{SERVICE}_SHA` names (consumers
  that write `.env` lines own those names where they author the compose
  holes). A deterministic function of HEAD over `config.service_shas()`;
  never cached, never committed — the tree-derived class has no home in any
  committed file.
- The `up` fail-closed probe runs over every recorded digest (index plus
  dishes) under the existing stderr-classification law.
- `lock:`/`lock-image:` markers resolve by substrate: the index under k8s
  (kubelet resolves per node — architecturally correct), the platform dish
  under compose.

Verify: synthesis determinism; both selection forms (singleton and
`PLATFORM=`); probe classification over dish digests; marker substrate
resolution; `lock-env` output composes bring-up; `tree-tags`
correct-by-HEAD, unknown-name vocabulary listing, one-tag-per-line shape.

## Phase 6 — Literal image references refused at derivation

Cluster declaration teeth · docker-devkit · ADR D5. Load: `service_builds.py`
(`derive_services` — parses every `FROM` for context edges), `score.py`
(reads every workload `image:` line), `image_refs.py`
(`unpinned_references`), `tests/test_image_refs.py`.

End state:

- In devkit-owned trees, hand-written literal image references — Dockerfile
  `FROM`, `COPY --from=` (slash-bearing), yaml `image:` — are refused outright
  at the parse boundaries that already read them: `derive_services` refuses a
  literal `FROM`/`COPY --from=` (use `base_images` ARGs), generation refuses
  a literal `image:` (use `lock:` markers). A validity rule of the input
  format, the same category as `extra="forbid"` — strictly the file formats
  permit literals and docker builds them, but with literals banned there is
  no pair of authored homes left to drift anywhere in the system.
- `unpinned_references` and the pinned-literals-allowed scan it implemented
  die: zero literal refs exist in any consumer tree today, so the class it
  permitted was empty. Consumer CI steps consuming its result die in
  phase 9.

Verify: each refusal fires with a fix-naming error; the scan is gone from
`image_refs.py`; fixture trees carrying literals refuse at both boundaries.

## Phase 7 — Registry-retargeted builds

Cluster retarget · docker-devkit · ADR D7. Load: `bake.py` (`run_build`, push
mode, cache refs, the shelf push), `config.py` (`render_bake`),
`verbs/build.py` (flags).

End state:

- `build --registry <addr>` implies push mode — a retargeted build has
  nowhere to go but the registry — and a `--registry` + `--load` pairing
  refuses as meaningless.
- Tags and cache refs re-root under the registry: `{registry}/{target}` tags
  and `{registry}/build-cache:{target}` cache refs — one law everywhere:
  cache rides the same registry the tags do, so the persistent laptop
  registry gives the install iteration loop the same layer caching ghcr
  builds get. The builds-shelf digest-manifest push is unchanged (it is
  HEAD-keyed and reads its registry facts from the neutral env).
- install-zed's forked pipeline — its own bake render, env assembly, and
  `--set` retargeting to a transient host-local registry — dissolves into
  this flag in phase 9. The box-facing address spelling stays
  consumer-side: install-zed derives it from `$SSH_CLIENT` as it already
  does.

Verify: retargeted tags and cache refs in the rendered bake (golden tests);
the `--load` refusal; push-mode implication; a retargeted build round-trips
through a transient local registry.

## Phase 8 — Docs pass and initiative release

Cluster release · docker-devkit · ADR D1 and D6. Load: docker-devkit's
`AGENTS.md`, the release flow in github-actions-toolkit's `AGENTS.md`.

End state:

- The AGENTS rewrite lands with the code, stating present truth: the
  release-flow law is restated — what ships with a bumped `major_minor` from
  1.0 on is schema/CLI/artifact-breaking change; there is no Python API to
  break. The stale "pulsar's bake_bundle" consumer reference in the
  `publish.py` row dies. The lock section is rewritten for the structured
  YAML lock — the env-line-format law reverses, and the section records that
  synthesis at the edges preserves both its documented virtues (native
  `--env-file` consumption at the `up` handoff, the raw-`docker compose`
  valve). The `image_refs.py` row loses the coupling machinery and the scan;
  `tree-tags` and `lock-env` join the entry-point table; the D5 refusals
  join their parse-boundary homes.
- docker-devkit ships one release carrying phases 2–7 (pre-1.0; breaking
  rides the patch flow). This release is the pin target of the wave.

Verify: the battery green; the release published; `AGENTS.md` matches the
shipped surface with no stale references (grep the repo for `bake_bundle`,
the coupling names, the env-line law wording).

## Phase 9 — Consumer catch-up wave

Cluster wave · all five consumers · ADR D8, D9, and phase 1's edit list.
Load: each consumer's `docker-devkit.yaml`, `docker-devkit.lock`, `.env`,
`uv.lock`, CI workflows; the phase 1 edit list (fold its findings in before
editing).

End state:

- Every consumer repins once, onto the phase 8 release, and relocks — the
  env-format lock is rewritten as structured YAML by `build --lock-only`,
  which re-resolves every entry, so no per-entry migration exists.
  `platforms` completes where the config refusal forces it: placeframe
  declares amd64 and Make-it-Sing declares its platform in the same relock.
- placeframe: `scripts/preflight.py` drops its four library imports and the
  `VERSION_COUPLINGS` list (the coupling CI step dies with it, and so does
  any `unpinned_references` step — D5 moved the teeth into the devkit);
  `scripts/src/scripts/{api_auth,sweep_postprocess}.py` read `.env` through
  a consumer-local module implementing compose's env-file grammar
  (`KEY=VALUE`, `#` comments, no interpolation — compose is the file's
  authoritative reader, so its rules are the spec; no dotenv dependency, no
  shared micro-package, the filename hardcoded exactly as the scripts
  already spell it); the two score test modules
  (`scripts/tests/test_score_{compose,k8s}_generation.py`) rewrite to assert
  against committed artifacts (freshness is already gated by
  `generate-score --check` in CI); `scripts/AGENTS.md` drops its
  `docker_devkit` module references. After the relock: one deliberate
  `stack/generated/**` regeneration cycle — the structured lock changes every
  marker-resolved image reference in the artifacts (compose-side resolution
  now selects platform dishes) — commit the devkit bump, run
  `generate-score`, commit the artifacts in a second commit (the score
  staleness gate is disabled there; operator discipline).
- placeframe-capture-tool: install-zed (`orchestrator.py`, `box_install.py`,
  `constants.py`) drops its library imports and rewrites onto the read
  surface — arm64 dishes by reading `images.<NAME>.platforms[linux/arm64]`
  from the committed YAML lock with plain `yaml.safe_load`, service tags
  through `uv run tree-tags <service>…`, and its `--build` mode through
  `uv run build --registry <addr>` (the forked bake pipeline dissolves). Its
  relock lands arm64 dish digests for loki/alloy — the box ships executable
  images and the ZED-box AOA deployment unblocks (its
  `zed-aoa-diagnosis-handoff.md` gates the deploy on exactly this).
- infra-rathole and lbe-toolkit: repin and relock only — no imports to drop.
  lbe-toolkit's floating uv tag (`ghcr.io/astral-sh/uv:python3.13-…`)
  becomes an `{uv}`-templated value in the same relock: intent stays
  authored, components derive.
- github-actions-toolkit: never a dependency (the resolver-cycle law); its
  reusable workflows invoke the CLI and bind `docker-build-matrix` and
  `publish-compose --plan` — frozen CI contracts that remain as-is. Phase 1
  findings may surface flag-level edits here; fold them into this phase.

Verify: consumer CI green end-to-end; grep over the five consumer repos
returns zero `docker_devkit` imports; the placeframe regeneration diff is
committed and reviewed; capture-tool's lock carries arm64 dishes; `up`
probes pass against the mirror.
