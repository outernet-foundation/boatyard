# Closeout: the 0.6.0 critical path

This file is the critical path only — the work that must ship imminently, in
one release and one consumer sweep: the iteration unit (disk-state service
hashes, qualified bundle grammar, path-form overrides), the 0.6.0 release,
the toolkit cutover, the consumer sweep. Bang through it; when the four
units land, delete this file. Future initiatives live in `design/` (one
initiative per file, indexed by `design/AGENTS.md`); the six consolidated
plan files this record absorbed live in git history. `AGENTS.md` is the
present-truth operations manual for the shipped surface. Every decision
below is closed — do not re-litigate without its recorded flip condition. If
this file and the code disagree, reconcile this file before touching code
(spec-first). One phase per session — never a whole work unit in one go;
verify with the target repo's battery; commit as you go, prose and code in
separate commits.

## Snapshot (verified 2026-11-03)

- boatyard branch `refactor`: all consumer-contract work, the module-tiers
  refactor, the score unification, the boatyard rename + CLI refactors, and
  Work 1 in full — the disk-state service-hash phase (design signed off,
  battery green, `pathspec` dropped), the qualified-selection-names phase
  (qualified `BUNDLES=` grammar, flat vocabulary law repealed into a
  qualified-vocabulary collision check, options identified by qualified name
  in bundle outputs, publish legs, and selection), the ref-grammar phase
  (tag-only `ref:` grammar with digest/`latest`/range refusals;
  `{QUALIFIED_NAME}_BUNDLE` lock entries written by the lock verb, resolving
  direct via `crane digest`, declaration-incremental; cross-section collision
  refusal; missing-entry refusal naming the relock; bundle probes via crane
  under the stderr-classification law; includes carry the lock's digest ref),
  and the bundle-override phase (`BUNDLE_OVERRIDE_<QUALIFIED_NAME>` path and
  `oci://` value forms over the ref tag grammar; probe and lock-entry skip;
  path-form teeth — exists, parses, `edge` refusal, service collisions;
  producer-lock attachment three parents up, ordered before the consumer's
  lock env file, later `--env-file`s winning shared keys pinned against the
  version-pinned compose binary; `producer_env_entries` selects platforms
  from the producer lock's recorded platforms) — are landed locally, ahead
  of origin — unpushed. Work 1 is complete; the branch is ready for Work 2's
  operator handoff. `publish.yml` is wired (`packages.boatyard`,
  `major_minor: "0.6"`, `registry: pypi`).
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
- Content identity hashes the working tree, never git state: the hash reads
  the same source the build compiles (the `.dockerignore` allowlist, one
  shared matcher), so dirt is content and a lying tag is unrepresentable.
  "The tag of commit X" means "the tag of a clean checkout of X";
  clean-checkout reproducibility is the CI-side invariant — no workflow step
  writes into a hashed path before `build` / `generate-score --check`.
- Three-class principle: authored facts in the config, registry-resolved facts
  in the lock, tree-derived facts computed on demand and never committed.
- Fail closed: unknown name, missing lock entry, template composing a
  nonexistent tag, literal image reference — all refuse loudly, never
  silently.
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
1  build-out   the iteration unit on the refactor branch: disk-state service
               hashes, qualified BUNDLES= grammar, ref tag grammar + _BUNDLE
               lock entries, path-form bundle overrides
2  release     boatyard 0.6.0 — operator; one release, the pin target
3  toolkit     cutover onto boatyard spellings; the SHA the wave bumps to
4  sweep       all five consumers repin once, relock, rename, drop imports,
               adopt the iteration unit
```

Order within a unit is strict; unit order is fixed. 1 precedes 2 deliberately:
one release and one sweep carry the iteration unit — with no consumer on
old-basis tags there is no re-key wave at all, the world adopts disk-state
tags from day one, old git-basis `tree-` tags freeze in ghcr, and infra's
three `tree-` pins keep resolving as frozen digests. Nothing consumer-facing
ships separately. 3 runs parallel to 1 (only the sweep's `uses:` bumps order
on it). After the sweep, the next initiative is precision attribution
(`design/precision-attribution.md`), which precedes the semver lines by
construction; consumers adopt that work once, never per feature.

## Work 1 — Iteration unit (build-out)

Lands on the refactor branch; 0.6.0 publishes only after it. Order within:
disk-state hashing first (everything downstream builds on tags that track
disk state), then the selection cluster. The phases ship as one release
because they are one cluster in the selection machinery; an override shipped
before bundle pinning would be built against a digest-ref spelling the
grammar deletes, and the grammar flip breaks `.env` spellings — relock and
flip are atomic per repo, riding Work 4's wave.

### Disk-state service hashes

The old hash read git objects while the build compiled the disk; every
lying-tag vector (a dirty build under a stale tag, an untracked file entering
image bits the hash never saw) flowed from that mismatch, and the dirty-tree
guard this plan once carried was policing over a defect that disappears when
the sources unify. Dirt is content; nothing refuses; there is no flag and no
second channel.

The grammar is the allowlist and nothing else: comments, one `*`, `!path`
re-includes; every other line refuses at load, naming the line number and
the invariant. No pathspec (the dependency leaves pyproject), no buildkit
patternmatcher port, no new module, no class, no single-call helper — the
matcher is ~20 flat lines inline in `compute_service_shas`; a missing
`.dockerignore` fails as the plain `FileNotFoundError`. Restriction, not
porting, is the fix: pathspec/gitignore semantics and buildkit diverge on
precisely the lines this grammar refuses (a bare pattern is root-anchored
in buildkit, basename-any-level in gitignore), so refusing them keeps the
matcher trivial and its agreement with the real build context true by
construction.

```python
def compute_service_shas(repo_root: Path, service_builds: dict[str, ServiceBuild]) -> dict[str, str]:
    repo_root = repo_root.resolve()

    service_dirs = {str(PurePosixPath(service.dockerfile).parent) for service in service_builds.values()}

    if not service_dirs:
        # Skip the disk walk when no services exist.
        return {}

    # Derive the workload root as the single top-level directory holding every service.
    tops = {PurePosixPath(service_dir).parts[0] for service_dir in service_dirs}
    _require_single_top(tops, "services")
    workload_prefix = f"{next(iter(tops))}/"

    # Require that .dockerignore is an allowlist
    includes: list[str] = []
    rules = [line.strip() for line in (repo_root / ".dockerignore").read_text(encoding="utf-8").splitlines()]
    if sum(1 for rule in rules if rule == "*") != 1:
        raise ValueError(".dockerignore: must contain exactly one '*' line")
    for number, rule in enumerate(rules, start=1):
        if not rule or rule.startswith("#") or rule == "*":
            continue
        if not rule.startswith("!"):
            raise ValueError(
                f".dockerignore:{number}: only allowlists are supported ('*' + '!path' lines); got {rule!r}"
            )
        path = rule[1:].strip("/")
        if not path or any(character in path for character in "*?[]\\"):
            raise ValueError(
                f".dockerignore:{number}: re-include lines must be literal paths without wildcards; got {rule!r}"
            )
        includes.append(path)

    # Collect allowlisted files
    allowed = [
        path
        for path in (entry.relative_to(repo_root).as_posix() for entry in repo_root.rglob("*") if entry.is_file())
        if any(path == include or path.startswith(include + "/") for include in includes)
    ]

    # Partition into shared and per-service paths, dropping workload-root files owned by no service.
    shared_paths = [path for path in allowed if not path.startswith(workload_prefix)]
    per_service = {d: [p for p in allowed if p.startswith(d + "/")] for d in service_dirs}

    result: dict[str, str] = {}
    for service_dir in sorted(service_dirs):
        with TemporaryDirectory() as tmpdir:
            index_env = {"GIT_INDEX_FILE": str(Path(tmpdir) / "index")}
            # Conversion pinned off so blobs hash the raw disk bytes the build compiles.
            bash(
                "git -c core.autocrlf=false update-index --add --stdin",
                cwd=repo_root,
                env=index_env,
                stdin_text="".join(f"{path}\n" for path in shared_paths + per_service[service_dir]),
            )
            tree_hash = bash_output("git write-tree", cwd=repo_root, env=index_env).strip()

        result[service_sha_env(PurePosixPath(service_dir).name)] = f"tree-{tree_hash}"

    return result
```

- Pinned empirically against `docker buildx build --output type=local`
  (2026-11-03): `#` lines are not comments to buildkit (inert root-anchored
  patterns — nothing strips them, none may matter); directory symlinks never
  enter the context; nested `!deep/re-include/` works with no parent
  re-include; exec bits ride through. The golden test builds a tree in the
  real pattern shape and asserts the walk's file set (paths, symlinks, exec
  bits) equals the exported context — daemon-gated (skipif), green in CI.
- Deterministic serialization, and it stays git's — no hand-rolled hash anywhere:
  enumeration is a disk `rglob` walk under the grammar; the walked paths stage into a
  fresh per-service temp index (`git -c core.autocrlf=false update-index --add
  --stdin`; git derives modes and symlinks from disk) and `git write-tree`
  mints `tree-{OID}`, the same tag grammar as the old code. Conversion pinned
  off so the OID is a pure function of the raw disk bytes — the bytes the
  build compiles. Residual vector: a declared attribute filter (none of these
  repos declares one, and an LFS pointer is itself content-addressed, so
  hashing it stays identity-sound).
- Identity is content-state, not commit-state: a local dirty state mints a
  machine-local name that never leaves the daemon; clean checkouts anywhere
  reproduce the same tags from the same commit.
- No push-with-dirty refusal: pushing dirty-content tags is honest
  (content-named bits) and the PR channel already publishes uncertified
  ephemera; certification discipline lives in Work 6's stable channel, which
  builds from clean CI.
- CI-side invariant, audited 2026-11-03 and holding: no toolkit workflow
  step writes into a hashed path before `build` / `generate-score --check` —
  every pre-hash write lands outside the allowlist (the runner.temp toolkit
  clone, `~/.cache/uv`, `.venv` / `metadata.json` / the lock at the excluded
  repo root). A drifting workspace mints drifting tags and Work 6 would bump
  versions on noise.
- The OID basis is unchanged: a conforming clean checkout mints the same tags
  as the old code (same file set, same modes), so the flip itself re-keys
  nothing — re-keys happen only where content genuinely differs (a dirty
  edit, a newly visible untracked file) or where the grammar forced an
  allowlist edit (bare patterns refuse at load now).

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
- The loop this buys once Work 6 lands: producer release mints `{version}` on
  the certified bundle digest; the consumer's upgrade is a yaml tag bump
  (Renovate-native) plus a relock, with the digest movement visible as a
  one-line lock diff in review — no registry pages, no hand-copying, no drift.

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

The iteration loop this buys: edit in the producer → `build` +
`generate-score` (fresh disk-state `tree-` tags land in the local daemon; the
regenerated `stack/generated/**` diff rides the working tree and lands with
the work) → `uv run up` in the consumer. No commit until a content-addressed
artifact is wanted; `up`'s `--pull missing` serves local-only tree tags. The
expensive part of the old loop (push, CI, digest-copy) is what disappears.

Verify per phase: matcher battery above; qualified resolution incl.
same-named options in two groups and the refusal list; digest/`latest`/range
refusals; lock-key derivation including bare entries;
declaration-incremental copy-forward, tag-bump re-resolve, `--upgrade`;
collision refusal; missing-entry refusal naming the relock; bundle probe
classification; materialized root carries digest refs; both override value
forms; the inert override; probe and requirement skip; producer-lock
attachment and ordering; `edge` and collision teeth.

## Work 2 — Release 0.6.0 (operator)

Hand branch/SHA to the operator: push `refactor` (now carrying Work 1),
merge, tag through the normal release flow (the toolkit's binding owns it);
the PyPI publish rides the flow — the name is registered, no placeholder
race. One release, one consumer pin; nothing consumer-facing ships
separately. Confirm the GitHub repo rename happened (Settings → Rename; old
blob URLs redirect, consumers fix links in Work 4). Optional: a final
`docker-devkit` release whose description points at `boatyard` (precedent:
`stack-toolkit` ≤0.1.1 signposts) — skip if unwanted.

Verify: the release published; `AGENTS.md` matches the shipped surface.

## Work 3 — github-actions-toolkit cutover

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

## Work 4 — Consumer sweep (one wave)

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
   infra-rathole, lbe-toolkit theirs. The same relock adds `ALPINE_IMAGE`
   to `base_images` where missing — the compose target requires the lock
   entry from 0.6.0. placeframe declares its variant axis
   (`variants: {TORCH_DEVICE: [cuda, rocm]}`, cuda the default cell) in the
   same relock.
5. Every consumer's toolkit `uses:` SHAs bump to the Work-3 commit — one SHA
   per repo (Make-it-Sing starts a SHA behind and jumps from further back).
6. `.env` `BUNDLES=` flips to qualified spellings, atomic with the relock
   (the grammar flip breaks the old spellings): placeframe
   `core-cuda,auth-keycloak,tunnel-ngrok`; Make-it-Sing
   `placeframe-cuda,tunnel-rathole`; the rest theirs.
7. Bundle `ref:` entries flip digest → tag spellings (`{40-hex}` sha tags —
   published today), atomic with the relock: digest refs refuse at load under
   the new grammar. The relock writes the `_BUNDLE` lock entries; nothing is
   hand-authored.
8. Docs: `docker-devkit` → `boatyard` / `boatyard.yml` / `boatyard.lock.yml`;
   `uv run <verb>` → `uv run boatyard <verb>`; GitHub doc links to the renamed
   repo. `--lock-only` is a dead flag everywhere → `boatyard lock` (plus
   `--upgrade` where the old text said `--lock-only --upgrade`).
9. Consumer env reads go through a consumer-local module implementing
   compose's env-file grammar (`KEY=VALUE`, `#` comments, no interpolation —
   compose is the file's authoritative reader, so its rules are the spec; no
   dotenv dependency, no shared micro-package, filename hardcoded as the
   scripts already spell it).
10. `stack/secrets.yaml` is required from 0.6.0 — consumers lacking it add the
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
  gated by `boatyard generate-score --check` in CI); the `image: {lock: NAME}`
  markers in the workload files `keycloak.yaml` and `ngrok.yaml` flip to
  `image: ${NAME}` holes — the workload lens refuses the marker spelling, and
  generation resolves the hole from the lock, so digests still land inline in
  both renderings (provisioner and chart-values markers are unchanged); the
  stale `compute_service_shas` comments in `workloads/{reconstructor,localizer}`
  Dockerfiles and the `stack/AGENTS.md` "devkit's built-in gate-pin template"
  line (the gate pin moved into `generate_compose`) go in the docs pass. After
  the relock: one deliberate `stack/generated/**` regeneration cycle — the
  structured lock changes every marker-resolved image reference, and the
  disk-state basis re-keys every own-service tag — commit the bump, run
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
- **Make-it-Sing, lbe-toolkit** — standard renames and the step-6/7 flips;
  Make-it-Sing adds `BUNDLE_OVERRIDE_PLACEFRAME_CUDA` locally (never
  committed — the point of the iteration unit); lbe-toolkit's floating uv
  tag (`ghcr.io/astral-sh/uv:python3.13-…`) becomes an `{uv}`-templated value
  in the same relock: intent stays authored, components derive.
- **infra-rathole** — README carries `uv run build --lock-only` (→ `uv run
  boatyard lock`) and the tree-layout table naming both files; AGENTS exact-pin
  prose restates the release pin in the docs pass.
- The reserved front-door service name is `edge` from 0.6.0 — devkit-derived,
  consumers edit nothing; the renamed container at next bring-up is expected.

Verify: consumer CI green end-to-end; grep over the five repos returns zero
`boatyard` imports; the placeframe regeneration diff committed and reviewed
(disk-basis tags minted); capture-tool's lock carries arm64 platform
manifests; `up` probes pass against the mirror; every toolkit `uses:` ref
carries one SHA and every `--lock-only`/`--gpu` mention is gone from consumer
docs and workflows; Make-it-Sing's `up` assembles through the path override
against a sibling placeframe checkout.
