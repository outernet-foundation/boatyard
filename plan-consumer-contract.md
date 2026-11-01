# The consumer contract: CLI, declarations, and artifacts only

Pre-implementation ADR, captured 2026-11-01 at the end of the
import-elimination design session. The decisions below are locked; the one
open item (O1, the catch-up archaeology) is execution-scoped — it runs as the
first phase of the implementation plan
(`plan-consumer-contract-implementation.md`), and its findings fold back into
that plan's catch-up phase before the wave runs. This ADR absorbs Parts 0 and
1 of `plan-artifact-identity.md` (see "Absorption"); that file's Parts 2–4
remain paused with it.

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

| Class | Examples | Lives in |
|---|---|---|
| Authored | repository, platforms, `base_images` tags | `docker-devkit.yaml` |
| Registry-resolved | digests | `docker-devkit.lock` |
| Tree-derived | service set, `tree-` sha tags | computed on demand, recorded nowhere |

Drift surface = independently-authored sources for one fact. Derived
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
  GOLANG_IMAGE: "docker.io/library/golang:1.22-alpine"   # plain — declared nowhere else
```

`{uv}` resolves from `tool.uv.required-version` (operator-stripped; a range
where an exact pin is required refuses loudly), `{python}` from
`.python-version`. uv — owning both files — already enforces the
range-versus-selection consistency between `requires-python` and
`.python-version` natively, so no coupling remains anywhere. The vendor's tag
grammar stays authored data (the template string); the devkit implements only
generic substitution, extending the existing `lock:`/`lock-image:` marker
philosophy from lock facts to toolchain facts. Plain values stay plain (the
golang/caddy/zed class — a tag declared nowhere else has no drift surface).
A literal value in place of a template is the deliberate-skew escape hatch,
visible in every diff. A template composing a nonexistent tag fails at
`build --lock-only` — fail-closed, same as a mistyped literal today.

Rationale: the version-coupling machinery treated symptoms of a
misclassification — the uv image tag is a *function of* decisions already made
in pyproject and `.python-version`, not an independent decision. It is why the
registry only ever contained uv and python entries, why wiring it cost enough
that four of five consumers never did, and why capture-tool quietly
accumulated four authored sources for the python version.

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
banned there is no pair of authored sources left to drift anywhere in the
system.)

### D6 — The read surface is four sources

1. `docker-devkit.yaml` — authored facts; direct read.
2. `docker-devkit.lock` — resolved facts, structured YAML per absorbed Part 0;
   direct read with plain `yaml.safe_load` (the env-line format that forced
   `parse_env_file` imports dies here). install-zed selects its arm64 manifest by
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
`uv run build --registry <addr>`. Rulings, as design consequences rather than
choices: `--registry` implies push mode — a retargeted build has nowhere to go
but the registry, and a `--load` pairing is meaningless and refuses; cache
refs follow the retarget (`{registry}/build-cache:{target}` — one law
everywhere: cache rides the same registry the tags do; the persistent laptop
registry gives the install iteration loop the same layer caching ghcr builds
get); the box-facing address spelling stays consumer-side — install-zed
derives it from `$SSH_CLIENT` as it already does. Flag name is plan-session
material.

### D8 — Consumers own their env-line reading

`.env` wears two hats: the devkit's interpolation input at bring-up, and the
repo's single per-machine location for deployment facts (`PUBLIC_URL`, S3
credentials) that client tooling reads directly — calibration scripts hitting
the API, helper containers on the stack network. Reading the one file is
single-sourcing, not restating. The parser is a tiny consumer-local module
implementing compose's env-file grammar (`KEY=VALUE`, `#` comments, no
interpolation — compose is the file's authoritative reader, so its rules are
the spec), filename hardcoded exactly as consumers already do for `.env`. No
shared micro-package, no dotenv dependency (its interpolation and `export`
semantics diverge from compose). Lock reading after Part 0 is plain YAML and
needs nothing. Post-dissolution inventory: placeframe's `api_auth.py` and
`sweep_postprocess.py`; the devkit's own `parse_env_file` stays internal to
`assembly.py`.

### D9 — Consumer catch-up: one wave

All five consumers repin once, onto a single devkit release carrying this
ADR's devkit-side work plus absorbed Part 0 (Part 1 follows at Plan B's
opening; see Absorption). Known consumer-side work:
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
relock-and-catch-up wave; riding P0 on it costs zero additional waves and
lands every read-surface change (YAML lock, `lock-env`,
tag lookup, templates) in the single release consumers pin once. Part 0 also fixes
capture-tool's live per-arch defect (index digests vs arm64 platform
manifests — boxes
currently ship unexecutable amd64 loki/alloy).

Amendments recorded against the absorbed material:

- Part 1 executes at the opening of `plan-artifact-identity-implementation.md`
  (Plan B), not in this initiative: Plan B's change detection hard-depends on
  source-precise tree tags, and splitting the precision lift out lets this
  initiative's release ship sooner. Cost: a second consumer repin and a
  second placeframe regeneration at Plan B's precision wave — the
  zero-additional-waves rationale above held only while P1 rode this
  release. The D9 release therefore carries Part 0 only.

- No consumer calls the `entry_for` selection function — it is devkit-internal
  glue; consumers read the committed YAML or `lock-env` (the D1 law over the
  ADR's "install-zed calls it" phrasing).
- The implementation plan's module anchors predate the refactor
  (`build_docker.py`, `compose.py`, `documents.py` members have moved); the plan
  rewrite absorbs the renames.
- The current AGENTS law "the env line format is the design, not a vestige" is
  reversed by this absorption; the AGENTS lock section is rewritten when Part
  0 lands (synthesis at the edges preserves both documented virtues).

Parts 2–4 (semver lines, bundle pinning, overrides) stay paused with the
parent ADR.

## Construction over policing — the session's axiom

Every drift surface was presumed eliminable by construction; policing was
admitted only where logically forced. Outcomes: uv and python versions —
eliminated (D3); distro suffix — authored in exactly one place; floating uv tag
(lbe-toolkit) — intent stays authored, components derive; golang-class tags —
no surface; literal refs — the escape-hatch class banned outright (D5), the
one place detection remains, reframed as format validity. No cross-file
invariant check survives anywhere in the design.

## The plan pair

Two ADRs and two implementation plans, each self-contained:

1. **Plan A** — `plan-consumer-contract-implementation.md` — implements this
   ADR: D3–D7 devkit work; the absorbed Part 0, with its design detail
   lifted in (resolver selection rules, migration, test batteries); the
   consumer catch-up (D9); and the O1 archaeology as its first phase. It
   locks the names this ADR deferred to plan-session material: the tag-lookup
   verb is `tree-tags`, the retarget flag is `--registry`.
2. **Plan B** — `plan-artifact-identity-implementation.md` — opens with the
   absorbed Part 1 precision cluster (attribution parser, precise hashes,
   dirty-tree guard, release, consumer re-key wave — phases 8–11) and then
   executes Parts 2–4 (phases 12–25) over the P0 foundation Plan A delivers;
   its session-start checkout table is re-derived at this initiative's
   release boundary, before its first session opens.
3. `plan-artifact-identity.md` is the Parts 2–4 decision record; this ADR's
   Absorption section holds the Part 0/1 amendments.
4. Both plans carry refactor-true module anchors (`bake.py`,
   `assembly.py`/`verbs/`, `config.py`, `service_builds.py`; `host.py` is
   dissolved).

Archaeology starting point (verified at capture): all five consumers pin
`docker-devkit==0.4.9.dev165` — the latest published; the refactor's 0.5.9 is
unpublished. Library imports live in placeframe's `scripts/preflight.py`,
`scripts/src/scripts/{api_auth,sweep_postprocess}.py`,
`scripts/tests/test_score_{compose,k8s}_generation.py`, and capture-tool's
`scripts/src/scripts/install_zed/{orchestrator,box_install,constants}.py` —
each maps to a D-decision dissolution; re-verify with grep before editing.

## Open question

- **O1 — catch-up archaeology.** The dev165→final diff across CLI, schema,
  lock format, and artifact shapes may surface consumer edits beyond D9's
  enumeration; it executes as Plan A's first phase, and findings fold back
  into that plan's catch-up phase before the wave runs.
