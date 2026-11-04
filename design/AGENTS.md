# design

Pre-implementation designs for un-built or cross-cutting initiatives — one
initiative per file, plain git-tracked Markdown. Never a source of truth
about shipped code: that is the parent `AGENTS.md`. Expected to go stale;
when a design is built, its hardened parts fold into the co-located
`AGENTS.md` and the file is deleted.

Active:

- precision-attribution.md — attribution-based tree tags over the disk-state
  basis; precedes semver by construction
- semver-image-bundle-lines.md — per-unit image + bundle version lines
  (toolkit + build-artifact-registry + boatyard)
- adoption-semver-wave.md — consumer adoption of the semver work
- dev-overlay.md — cross-repo dev overlay, trigger-gated on rebuild latency

Parked:

- apt-hermeticity.md — hermetic apt in image builds
- mirror-digest-retention.md — mirror digest retention policy
- gpu-template-collapse.md — open drill, operator's lead
- bundle-manifest-contract.md — derived bundle manifests in the lock; name
  and env validation moves to load; up's compose checks deleted
