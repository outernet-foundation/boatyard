# Per-unit semver image and bundle lines (toolkit + bar + boatyard)

Pre-implementation design. Prerequisite: precision attribution has landed —
the change gate below compares tree tags, and tags are a trustworthy change
signal only once they are source-precise. One release; consumer adoption
lives in the adoption-wave design.

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
  surface (consumers no longer hand-copy digests at all once this lands).

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
standing no-GC law.

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
   Consumers relock devkit and bump the toolkit pin in one commit (the
   adoption wave), so no CI window ever passes a dead flag. Verify:
   bake-rendering golden tests for push vs load mode; sha-only publish tests;
   the toolkit reusable renders without the flag.

## Closed decisions

- Image-attached contract metadata / lock-as-DAG-resolver: ruled out — beyond
  state of practice and redundant with the compose layer, which already
  expresses the dependency graph. Semver tags are the substrate; the lock
  resolves tags to digests for org artifacts exactly as for third parties.
- App CalVer conversion retired (consistent with images staying semver);
  per-image CalVer aliases available later as free additional tags.
