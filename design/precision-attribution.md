# Precision cluster: attribution-based tree tags (boatyard)

Pre-implementation design. Prerequisite: the closeout critical path has
landed (consumers compute disk-state tree tags). Precedes the semver
image/bundle lines by construction: that change gate compares tree tags, and
tags are a trustworthy change signal only once this makes them source-precise
— every over-inclusion is a spurious version bump.

Two tag grammars, two jobs — `tree-{hash}` is content identity on every
channel; release channels add unit versions (`{unit}-v{semver}` git tags,
registry semver, `{base}-dev.{count}`) once the semver design lands.
Post-sweep tags are disk-state but over-inclusive (whole allowlist contexts),
so they are not yet a precise change signal. Three phases, one boatyard
release, one consumer regeneration.

## Attribution parser and construct guard

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

## Attribution-based service hashes

- The closure walk (union-find over the derived context edges, inside
  `build_partitions`) lifts to a shared function in `dockerfiles.py` beside
  the model it walks; `build_partitions` and the hasher both consume it — the
  one-way import law holds (`dockerfiles` imports nothing from `config`).
- `compute_service_shas` hashes, per service: the Dockerfile itself (any
  recipe change re-keys, keeping the closure self-consistent — a new `COPY`
  line moves the hash at the same moment the newly copied files enter the
  resolved set); the resolved `COPY`/`ADD` set read from the working tree
  under the allowlist (shared roots enter only when copied); the lock entries
  whose names intersect the Dockerfile's `ARG` declarations, serialized from
  the structured model (declared-but-unused args still hash; compose-runtime-only
  entries drop out of build hashes entirely); the service's build mapping; and
  each context dependency's computed tree hash embedded as a synthetic entry —
  hash-of-hash over the DAG, no circularity.
- `.dockerignore` is not a hash input: an edit that changes what a `COPY` glob
  matches re-keys exactly the affected services through the resolved set; an
  edit that matches nothing matched changed no bits, so no re-key is correct.
- Services sharing one Dockerfile dir hash identically (the variant difference
  is an arg inside the build mapping); a variant-arg edit re-keys both. An
  empty service set still returns `{}` without walking.

The dirty-tree guard an earlier plan carried was dissolved by the disk-state
basis: the hash reads what the build compiles, dirt is content, nothing
refuses. Do not reintroduce one.

Verify (the negative-test battery): a lock line for an undeclared base edited
→ unaffected services unchanged; another service's manifest arg edited → this
service unchanged; a shared-root file no service copies edited → no re-key; a
`.dockerignore` edit that matches nothing → no re-key; own-dir edit → re-key;
dependency-dir edit → dependent re-keys; declared-arg lock edit → re-key. And
one per loud failure and per attributed construct from the parser above.

## Release and regeneration

boatyard ships one patch release carrying the two phases above (pre-1.0;
breaking rides the patch flow). The image-building consumers — placeframe,
placeframe-capture-tool, Make-it-Sing — repin onto it before the semver change
detection ever runs: the toolkit compares tree tags recorded by consumer CI
builds, so those builds must compute precise tags at the certified head. The
repin is a `uv.lock` bump — no declaration changes, no lock rewrite. placeframe
runs one deliberate `stack/generated/**` regeneration cycle after the re-key
(every image gets a fresh `tree-` tag at its next build; same two-commit shape
as the sweep's cycle). Old `tree-` tags freeze in place — CI never re-pushes
them, so existing pins convert from movable to actually-immutable; infra's
three `tree-` pins keep resolving to frozen digests, re-pointed at leisure or
at the semver version tags. Calibration-corpus rows keep their old
`pipeline_version` values (the existing re-key hard-fail on redeploy stands).

Verify: consumer CI green end-to-end; the regeneration diff committed and
reviewed; a rebuild of an unchanged tree keeps its tag.
