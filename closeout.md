# Closeout: publish, consumer sweep, artifact identity

One compaction carrying every unexecuted item from the root plan docs; the six
consolidated files (`plan-artifact-identity.md`,
`plan-artifact-identity-implementation.md`, `plan-consumer-contract.md`,
`plan-consumer-contract-implementation.md`, `score-refactor-plan.md`,
`boatyard.md`) live in git history. `AGENTS.md` is the present-truth operations
manual for the shipped surface; this file is the remaining-work record.
`plan-apt-hermeticity.md` and `plan-mirror-digest-retention.md` are separate
initiatives, untouched here. Every decision below is closed — do not
re-litigate without its recorded flip condition. If this file and the code
disagree, reconcile this file before touching code (spec-first). One work unit
per session; verify with the target repo's battery; commit as you go, prose and
code in separate commits.

## Snapshot (verified 2026-11-02)

- boatyard branch `refactor`: all consumer-contract work, the module-tiers
  refactor, the score unification, and the boatyard rename + CLI refactors are
  landed locally, ahead of origin — unpushed. `publish.yml` is wired
  (`packages.boatyard`, `major_minor: "0.6"`, `registry: pypi`).
- PyPI: `boatyard` registered, nothing published. GitHub repo rename
  (docker-devkit → boatyard) is an operator item, status unverified.
- No `lock-env` verb exists (a stale name in the absorbed ADR): lock env is
  synthesized per-run inside `compose.py`/`bake.py` at the compose/bake edges.
- Consumers and toolkit, all verified still on docker-devkit spellings:

| Repo | Pin today | Library imports to dissolve |
|---|---|---|
| placeframe | scripts: `docker-devkit>=0.4.9.dev165` | `scripts/preflight.py` (4 imports + `VERSION_COUPLINGS`), `scripts/src/scripts/{api_auth,sweep_postprocess}.py`, `scripts/tests/test_score_{compose,k8s}_generation.py` |
| placeframe-capture-tool | root `==0.4.9.dev165`, scripts `>=0.1.15` | `scripts/src/scripts/install_zed/{orchestrator,box_install,constants}.py` |
| Make-it-Sing | root `==0.4.9.dev165` | none (CLI only) |
| lbe-toolkit | root `==0.4.9.dev165` | none (CLI only) |
| infra-rathole | dev group `==0.4.9.dev165` | none (CLI only) |
| github-actions-toolkit | never a dependency (resolver-cycle law) | — |

The five consumers sit on branch `compose-bundles` (the stale checkout state
they were left on, not a deliverable); the toolkit on `dev`. Re-verify with
grep at session open before editing — the import inventory maps each site to a
dissolution below.

## Standing constraints

These rulings bind every work unit below.

- No Python API (ADR D1): the contracts are exactly the verb CLI, the
  declaration schema (`boatyard.yml` + `boatyard.lock.yml`), and the committed
  artifact formats (`stack/generated/**`). Nothing lands that a consumer could
  only use by importing `boatyard`; every consumer touch is CLI, declarations,
  or artifacts.
- Three-class principle: authored facts in the config, registry-resolved facts
  in the lock, tree-derived facts computed on demand and never committed.
- Fail closed: unknown name, missing lock entry, template composing a
  nonexistent tag, literal image reference, unattributable construct, dirty
  content-addressed build path — all refuse loudly, never silently.
- Platform facts come from declarations, never from the touching machine;
  variant axes follow the same law (value sets and defaults declared, never
  probed).
- Construction over policing: no cross-file invariant check survives anywhere;
  the one detection that remains is the literal-image refusal, a validity rule
  of the input format at parse boundaries boatyard already owns.
- Vocabulary stays concrete: `door`, `shelf`, `PublishLeg`, `build_legs`,
  `fan`/`own_fan`, and the gpu detection machinery are retired; nothing
  reintroduces them or mints new metaphor names in code, tests, refusal
  strings, or docs.
- The domain-record refactor is frozen; a straggler derivation that surfaces
  folds as ordinary work, not a reopened arc.
- Digests live exclusively in `boatyard.lock.yml`, written only by the `lock`
  verb and `build`'s incremental freeze — never hand-authored. Exact tags only.
  `latest` is never a pin and is aliased only by stable releases.
- The PR channel publishes nothing permanent anywhere — only content-keyed
  ephemera (`tree-`, `sha-`).
- One ordinal — the commit count at the certified head — keys `pr-{N}-{count}`
  and the landing's `dev-{count}`.
- No tag GC and no retention policies on image repos carrying release tags:
  ghcr deletion is version-level and kills all tags on a digest; any future
  sweeper must be version-level and release-protecting. None is planned.
- Mirroring is for third-party upstreams only; org-internal artifacts resolve
  direct, digest-pinned through the lock.
- Pre-1.0 breaking changes ride the patch flow; from 1.0 on, schema/CLI/
  artifact-breaking changes ship with a manually bumped `major_minor`.

## Sequence

```
1  release     boatyard 0.6.0 — operator; the pin target
2  toolkit     cutover onto boatyard spellings; the SHA the wave bumps to
3  sweep       all five consumers repin once, relock, rename, drop imports
4  precision   attribution-based tree tags; boatyard release + consumer re-key
5  semver      per-unit image + bundle version lines (toolkit, bar, boatyard)
6  pin+over    qualified grammar, bundle lock pinning, overrides — one release
7  adoption    one commit per consumer repo onto 5–6
8  overlay     cross-repo dev overlay — trigger-gated, not scheduled
```

Order within a unit is strict; unit order is fixed. 4 precedes 5 by
construction: the change gate in 5 compares tree tags, and tags are a
trustworthy change signal only once 4 makes them source-precise — every
over-inclusion is a spurious version bump. 5 and 6 are separate releases,
each independently testable; 6 ships the qualified grammar, lock writer,
probe, and override together because they are one cluster in the selection
machinery (an override shipped before bundle pinning would be built against a
digest-ref spelling 6 deletes). Consumers adopt once, in 7, never per feature.

## Work 1 — Release 0.6.0 (operator)

Hand branch/SHA to the operator: push `refactor`, merge, tag through the
normal release flow (the toolkit's binding owns it); the PyPI publish rides
the flow — the name is registered, no placeholder race. One release, one
consumer pin; nothing consumer-facing ships separately. Confirm the GitHub
repo rename happened (Settings → Rename; old blob URLs redirect, consumers fix
links in Work 3). Optional: a final `docker-devkit` release whose description
points at `boatyard` (precedent: `stack-toolkit` ≤0.1.1 signposts) — skip if
unwanted.

Verify: the release published; `AGENTS.md` matches the shipped surface.

## Work 2 — github-actions-toolkit cutover

- `src/github_actions_toolkit/config.py`: `DOCKER_DEVKIT_CONFIG_PATH` →
  `Path("boatyard.yml")` (plain-YAML presence probe; the resolver-cycle law
  keeps it import-free).
- Workflows: every `uv run --locked <verb>` → `uv run --locked boatyard <verb>`
  (`build --push`, `mirror`, `publish-compose --plan`, `generate-score
  --check`, `docker-build-matrix`); fix the `--lock-only` ghost → `uv run
  --locked boatyard lock --check`; the build line drops its `--gpu` argument
  and `GPU:` env translation (matrix targets carry variant cells). The
  builds_ref naming stays `builds_ref` throughout.
- Tests: the three fixtures writing `docker-devkit.yaml` (test_prerelease,
  test_run_releases, test_config) → `boatyard.yml`.
- AGENTS.md: the config-file law line and the verb invocation spellings.

Verify: the toolkit's own suite green.

## Work 3 — Consumer sweep (one wave)

Open by re-running the archaeology greps to re-derive the edit list against
wherever the release actually landed. Common to all five, order within a repo
code → lockfiles → prose:

1. Dependency: `docker-devkit…` → `boatyard==0.6.0` (or `>=`); `uv lock
   --upgrade-package boatyard`. `requires:` floor → `>=0.6.0`.
2. `git mv docker-devkit.yaml boatyard.yml`; `git mv docker-devkit.lock
   boatyard.lock.yml` (content unchanged — the `lock` verb's re-resolve
   rewrites the env format as structured YAML in one step; no per-entry
   migration exists).
3. `.gitattributes`: `docker-devkit.lock merge=ours` → `boatyard.lock.yml
   merge=ours`.
4. Relock completes `platforms` where the config refusal forces it (every
   `base_images`-declaring consumer): placeframe amd64; Make-it-Sing,
   infra-rathole, lbe-toolkit theirs. placeframe declares its variant axis
   (`variants: {TORCH_DEVICE: [cuda, rocm]}`, cuda the default cell) in the
   same relock.
5. Every consumer's toolkit `uses:` SHAs bump to the Work-2 commit — one SHA
   per repo (Make-it-Sing starts a SHA behind and jumps from further back).
6. Docs: `docker-devkit` → `boatyard` / `boatyard.yml` / `boatyard.lock.yml`;
   `uv run <verb>` → `uv run boatyard <verb>`; GitHub doc links to the renamed
   repo. `--lock-only` is a dead flag everywhere → `boatyard lock` (plus
   `--upgrade` where the old text said `--lock-only --upgrade`).
7. Consumer env reads go through a consumer-local module implementing
   compose's env-file grammar (`KEY=VALUE`, `#` comments, no interpolation —
   compose is the file's authoritative reader, so its rules are the spec; no
   dotenv dependency, no shared micro-package, filename hardcoded as the
   scripts already spell it).
8. `stack/secrets.yaml` is required from 0.6.0 — consumers lacking it add the
   file (an empty file is legal: the empty declaration, no keys consumable)
   and commit the mint artifact `stack/generated/secrets.txt` it generates.

Per-repo:

- **placeframe** — `scripts/preflight.py` drops its four library imports and
  `VERSION_COUPLINGS` (the coupling CI step dies with it, and so does any
  `unpinned_references` step — the literal-image teeth moved into boatyard);
  the postgres-wrapper leg rewrites onto the CLI (`boatyard build --targets
  postgres`; `boatyard tree-tags postgres` for the `POSTGRES_SHA` the
  `docker run` invocation spells); `api_auth.py`/`sweep_postprocess.py` read
  `.env` through the consumer-local parser, and sweep_postprocess's lock read
  (`INITIALIZE_S3_IMAGE`) becomes a plain YAML platform selection
  (`images.INITIALIZE_S3_IMAGE.platforms[linux/amd64]`); the two score test
  modules rewrite to assert against committed artifacts (freshness is already
  gated by `boatyard generate-score --check` in CI); the stale
  `compute_service_shas` comments in `workloads/{reconstructor,localizer}`
  Dockerfiles and the `stack/AGENTS.md` "devkit's built-in gate-pin template"
  line (the gate pin moved into `generate_compose`) go in the docs pass. After
  the relock: one deliberate `stack/generated/**` regeneration cycle — the
  structured lock changes every marker-resolved image reference (compose-side
  resolution now selects platform manifests) — commit the bump, run
  `boatyard generate-score`, commit the artifacts in a second commit
  (placeframe's CI disables the score-staleness gate; operator discipline
  covers this cycle).
- **placeframe-capture-tool** — install-zed drops its library imports and
  rewrites onto the read surface: arm64 manifests by reading
  `images.<NAME>.platforms[linux/arm64]` from the committed YAML lock with
  plain `yaml.safe_load`; service tags through `boatyard tree-tags`; its
  `--build` mode through `boatyard build --push-to localhost:5000` (its
  `REGISTRY_PORT` constant; the forked bake pipeline dissolves). Its relock
  lands arm64 platform-manifest digests for loki/alloy — the box ships
  executable images and the ZED-box AOA deployment unblocks
  (`zed-aoa-diagnosis-handoff.md` gates the deploy on exactly this; update
  only its still-live instructions). The repin touches both declarations (the
  root exact pin and the `scripts/` member floor).
- **Make-it-Sing, lbe-toolkit** — standard renames; lbe-toolkit's floating uv
  tag (`ghcr.io/astral-sh/uv:python3.13-…`) becomes an `{uv}`-templated value
  in the same relock: intent stays authored, components derive.
- **infra-rathole** — README carries `uv run build --lock-only` (→ `uv run
  boatyard lock`) and the tree-layout table naming both files; AGENTS exact-pin
  prose restates the release pin in the docs pass.
- The reserved front-door service name is `edge` from 0.6.0 — devkit-derived,
  consumers edit nothing; the renamed container at next bring-up is expected.

Verify: consumer CI green end-to-end; grep over the five repos returns zero
`boatyard` imports; the placeframe regeneration diff committed and reviewed;
capture-tool's lock carries arm64 platform manifests; `up` probes pass against
the mirror; every toolkit `uses:` ref carries one SHA and every
`--lock-only`/`--gpu` mention is gone from consumer docs and workflows.

## Work 4 — Precision cluster: attribution-based tree tags (boatyard)

The tagging system this serves: two tag grammars, two jobs — `tree-{hash}` is
content identity on every channel; release channels add unit versions
(`{unit}-v{semver}` git tags, registry semver, `{base}-dev.{count}`) once Work
5 lands. Today `tree-` tags over-include (whole allowlist contexts), so they
are not yet a precise change signal. Three phases, one boatyard release, one
consumer re-key.

### Attribution parser and construct guard

A pure parsing layer in `dockerfiles.py`, grown from `derive_services`,
computes per service: the Dockerfile's `COPY`/`ADD` source paths and globs,
its `ARG` declarations, its build mapping as the record derives it
(dockerfile, target, platforms, rendered args, context edges — the derivation
itself is the serialization; tag lists are not inputs), and its
`additional_contexts` edges in `target:` form only. The Dockerfile is the
declaration; no input declarations join `boatyard.yml` — a config-declared
input list is a second source of truth that drifts, and verifying it against
the Dockerfile requires the parser anyway.

Every construct that can read the build context is either fully attributed or
a loud failure: path-form `additional_contexts` rejects; `RUN
--mount=type=bind,source=…` rejects; heredoc `COPY`, unknown syntax,
unresolvable globs reject; URL-form `ADD` rides (network channel, uncovered by
design — same category as apt). `COPY` sources outside {own dir} ∪ {copied
shared roots} ∪ {declared dependency outputs} reject. A bare-identifier `COPY
--from=` naming a sibling service the same file never `FROM`s refuses (it
passes silently today — load accepts what bake then breaks on), and the
independent-axis refusal text is corrected to match its behavior (any parented
service, variant-bearing or not).

### Attribution-based service hashes

- The closure walk (union-find over the derived context edges, inside
  `build_partitions`) lifts to a shared function in `dockerfiles.py` beside
  the model it walks; `build_partitions` and the hasher both consume it — the
  one-way import law holds (`dockerfiles` imports nothing from `config`).
- `compute_service_shas` hashes, per service: the Dockerfile itself (any
  recipe change re-keys, keeping the closure self-consistent — a new `COPY`
  line moves the hash at the same moment the newly copied files enter the
  resolved set); the resolved `COPY`/`ADD` set against the allowlisted
  committed tree (shared roots enter only when copied); the lock entries whose
  names intersect the Dockerfile's `ARG` declarations, serialized from the
  structured model (declared-but-unused args still hash; compose-runtime-only
  entries drop out of build hashes entirely); the service's build mapping; and
  each context dependency's computed tree hash embedded as a synthetic entry —
  hash-of-hash over the DAG, no circularity.
- `.dockerignore` is not a hash input: an edit that changes what a `COPY` glob
  matches re-keys exactly the affected services through the resolved set; an
  edit that matches nothing matched changed no bits, so no re-key is correct.
- Services sharing one Dockerfile dir hash identically (the variant difference
  is an arg inside the build mapping); a variant-arg edit re-keys both. An
  empty service set still returns `{}` without touching git.

Verify (the negative-test battery): a lock line for an undeclared base edited
→ unaffected services unchanged; another service's manifest arg edited → this
service unchanged; a shared-root file no service copies edited → no re-key; a
`.dockerignore` edit that matches nothing → no re-key; own-dir edit → re-key;
dependency-dir edit → dependent re-keys; declared-arg lock edit → re-key. And
one per loud failure and per attributed construct from the parser above.

### Dirty-tree guard

`build` and `up --build` run `git status --porcelain` filtered to the
allowlisted set (modified and untracked both count) and refuse loudly listing
the offending paths — the hash is over the committed tree while builds compile
the working tree, and post-precision this is the only remaining way a `tree-`
tag can lie. `build --dev` and bare `up` are exempt (dev tags are not
content-addressed; the bind-mount workflow runs dirty by design; bare `up`
pulls committed-tree tags and is self-consistent). `lock`, `generate-score`,
and `publish-compose` carry no guard (clean CI checkouts make it a no-op
there).

### Release and re-key wave

boatyard ships one patch release carrying the three phases above (pre-1.0;
breaking rides the patch flow). The image-building consumers — placeframe,
placeframe-capture-tool, Make-it-Sing — repin onto it before Work 5's change
detection ever runs: the toolkit compares tree tags recorded by consumer CI
builds, so those builds must compute precise tags at the certified head. The
repin is a `uv.lock` bump — no declaration changes, no lock rewrite. placeframe
runs one deliberate `stack/generated/**` regeneration cycle after the re-key
(every image gets a fresh `tree-` tag at its next build; same two-commit shape
as Work 3's cycle). Old `tree-` tags freeze in place — CI never re-pushes them,
so existing pins convert from movable to actually-immutable; infra's three
`tree-` pins keep resolving to frozen digests, re-pointed at leisure or at
Work 5's version tags. Calibration-corpus rows keep their old
`pipeline_version` values (the existing re-key hard-fail on redeploy stands).

Verify: consumer CI green end-to-end; the regeneration diff committed and
reviewed; a rebuild of an unchanged tree keeps its tag.

## Work 5 — Per-unit semver image and bundle lines (toolkit + bar + boatyard)

Version space: per-unit semver lines with tag-everything — every release
aliases the full image set (changed images at their new version, unchanged at
their existing one, a metadata no-op), so complete sets exist at every release
and "newest tag for image X" is always its highest semver. Semver's
compatibility contract maps to image surfaces (env holes, ports, service
names, volume layout): major = breaking contract, minor = additive, patch =
fix, human-asserted via `major_minor` — the packages' trust model. The CalVer
git tag stays the release-event grouping label; apps stay on their existing
per-app semver. Semver over CalVer was decided on ecosystem grounds
(semver-native consumers and bump machinery — Renovate's docker manager, Flux
`semver` policies, dependabot; org declarations already consume semver
third-parties).

Declaration surface: `publish.yml` carries `images:` — one entry per version
line, keyed by unit name (the bake service name minus the `-cuda`/`-rocm`
variant suffix), with required `major_minor` — and `bundles:` — name, ref(s),
`major_minor`, and `sources` (the compose source paths, stated nowhere else —
new information, not duplicated state; includes `boatyard.lock.yml`, so a
runtime third-party bump changes bundle bits without any local tree moving).
All variant repositories of a unit share the version tag and the
`{unit}-v{semver}` git tag. placeframe: 15 services / 13 units.

Change detection via the shelf — the release verb imports no boatyard: per
unit, resolve the highest `{unit}-v*` git tag → its commit → pull that SHA's
digest manifest from the shelf → compare the recorded `tree-` tags (recorded
in each entry's `tags[]` by the build push) against the certified head's. Any
variant's tree tag differs ⇒ ship and bump patch; the declared `major_minor`
sitting above the line forces the bump; no tag yet ⇒ first release ships. The
dev channel compares against the highest stable tag's commit, exactly like
package path-diff. Stable ships when any package, app, image unit, or bundle
changed; the dev gate tightens from manifest-presence to the same change-based
gate (a docs-only landing in an images-only repo cuts nothing). Digest-based
detection was rejected: rebuild churn re-digests unchanged trees and would
bump versions on noise.

Channels and spellings:

- Stable: alias each unit's variant digests onto `{version}` and `latest`;
  push the `{unit}-v{version}` git tag; alias the bundle's certified `{sha}`
  artifact onto `{version}`; push the `{bundle}-v{version}` git tag.
- Dev: alias changed units onto `{base}-dev.{count}` (npm spelling; valid tag
  grammar); unchanged units alias nothing (their stable tag already resolves);
  no git tags on dev.
- PR: no image version tags, permanently — ghcr deletes tags per
  digest-version, so any `pr-*` image tag sweep would endanger stable tags on
  shared digests. PR bodies keep `tree-` tag links.
- `latest` is aliased by stable only. PR builds stop pushing the derived
  `:latest` (today it points at uncertified heads — the one cell violating
  the PR-column principle); it becomes honestly "newest released version of
  this image". Dev never aliases it.
- Release notes: pr/dev/stable render from one spine; the images table is
  `Image | Version` (the changed/unchanged pattern of the packages table); a
  Bundle row links the ghcr version page — the human and Renovate discovery
  surface (consumers no longer hand-copy digests at all once Work 6 lands).

Aliasing mechanism (verified live against `registry:2` with the pinned oras
1.2.2): a new primitive in build-artifact-registry beside `promote_scope`
(org law: the toolkit owns no registry CLI code) — resolve `ref@digest` from
the shelf manifest, `oras tag` the new tags; multiple tags per call; missing
references fail loudly; aliases preserve digest and media type for OCI image
indexes and single manifests alike. ghcr itself was not push-tested from the
sandbox; `docker buildx imagetools create -t` is the documented one-line
fallback. Push-once discipline holds under the release mutex; the shelf is
SHA-keyed so re-runs alias identical digests.

ghcr semantics to respect: ghcr's model is version-level (a version = a
digest; tags are attributes; deleting a version kills all its tags). A stable
release's certified digest already carries its dev spelling, so dev image tags
can never be swept the way stable sweeps `dev-*` git releases — hence the
no-GC law above.

Delivery order:

1. build-artifact-registry: the aliasing primitive. Verify: stub-registry
   tests — index and manifest aliasing, multi-tag calls, missing-ref failure.
2. toolkit: `images:`/`bundles:` parsing, unit-tag suffix stripping, version
   math reuse (highest `{unit}-v*` + patch, forced publish on
   declared-line-above-tags), tag-everything. Verify: derivation, version
   math, schema refusals (missing `major_minor`, unknown keys).
3. toolkit: change detection per above. Verify: stubbed shelf — changed,
   unchanged, first-release, missing-shard; nothing-to-ship including images
   and bundles; the bundle-gate trio (image-advanced, source-diff, lock-diff).
4. toolkit: channels, aliasing, note surfaces. Verify: aliasing calls against
   a stub registry; `latest` on stable only; dev aliases changed units only;
   note rendering for the three channels.
5. boatyard + toolkit publish-side cleanup, as boatyard's second release of
   this effort: the bake rendering emits no derived `:latest` in push mode
   (local load-mode builds keep theirs); `publish-compose` publishes `{sha}`
   only — branch tags and the `--branch` flag are gone, `--plan` output
   unchanged; the toolkit's reusable workflow stops passing `--branch`.
   Consumers relock devkit and bump the toolkit pin in one commit (Work 7), so
   no CI window ever passes a dead flag. Verify: bake-rendering golden tests
   for push vs load mode; sha-only publish tests; the toolkit reusable
   renders without the flag.

## Work 6 — Qualified grammar, bundle pinning, overrides (boatyard, one release)

One boatyard consumer-surface release carrying all four phases below;
consumers relock only in Work 7's wave — the grammar flip breaks `.env`
spellings, so relock and flip are atomic per repo.

### Qualified selection names

`BUNDLES=` becomes qualified `group-dash-option`, uniformly, for every option
— no unqualified short forms even when unambiguous (one grammar rule beats a
conditional rule; a group gaining a second option would break every bare
spelling; one spelling per bundle greps). Bare entries are their own root and
ride unconditionally, as today. The flat-vocabulary collision law is repealed:
qualified names are structurally unique (group names unique as dict keys,
option names unique within group), so two groups may each carry a `cuda`
option and no hand-uniquing (`pf-cuda`) is forced on authors.
At-most-one-option-per-group refusal unchanged; unknown names refuse loudly
listing the qualified vocabulary; a bare entry's name refuses as unselectable.

### Ref grammar and lock entries

- `ref:` carries a tag, never a digest: a digest spelling refuses at config
  load — the hand-copy workflow this kills becomes unrepresentable, not merely
  discouraged. Any published channel spelling is a legal pin — stable semver
  (`0.3.1`), dev (`0.3.1-dev.42`), sha (`{40-hex}`) — matching how packages
  accept dev pins. Exactly one denylist entry: `latest` refuses (the sole
  mutable spelling, repointed at every stable release — the standing
  pin-everything law, not channel policing). Exact tags only, no range
  specifiers (`>=`, `.x`): Renovate's docker manager bumps exact pins exactly
  as it already does for `base_images` and charts; dev/prerelease pins get no
  automated bump (Renovate skips prereleases) — accepted, the same behavior
  package dev pins see. Flip condition for channel restrictions (certified-only
  pins): a first consumer policy that wants them; if restrictions ever come,
  they come everywhere at once (packages, images, bundles).
- Lock entries: `{QUALIFIED_NAME}_BUNDLE={reference}@sha256:…` — the qualified
  selection name upper-cased plus `_BUNDLE` (`PLACEFRAME_CUDA_BUNDLE`,
  `TUNNEL_RATHOLE_BUNDLE`, bare `LIVEKIT_BUNDLE`), bundles arch-neutral (no
  index/platforms structure). Written by the `lock` verb,
  declaration-incremental like every lock line (unchanged tag copies its
  frozen digest forward; tag bump re-resolves; `--upgrade` re-resolves
  everything); `_BUNDLE` names collision-refuse against `base_images` keys at
  load. A version-tagged ref without its lock entry refuses loudly naming
  `boatyard lock` — the charts law, verbatim.
- First-party artifacts resolve direct — org ghcr refs, un-prefixed; the
  mirror is for upstreams that can hurt us; ours can't. The `mirror` verb
  never enumerates bundle declarations. (The absorbed plan's standing default
  "org-internal rides the mirror" never shipped and is reversed: two
  resolutions for the same artifacts at two levels of one pull — bundle
  mirrored, its embedded images direct — is incoherence, and mirroring
  ourselves buys insurance against our own discipline at doubled storage.)
- `up`'s fail-closed probe stays one law over every lock entry; bundle entries
  probe via `crane digest` (compose-bundle media types are not image indexes;
  `buildx imagetools inspect` may refuse them), under the same
  stderr-classification law as image probes. `up` includes the lock's digest
  ref; the yaml tag is never used directly at bring-up.
- The loop this buys: producer release mints `{version}` on the certified
  bundle digest; the consumer's upgrade is a yaml tag bump (Renovate-native)
  plus a relock, with the digest movement visible as a one-line lock diff in
  review — no registry pages, no hand-copying, no drift.

### Local bundle overrides (Stage 1)

`.env` may carry `BUNDLE_OVERRIDE_<QUALIFIED_NAME>` per machine — never
committed, one variable per override, commentable per-line, the same qualified
identity as `BUNDLES=` and the lock key. Value grammar: a filesystem path
(relative resolves against the consumer repo root, so sibling checkouts work
anywhere; absolute legal) or an `oci://` ref of any channel — the ref form
tests a colleague's landed-but-unreleased bundle without local builds and
without dirtying the tree. Unknown names refuse loudly listing the vocabulary;
a known-but-unselected name is inert — the override rides with selection.

An overridden option skips its lock-entry fail-closed probe and its
lock-entry requirement — testing a bundle that may not be published at any
digest yet is the point. The path form gets the same teeth as a local bundle
output: must exist and parse, refuses an `edge` service, service-name
collisions with other selected sources refuse, route/port validation runs
unchanged. Producer-lock attachment (path form only): the bundle's
third-party holes (`${LOKI_IMAGE:?err}`, …) have no declaration source in the
consumer, and lock lines are never hand-authored — so the producer's
`boatyard.lock.yml` attaches as an extra `--env-file`, derived
deterministically from the override path's fixed
`stack/generated/compose/` layout (three parents up), ordered so the
consumer's own declarations win any shared key (pin the precedence against
the version-pinned compose binary in tests). The ref form attaches nothing —
registry bundles had their holes filled at publish.

The iteration loop this buys: commit in the producer → `generate-score` +
`build` (tree-sha-tagged images land in the local daemon) → `uv run up` in
the consumer. `up`'s `--pull missing` already serves local-only tree tags.
Tree shas hash `git ls-tree HEAD`, so the unit of iteration is commit →
build, not save → reup; the expensive part of the old loop (push, CI,
digest-copy) is what disappears.

Verify per phase: qualified resolution incl. same-named options in two groups
and the refusal list; digest/`latest`/range refusals; lock-key derivation
including bare entries; declaration-incremental copy-forward, tag-bump
re-resolve, `--upgrade`; collision refusal; missing-entry refusal naming the
relock; bundle probe classification; materialized root carries digest refs;
both override value forms; the inert override; probe and requirement skip;
producer-lock attachment and ordering; `edge` and collision teeth.

## Work 7 — Adoption wave

One commit per consumer repo, onto Works 5–6. The Work-3 relock and its
regeneration rode the consumer-contract wave; the Work-4 re-key rode the
precision wave; the items here are strictly the Work-5/6 additions.

- **placeframe** — `publish.yml` gains `images:` (13 units) and `bundles:`
  (`placeframe`, variants cuda/rocm, compose source paths); one adoption
  commit: relock onto the Work-6 devkit release, bump the toolkit pin, flip
  `.env` `BUNDLES=` to qualified spellings
  (`core-cuda,auth-keycloak,tunnel-ngrok`); no `--branch` usage survives. The
  unused `.dockerignore` allowlist entry `packages/generated/csharp/api-client/`
  is removed — it re-keys nothing under precision and shrinks every build
  context. The first release after adoption mints image and bundle versions;
  the release body carries the `Image | Version` table and the bundle row.
- **Make-it-Sing** — `publish.yml` gains `images:` for `livekit-token`;
  `:latest` references gone; `.env` flips to `BUNDLES=placeframe-cuda,tunnel-rathole`;
  the yaml bundle ref flips from digest to version tag; the `lock` verb
  writes `PLACEFRAME_CUDA_BUNDLE` and `TUNNEL_RATHOLE_BUNDLE`. Tag hygiene:
  stale git releases and tags `pr-4-237`, `pr-4-238`, `pr-6-250` deleted;
  `pr-5-248` and `pr-7-254` checked against still-open PRs first and deleted
  if orphaned (deletion needs contents-write — hand off to the operator if
  the session token cannot).
- **placeframe-capture-tool** — `images:` for its workloads; `:latest`
  removal; `.env` flip; relock onto the Work-6 release.
- **infra** — its three `tree-` pins switch to version tags; no deadline (the
  frozen tree pins remain valid indefinitely).

Verify: CI green per repo through the first release; versions resolvable in
ghcr; the stale tags gone; the `packages/generated` entry gone from
placeframe's `.dockerignore`.

## Work 8 — Cross-repo dev overlay (trigger-gated)

Build only when the trigger holds: the Work-6 commit → rebuild loop proves
too slow in daily consumer-driven development. A session taking this work
first confirms the trigger; otherwise it does not run.

With a path-form override active, `up --dev` generates overlay entries from
the producer's declaration: the producer's dev-stage services, the producer's
repo root as the bind source (the same derivation the producer-lock
attachment uses), and the producer's `dev-toggles` as `${NAME:-default}` holes
— overlaid onto the consumer's assembly. `:dev` images come from the
producer's `build --dev`.

Verify: overlay generation over a producer/consumer fixture pair; toggle
holes present; bind sources point at the producer root.

## Dismissed and deferred

- Image-attached contract metadata / lock-as-DAG-resolver: ruled out — beyond
  state of practice and redundant with the compose layer, which already
  expresses the dependency graph. Semver tags are the substrate; the lock
  resolves tags to digests for org artifacts exactly as for third parties.
- App CalVer conversion retired (consistent with images staying semver);
  per-image CalVer aliases available later as free additional tags.
- Range specifiers for bundle refs: rejected — exact tags plus Renovate is
  the docker/chart-native pattern; ranges drag resolver semantics into the
  devkit.
- Channel-restriction policy: deferred with its flip condition recorded in
  Work 6.

## Open drill — GPU template collapse [LOW, operator's lead]

The cuda/rocm pair: one annotation (`outernet.internal/gpu`), two compose
templates (`gpu-cuda.tpl`, `gpu-rocm.tpl`), one registered, selected by the
`GPU_VARIANT` build axis; the k8s analog (`gpu-pod.tpl`) is one template —
proof the one-template shape is natural. The operator believes a consolidation
key is unfound; the agent does not believe this collapses cleanly. Drill
continues.

Settled facts (do not re-derive): the score-compose patch-template context is
`.Compose` + `.Workloads` only — no env, no rendering vars (sprig helpers
available); the score-k8s context adds `.Manifests` + `.Namespace`; patch
templates fire after conversion/provisioning, before the final file is
written. `GPU_VARIANT` is a rendering-level variable that never enters the
template context; the only channel to thread it in is the annotation value at
render time (compose-only; k8s has no `GPU_VARIANT`), which couples the
shared `rendered_workloads` to the gpu annotation — a real cost. If the key
is found, the compose templates list becomes a fixed `[healthcheck, restart,
gpu]` and the templates param a plain per-substrate constant; if not,
templates stay computed at the call site, which is the landed shape — the
unified `render_score` is correct either way.
