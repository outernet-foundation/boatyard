# The consumer contract: CLI, declarations, and artifacts only

Pre-implementation ADR, captured 2026-11-01 at the end of the
import-elimination design session. The decisions below are locked; the open
questions at the end are the design's unfinished surface, answered in follow-up
sessions before any implementation plan (a separate session's artifact) is
written. This ADR absorbs Parts 0 and 1 of `plan-artifact-identity.md` (see
"Absorption"); that file's Parts 2–4 remain paused with it.

## Orientation: the problem

Every consumer repo resolves `docker-devkit==0.4.9.dev165` — also the latest
published PyPI version — while the refactor branch sits at `0.5.9` across 189
commits. Consumer code was deliberately left stale through the refactor, and
the breakages were never recorded; part of this initiative is the
archaeological catch-up.

The deeper problem the session exposed: the Python library was an
implementation with accidental customers. ~15 import sites across
placeframe/scripts and placeframe-capture-tool/scripts consumed module
internals (`docker_devkit.host`, `documents.compute_service_shas`,
`score_generate`, the `compose_bundles` free function) that the refactor
renamed wholesale — while the CLI surface (verbs, flags, declaration schema,
artifact formats) did not move at all. Nothing in `AGENTS.md` ever documented
a library API; the documented contract was always the verbs. The refactor is
the empirical proof: it broke 100% of library imports without touching the
contract consumers were supposed to depend on.

Consumer inventory (the full blast radius):

| Repo | Consumption |
|---|---|
| placeframe | CLI (via workspace member dep) + library imports in `scripts/preflight.py`, two utility scripts, two test modules |
| placeframe-capture-tool | CLI + library imports in `scripts/src/scripts/install_zed/` (orchestrator, box_install, constants) |
| infra-rathole, lbe-toolkit, Make-it-Sing | CLI only; root `docker-devkit.yaml` + lock; no imports |
| github-actions-toolkit | CLI only, invoked inside its reusable workflows; never a dependency (the resolver-cycle law) |

## The three-class principle

The lens every decision below follows:

| Class | Examples | Home |
|---|---|---|
| Authored | repository, platforms, `base_images` tags | `docker-devkit.yaml` |
| Registry-resolved | digests | `docker-devkit.lock` |
| Tree-derived | service set, `tree-` sha tags | computed on demand, recorded nowhere |

Drift surface = independently-authored homes for one fact. Derived
restatement costs nothing (regeneration is deterministic). Classes never mix
files: authored facts in the config, resolved facts in the lock, tree-derived
facts computed. The tree-derived class can never live in a committed file —
tree-shas churn on every workload edit, and committing them would erect a
staleness gate on every commit (the disease placeframe already killed once in
its disabled score-staleness gate).

## Decisions

### D1 — The library is internal; consumption is CLI, declarations, artifacts

docker-devkit's public contracts are exactly three: the verb CLI, the
declaration schema (`docker-devkit.yaml` + `docker-devkit.lock`), and the
committed artifact formats (`stack/generated/**`). Every Python import of
`docker_devkit` in consuming repos is eliminated. The AGENTS release-flow law
is restated accordingly: what ships with a bumped `major_minor` from 1.0 on is
schema/CLI/artifact-breaking change — there is no Python API to break. The
stale "pulsar's bake_bundle" consumer reference in the `publish.py` row dies
with the same docs pass.

### D2 — The domain-record refactor is frozen at 372fa45

No further consolidation arc. The AGENTS law is satisfied by the audit run in
this session (score.py and publish.py hold pipelines, validators, and
sole-consumer helpers — all legally placed); any straggler derivation surfaces
during this initiative's work folds as ordinary work, not a reopened arc.

### D3 — Toolchain components in `base_images` derive by construction

`base_images` values may be templates over the repo's toolchain pins:

```yaml
base_images:
  UV_BASE_IMAGE: "ghcr.io/astral-sh/uv:{uv}-python{python}-trixie-slim"
  PYTHON_BASE_IMAGE: "docker.io/library/python:{python}-slim"
  GOLANG_IMAGE: "docker.io/library/golang:1.22-alpine"   # plain — no second home
```

`{uv}` resolves from `tool.uv.required-version` (operator-stripped; a range
where an exact pin is required refuses loudly), `{python}` from
`.python-version`. uv — owning both files — already enforces the
range-versus-selection consistency between `requires-python` and
`.python-version` natively, so no coupling remains anywhere. The vendor's tag
grammar stays authored data (the template string); the devkit implements only
generic substitution, extending the existing `lock:`/`lock-image:` marker
philosophy from lock facts to toolchain facts. Plain values stay plain (the
golang/caddy/zed class — tags with no second home have no drift surface).
A literal value in place of a template is the deliberate-skew escape hatch,
visible in every diff. A template composing a nonexistent tag fails at
`build --lock-only` — fail-closed, same as a mistyped literal today.

Rationale: the version-coupling machinery treated symptoms of a
misclassification — the uv image tag is a *function of* decisions already made
in pyproject and `.python-version`, not an independent decision. It is why the
registry only ever contained uv and python entries, why wiring it cost enough
that four of five consumers never did, and why capture-tool quietly
accumulated four authored homes for the python version.

### D4 — No version-coupling registry anywhere

`VersionCoupling`/`VersionSite` and `version_coupling_violations` die. D3
eliminates every coupling they covered by construction; policing an invariant
that construction makes unreachable is dead weight. The registry-as-sidecar
design considered mid-session is superseded.

### D5 — Literal image references are refused at derivation time

In devkit-owned trees, hand-written literal image references (`FROM`,
`COPY --from=`, yaml `image:`) are refused outright — use `base_images` ARGs
or lock markers. Empirical basis: zero literal refs exist in any consumer
tree today; the old pinned-literals-allowed scan permitted a class nobody
uses. Enforcement lives at parse boundaries the devkit already owns
(`derive_services` parses every `FROM` for context edges; score reads every
`image:` line) — a validity rule of the input format, the same category as
`extra="forbid"`, not drift policing. (Strictly, file formats permit literals
and docker builds them; detection is logically forced — but with literals
banned there is no pair of authored homes left to drift anywhere in the
system.)

### D6 — The read surface is four homes

1. `docker-devkit.yaml` — authored facts; direct read.
2. `docker-devkit.lock` — resolved facts, structured YAML per absorbed Part 0;
   direct read with plain `yaml.safe_load` (the env-line format that forced
   `parse_env_file` imports dies here). install-zed selects its arm64 dish by
   reading `images.<NAME>.platforms[linux/arm64]` directly.
3. The tag-lookup verb — `uv run <verb> <service>…`: positional service
   names, one bare `tree-` tag per line on stdout, unknown names refuse
   loudly listing the vocabulary. The whole contract is a string, not a
   schema: no JSON document, no service-list endpoint, no `{SERVICE}_SHA`
   names — consumers that write `.env` lines own those names where they
   author the compose holes. Deterministic function of HEAD, never cached,
   never committed. Exact verb name is plan-session material.
4. `lock-env` — the env-grammar projection of the lock for the compose/bake/
   raw-`docker compose` valves (absorbed Part 0 design).

`docker-build-matrix` and `publish-compose --plan` remain as-is: frozen CI
contracts with the toolkit binding.

### D7 — `build` grows a registry-retarget knob

install-zed's `--build` mode currently forks the build pipeline in consumer
code (renders its own bake file, assembles its own env, `--set`-retargets tags
to a transient host-local registry — a comment-synced mirror of the build
verb). The verb grows the retarget flag; the fork dissolves into
`uv run build --registry … --push`. Exact spelling is plan-session material;
the direction (one pipeline, configurable destination) is locked.

### D8 — Consumers own their env-line reading

`.env` reading becomes a tiny consumer-local module (the repo's own file
convention; hardcode the filename exactly as consumers already do for
`.env`). No shared micro-package, no dotenv dependency. Lock reading after
Part 0 is plain YAML and needs nothing.

### D9 — Consumer catch-up: one wave

All five consumers repin once, onto a single devkit release carrying this
ADR's devkit-side work plus absorbed Parts 0–1. Known consumer-side work:
placeframe's `scripts/preflight.py` drops its four imports and the
`VERSION_COUPLINGS` list; its two score-generation test modules rewrite to
assert against committed artifacts (freshness is already gated by
`generate-score --check` in CI); install-zed rewrites onto YAML-lock reads,
the tag-lookup verb, and `build --registry`; `platforms` declarations complete
(placeframe amd64, Make-it-Sing) under Part 0's config refusal; placeframe
runs one deliberate `stack/generated/**` regeneration cycle after the Part 1
re-key. The full dev165→final contract diff (CLI, schema, lock format,
artifacts) is owed archaeology — mechanical, but it may surface additional
consumer edits this ADR does not enumerate.

## Absorption: Parts 0–1 of `plan-artifact-identity.md`

The platform-resolved structured-YAML lock (Part 0) and the attribution-based
tree-hash precision (Part 1) — implementation phases 1–7 of
`plan-artifact-identity-implementation.md` — are absorbed into this
initiative. Rationale: the session already owes consumers exactly one
relock-and-catch-up wave; riding P0+P1 on it costs zero additional waves and
lands every read-surface change (YAML lock, `lock-env`,
tag lookup, templates) in the single release consumers pin once. Part 0 also fixes
capture-tool's live per-arch defect (index digests vs arm64 dishes — boxes
currently ship unexecutable amd64 loki/alloy).

Amendments recorded against the absorbed material:

- No consumer calls the `entry_for` selection function — it is devkit-internal
  glue; consumers read the committed YAML or `lock-env` (the D1 law over the
  ADR's "install-zed calls it" phrasing).
- The implementation plan's module anchors predate the refactor
  (`build_docker.py`, `compose.py`, `documents.py` homes have moved); the plan
  rewrite absorbs the renames.
- The current AGENTS law "the env line format is the design, not a vestige" is
  reversed by this absorption; the AGENTS lock section is rewritten when Part
  0 lands (synthesis at the edges preserves both documented virtues).

Parts 2–4 (semver lines, bundle pinning, overrides) stay paused with the
parent ADR.

## Construction over policing — the session's axiom

Every drift surface was presumed eliminable by construction; policing was
admitted only where logically forced. Outcomes: uv and python versions —
eliminated (D3); distro suffix — never a second home; floating uv tag
(lbe-toolkit) — intent stays authored, components derive; golang-class tags —
no surface; literal refs — the escape-hatch class banned outright (D5), the
one place detection remains, reframed as format validity. No cross-file
invariant check survives anywhere in the design.

## Open questions

These are answered in follow-up sessions before the implementation plan is
written; the design is not locked until they close.

- **O1 — the `.env` reader home.** D8's consumer-owned-module recommendation
  is favored but was not explicitly locked in-session.
- **O2 — `build --registry` spelling.** Direction locked (D7); flag name,
  interaction with `--push`/cache refs, and the host-vs-box address split are
  plan-session details.
- **O3 — catch-up archaeology.** The dev165→final diff across CLI, schema,
  lock format, and artifact shapes may surface consumer edits beyond D9's
  enumeration; findings fold back into the plan.
