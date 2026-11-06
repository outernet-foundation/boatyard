# Adoption wave: semver image and bundle lines

Pre-implementation design. Prerequisite: the semver image/bundle lines have
landed. One commit per consumer repo; the sweep already carried the grammar
adoption (qualified `.env`, tag-spelled refs, `_BUNDLE` lock entries,
overrides in Make-it-Sing) — the items here are strictly the semver
additions.

- **placeframe** — `publish.yml` gains `images:` (13 units) and `bundles:`
  (`placeframe`, variants cuda/rocm, compose source paths); one adoption
  commit: relock onto the semver devkit release, bump the toolkit pin. The
  unused `.dockerignore` allowlist entry `packages/generated/csharp/api-client/`
  is removed — it re-keys nothing under precision and shrinks every build
  context. The first release after adoption mints image and bundle versions;
  the release body carries the `Image | Version` table and the bundle row.
- **Make-it-Sing** — `publish.yml` gains `images:` for `livekit-token`;
  `:latest` references gone; the yaml bundle ref flips from sha tag to
  version tag; the `lock` verb re-resolves `PLACEFRAME_CUDA_BUNDLE` and
  `TUNNEL_RATHOLE_BUNDLE` onto the version pins. Tag hygiene:
  stale git releases and tags `pr-4-237`, `pr-4-238`, `pr-6-250` deleted;
  `pr-5-248` and `pr-7-254` checked against still-open PRs first and deleted
  if orphaned (deletion needs contents-write — hand off to the operator if
  the session token cannot).
- **placeframe-capture-tool** — `images:` for its workloads; `:latest`
  removal; relock onto the semver release.
- **infra** — its three `tree-` pins switch to version tags; no deadline (the
  frozen tree pins remain valid indefinitely).

Verify: CI green per repo through the first release; versions resolvable in
ghcr; the stale tags gone; the `packages/generated` entry gone from
placeframe's `.dockerignore`.
