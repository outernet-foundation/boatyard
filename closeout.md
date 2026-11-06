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

## Snapshot (verified 2026-11-06)

- Work 1 landed: the `refactor` branch was squashed to a four-commit structure
  on `dev` (AGENTS.md prose; code; docs/design prose; closeout.md), pushed as
  `big-refactor`, merged as PR #23. One CI sharp edge surfaced and closed:
  typer force-enables its rich terminal whenever `GITHUB_ACTIONS` (or
  `FORCE_COLOR`/`PY_COLORS`) is set, so CLI-error panels render ANSI-colored
  and a styled token (`localizer/<variant>`) splits on an escape boundary —
  `tests/conftest.py` sets `_TYPER_FORCE_DISABLE_TERMINAL=1` (typer's own
  knob, honored before any typer import); `NO_COLOR` does not defeat it.
- Work 2 complete (2026-11-06): `boatyard 0.6.0` published on PyPI (wheel +
  sdist; the `0.6.0.dev170` prerelease rode the PR channel); the GitHub repo
  rename happened (`outernet-foundation/boatyard`, old docker-devkit URLs
  redirect — consumers still fix links in Work 4); the optional final
  docker-devkit signpost release was skipped; AGENTS.md matches the shipped
  surface. `publish.yml` stays wired (`packages.boatyard`, `major_minor:
  "0.6"`, `registry: pypi`).
- Work 3 landed (2026-11-06): toolkit `dev` advanced to
  `f2d576acbceeca8afb324152aad8e4928d5a769b` (operator-direct push of the
  `boatyard-cutover` branch, this repo's own landing mode; the pre-work tip
  was an ancestor, so no history rewrite). That SHA is Work 4's `uses:` pin.
  The landing carries the `BOATYARD_CONFIG_PATH` presence probe in
  `config.py`, the docker workflows onto `uv run --locked boatyard <verb>`
  spellings (`build --push` sans `--gpu`/`GPU:`, `mirror`, `publish-compose
  --plan`/`--branch`, `generate-score --check`, `docker-build-matrix`, and
  the lock-freshness gate as `boatyard lock --check`), the three test
  fixtures writing `boatyard.yml`, and the AGENTS.md config-file law line +
  verb spellings. Toolkit battery green at that SHA (pytest 199, ruff,
  basedpyright, lint-workflows). `build-unity.yml` carries no docker-devkit
  spellings (an earlier count said two; its verbs are unity-devkit's, outside
  the cutover); the remote `boatyard-cutover` branch still points at the same
  SHA and may be deleted.
- Work 4 opened (2026-11-06): the archaeology greps re-ran against the
  `compose-bundles` tips — every consumer's sweep base is its `compose-bundles`
  branch (in all five repos the conversion exists there; lbe-toolkit's and
  infra-rathole's `origin/dev` carry no `docker-devkit.yaml` at all, and
  Make-it-Sing's local checkout sits on a diverged feature branch — switch to
  its `compose-bundles` before editing). The pins and import inventory below
  re-verified true. Scope facts the steps don't carry: `merge=ours` exists only
  in capture-tool's `.gitattributes`; `stack/secrets.yaml` exists only in
  placeframe; capture-tool's config lacks `project_name` (0.6.0 requires it —
  its sweep adds it); placeframe's real `--gpu` doc line is
  `stack/AGENTS.md:116` (the rest is vendored dirtorch, not consumer prose);
  PyPI and ghcr are reachable from the sandbox, so relocks run here. Next: the
  per-repo sweeps; every consumer's toolkit `uses:` bumps to
  `f2d576acbceeca8afb324152aad8e4928d5a769b`.
- infra-rathole swept (2026-11-06, five commits on its `compose-bundles`:
  repin+rename+relock, the secrets contract, `Run generate-score`, the
  toolkit SHA bump incl. its local setup-release-devkit composite, the docs
  flip). Battery green locally (ruff, format, basedpyright, matrix `[]`,
  plan, `lock --check`, lint-workflows at the new pin); zero
  docker-devkit/`--lock-only`/`--gpu`/hole mentions remain. Landed: the
  branch was rebased to a clean three commits on `origin/dev` (bundle+cutover
  code pinning `0.6.1`; `Run generate-score`; docs), force-pushed, and the
  rathole-client bundle is published carrying the 0.6.1 rendering —
  Make-it-Sing's ref flip targets the fresh digest's sha tag.
- Both boatyard defects fixed on `dev` (2026-11-06, `abd6236` prose +
  `dde430a` code, full preflight green): `generate-score`'s `--target`
  default now derives from the config (explicit misspellings still refuse),
  and `ensure_tools` presence-probes with `shutil.which` plus installs only
  the tools the target needs. Verified end-to-end: the dev verb, bare
  invocation against swept infra-rathole, regenerates byte-identically.
   Released as 0.6.1; the sweep's remaining consumers pin it from the start.
   Sweep order set (2026-11-06): placeframe, placeframe-capture-tool,
   lbe-toolkit, then Make-it-Sing (its local checkout needs `git switch
   compose-bundles` before editing).
- Placeframe swept (2026-11-06, pushed; PR "Compose bundles" open on its
  `compose-bundles`): repin (`boatyard==0.6.3`, `requires >=0.6.3`, DEP002 for
  the CLI-only dep, pyyaml added for the values-file reads), renames + empty-lock
  relock (platforms amd64, variants `TORCH_DEVICE: [cuda, rocm]`, tunnel ports to
  the mapping form with int literals), the keycloak/ngrok marker→`${NAME}` hole
  flips, ngrok's target `door`→`edge`, preflight onto `boatyard build --targets
  postgres` + `tree-tags postgres` (image-ref and coupling checks die with the
  imports), api_auth/sweep_postprocess onto `yaml.safe_load` (typed via pydantic
  TypeAdapter), both score test modules rewritten to assert the committed
  artifacts (no boatyard imports, no skipif gates), toolkit `uses:` →
  `f2d576a…` (nine refs across four files), the docs flip (six AGENTS files +
  README + secrets/Dockerfile/score-file comments: boatyard names, `uv run
  boatyard <verb>`, values-file spellings, hole→slot, `edge`, no `up --dev` —
  the dev overlay is unbuilt trigger-gated design and the dev-toggles
  annotations ride inert), and one clean-basis regeneration. Before pushing, the
  branch was rebased onto `origin/dev`: the twelve-commit sweep collapsed to
  five (cutover, toolkit bump, docs flip, one `Run generate-score`, tests), the
  pre-sweep old-basis `301b8f09 Run generate-score` dropped (artifacts-only), a
  `sweep-backup` branch held the pre-rebase chain during the rework and may be
  deleted; final tree byte-identical to the pre-rebase tip; the session's
  "clean-clone `--check` passes" claim was false — no 0.6.3 checkout passes
  it (CI self-contaminates; see the 0.6.4 entry under Work 4). Verified
  locally: ruff,
  format, basedpyright (the only errors are pre-existing torch-resolution gaps —
  the venv lacks the cuda extra and the sync was deliberately skipped), scripts
  pytest 17, `lock --check`, matrix (three partitions incl. the TORCH_DEVICE
  fans), `publish-compose --plan` (four qualified bundles), lint-workflows at
  `f2d576a`. First Integrate run (42203772626, 2026-11-06): the docker-build
  leg FAILED at `generate-score --check`; root-caused next session (the
  0.6.4 entry under Work 4 — 0.6.3's walk hashed the check's own uv-sync
  byproducts; the committed tags were correct). The 0.6.4 repin went up as
  `a5430b3b` (a literal `fixup!` subject — folding it into `e2819c07` needs
  a force-push + CI rerun; left as-is deliberately); the regen correctly
  no-ops under 0.6.4. Second Integrate run (42212833180):
   plan-docker-builds green including the staleness check, build legs
   running on the committed tags; unity legs in flight at session close.
   The run completed: every leg green except `release-pr-builds`, which
   failed pulling `images-digests-all:sha-a5430b3b…` — the Work-3 digest
   shelving gap (entry under Work 4); fix in flight next session.
  NEXT SESSION OPENS HERE: the placeframe-capture-tool sweep on its
  `compose-bundles` — the repin adds `project_name` (required from 0.6.0),
  install-zed rewrites onto `yaml.safe_load` + `tree-tags` + `build
  --push-to`, and the arm64 platform-manifest relock gating the ZED-box AOA
  deploy (`zed-aoa-diagnosis-handoff.md` gates on exactly this); then
  lbe-toolkit, then Make-it-Sing (`git switch compose-bundles` first).
  Also unverified locally for placeframe (needs the cuda sync / CI): the
  full preflight sidecar end-to-end and generate-clients.
- The values-file session (2026-11-04, battery green) landed atop Work 1 and
  rewrote the consumer surface: `.env` is dead — `boatyard.values.yaml`
  (gitignored, typed; `ConfigValues` in `values_file.py`) is the only
  machine-values source, the ambient shell fills nothing (selection
  spellings included — `BUNDLES=x boatyard up` is gone), and compose's
  shell-beats-env-file precedence is defeated by an `env -u` scrub over
  values ∪ lock entries ∪ requirement names (end-to-end pinned against the
  compose binary). `up`/`down` materialize ONE merged per-run dotenv —
  values then lock entries under `#` source headers, a key in both
  refusing loudly (the lock is its only source); `lock_env_unwind` keeps
  the file with a path-line-only echo (contents may carry secrets); the
  open values map enforces ALL_CAPS env names.
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

Every consumer's Work-4 sweep builds on its `compose-bundles` branch — the
correct base in all five repos (the wave's commits land there and ride its
merge to `dev`); the toolkit on `dev`. Re-verify with grep at session open
before editing — the import inventory maps each site to a dissolution below.

## Standing constraints

These rulings bind every work unit below.

- No Python API (ADR D1): the contracts are exactly the verb CLI, the
  declaration schema (`boatyard.yml` + `boatyard.lock.yml`), and the committed
  artifact formats (`stack/generated/**`). Nothing lands that a consumer could
  only use by importing `boatyard`; every consumer touch is CLI, declarations,
  or artifacts.
- Content identity hashes git-visible state: the `.dockerignore` allowlist
  walk intersects `git ls-files --cached --others --exclude-standard`
  (system and global excludes blanked) at raw on-disk bytes — tracked and
  untracked-not-ignored files hash, ignored files never do. The tag is an
  input name reproducing from any tree of the same git-visible state; the
  registry digest in the lock is the content address. "The tag of commit X"
  means "the tag of any checkout of X" — reproducibility holds by
  construction (flipped 2026-11-06 in 0.6.4; the prior disk-walk basis
  hashed the checker's own uv-sync byproducts, recorded under Work 4).
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
  strings, or docs. The env-interpolation vocabulary is `slot` — `hole` is
  retired everywhere boatyard owns the word: the boatyard-local rename
  (`hole-to-slot.md` at the repo root) lands on this branch before Work 2,
  and consumer prose flips in Work 4's docs pass.
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
               hashes, qualified selection grammar, ref tag grammar + _BUNDLE
               lock entries, path-form bundle overrides
2  release     boatyard 0.6.0, then 0.6.1 (the mid-wave defect fixes) —
               operator; the wave pins 0.6.1, never bare 0.6.0
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
  grammar deletes, and the grammar flip breaks the old values spellings — relock and
flip are atomic per repo, riding Work 4's wave.

### Disk-state service hashes

(The hashing basis flipped to git-visible in 0.6.4 — see the Work 4 entry;
the allowlist grammar and git-index staging below stand unchanged.)

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

`bundles:` names options by qualified `group-dash-option`, uniformly, for every option
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
- `up` includes the lock's digest ref; the yaml tag is never used directly at
  bring-up. `up` never probes the registry — compose's include fetch and
  `--pull missing` surface missing digests, unclassified, at bring-up.
- The loop this buys once Work 6 lands: producer release mints `{version}` on
  the certified bundle digest; the consumer's upgrade is a yaml tag bump
  (Renovate-native) plus a relock, with the digest movement visible as a
  one-line lock diff in review — no registry pages, no hand-copying, no drift.

### Local bundle overrides (Stage 1)

`boatyard.values.yaml` may carry `path_overrides` per machine — never
committed, one map entry per override keyed by qualified name, the same qualified
identity as `bundles:` and the lock key. Value grammar: a filesystem path
(relative resolves against the consumer repo root, so sibling checkouts work
anywhere; absolute legal) or an `oci://` ref of any channel — the ref form
tests a colleague's landed-but-unreleased bundle without local builds and
without dirtying the tree. Unknown names refuse loudly listing the vocabulary;
a known-but-unselected name is inert — the override rides with selection.

An overridden option skips its lock-entry requirement — testing a bundle that may not be published at any
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
  and `GPU:` env translation — matrix `targets` now carry `--targets`
  addresses (bare `name` / `name/variant`), which flow through the
  `TARGETS:` env indirection unchanged, and the dashed target spelling
  (`name-variant`) is bake-internal and refuses on the CLI. The
  builds_ref naming stays `builds_ref` throughout.
- Tests: the three fixtures writing `docker-devkit.yaml` (test_prerelease,
  test_run_releases, test_config) → `boatyard.yml`.
- The docker digest-manifest shelving moves onto the toolkit: docker-devkit's build verb
  pushed one shard per leg to the builds shelf over bake's `metadata.json`; boatyard's
  build sheds the shelf and leaves `metadata.json` as the handoff, so `build-docker.yml`
  gains a toolkit shelving step after `boatyard build --push` (this item was lost from
  the list until placeframe's second Integrate run failed pulling
  `images-digests-all:sha-…`; restored 2026-11-06).
- AGENTS.md: the config-file law line and the verb invocation spellings.

Verify: the toolkit's own suite green.

## Work 4 — Consumer sweep (one wave)

Open by re-running the archaeology greps to re-derive the edit list against
wherever the release actually landed. Upstream defects the sweep surfaces in
github-actions-toolkit or boatyard itself are fixable mid-wave — surface, fix
the upstream, record here; the wave's pin target follows whatever release
carries the fixes. Found in infra-rathole (both boatyard 0.6.0, both blocked
the CI-green verify), fixed and released as `0.6.1`:
- `generate-score` defaults `--target both` and a one-substrate repo refuses
  under that default — the toolkit getter hardcodes the bare
  `generate-score --check`, so every compose-only repo (infra-rathole,
  lbe-toolkit, Make-it-Sing) fails the gate. Correct fix is boatyard-side:
  the default derives from the config (k8s present + local entries → both;
  one substrate → that one), the explicit misspelling refusals stay, the
  toolkit stays shape-blind.
- `ensure_tools` probes with `bash_check`, which raises FileNotFoundError on
  a missing binary — the install path is unreachable on a fresh machine
  (runner or sandbox); `generate-score` 0.6.0 has never run clean off a fresh
  environment. Rider: compose-only runs still install score-k8s + helm.

Found in the placeframe sweep (2026-11-06), fixed on `dev` (full preflight
green), released as `0.6.2`:
- `PortOrSlot` was string-typed, so a natural YAML int port
  (`published: 4040`) refused with a coercion error and only the quoted
  digit-string loaded. Literal ports are now int-typed (strict; digit strings
  refuse naming the env-slot alternative), env slots stay the string branch,
  and the edge emission drops its `str()` coercion. infra-rathole declares no
  ports; unswept configs flip in their own sweeps.

Found in the placeframe sweep (2026-11-06), fixed on `dev` (full preflight
green), released as `0.6.3` together with the port fix; the wave's pin
target and `requires:` floor are `0.6.3` from here (infra-rathole's landed
`==0.6.1` pin and `>=0.6.1` floor bump in passing when next touched):
- `render_score` expanded workload `${SHA}` slots in-process but never
  exported the values to the score tool subprocess, so provisioner templates
  reading `{{ env "AUTH_INITIALIZER_SHA" }}` (placeframe carries fifteen
  sites across both substrates, plus `{{ env "SCORE_STORAGE_CLASS" }}` under
  `--local`) rendered empty — the oidc-issuer services template produced
  `image: …auth-initializer:` and refused to decode. The variable set
  (images ∪ service shas ∪ rendering vars) now rides the init/generate
  environment; the old devkit's os.environ export was the channel 0.4.9
  consumers depended on.

Found in the placeframe sweep's failed Integrate run (42203772626,
2026-11-06 — `plan-docker-builds`' `generate-score --check` exited 1),
fixed on `dev`, released as `0.6.4`; the wave's pin target and
`requires:` floor are `0.6.4` from here:
- The 0.6.3 disk walk hashed gitignored files under the allowlist — the
  check's own `uv run --locked` sync writes them
  (`packages/generated/python/*/…egg-info/`, `build/`) before the walk
  runs, so CI's checkout contaminated itself and minted tags (`c51eb…`
  set) no committed mint could match; the committed placeframe tags
  (`f2bb…` set) were correct all along (first diagnosed as a dirty-tree
  mint — wrong; the mint was clean). The walk now intersects the
  allowlist with git-visible files at raw on-disk bytes: tracked and
  untracked-not-ignored files hash, ignored files never do, and
  `generate-score` mints identically from any tree — the clean-clone
  mint discipline dies. Accepted residual, ruled deliberately: a local
  build with ignored dirt under the allowlist shares its tag with the
  clean image (ignored bytes bake unnamed); no push guard (YAGNI).
  Verified by the second Integrate run (42212833180) — the staleness
  check green on the committed tags.

Found in the placeframe sweep's second Integrate run (42212833180,
2026-11-06 — `release-pr-builds` died pulling
`images-digests-all:sha-a5430b3b…`, not found; every other job green):
the Work-3 toolkit cutover dropped the docker digest-manifest shelving.
docker-devkit's build verb had pushed one shard per leg
(`sha-{full}-{sha256(sorted targets)[:8]}` over bake's `metadata.json`,
boatyard history `9ab7c09`/`79a7087`); boatyard deliberately shed the
shelf, and the ruling that the toolkit picks the push up was lost with an
absorbed plan — both AGENTS files already described the intended state
(boatyard: `metadata.json` is "the handoff the toolkit's builds_ref step
reads"; toolkit: "the docker build legs push digest-manifest shards
there"). Fix: a toolkit `shelve-digests` verb reading the handoff, added
to `build-docker.yml` after `boatyard build --push` (the restored Work-3
item); the wave's consumers bump their toolkit `uses:` pins to whatever
toolkit SHA carries it.

Sweep mechanics discovered in infra-rathole, carried to the rest: the
score-tool trio (score-compose 0.42.0, score-k8s 0.15.0, helm 3.22.1) must
pre-exist in `/usr/local/bin` to run generate-score locally (the defect
above); local regeneration on compose-only repos spells `--target compose`;
the 0.6.0 rendering rewrites every bundle artifact (`{entry}-` namespaced
services and configs keys on a private `{entry}-net` with the bare name as
alias, `{ENTRY}__LOCK__KEY` image slots, `name:` last) — the diff is
expected, rides each repo's `Run generate-score` commit, and consumer AGENTS
prose claiming the workload-container name "collapses back" is stale
everywhere it appears; infra-rathole carries a third toolkit-SHA home (its
local `setup-release-devkit` composite, `RELEASE_DEVKIT_COMMIT`) — the only
repo with one. The Make-it-Sing phase must watch the rathole bundle's
default local addrs (`gateway:8443`, `livekit:6178`): they now sit on the
bundle's private network, where consumer service names may not resolve —
its `up` assembly (the path-override verify) surfaces whether the
  `TUNNEL_*_ADDR` slots need explicit values. Placeframe added: its checkout
  carries ignored dirt under the `.dockerignore` allowlist (`__pycache__`,
  `egg-info/`, the vendored deep-image-retrieval `.venv`) — inert for
  hashing since the 0.6.4 git-visible flip (regen mints from any tree);
  placeframe's `stack/AGENTS.md` clean-clone regen line rides its next
  touch.

Common to all five, order within a repo code → lockfiles → prose:

1. Dependency: `docker-devkit…` → `boatyard==0.6.4` (or `>=`); `uv lock
   --upgrade-package boatyard`. `requires:` floor → `>=0.6.4` (0.6.4
   carries the git-visible hashing flip; placeframe landed at 0.6.4 via its
   repin; infra-rathole's older floor bumps in passing when next touched).
2. `git mv docker-devkit.yaml boatyard.yml`; `git mv docker-devkit.lock
   boatyard.lock.yml`, then truncate the lock to the empty file before the
   relock — the old env format never parses at relock's committed
   schema-only read, and the empty lock bootstraps the structured YAML from
   nothing (digests are content-stable: infra-rathole's RATHOLE index digest
   carried forward identically; no per-entry migration exists).
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
6. `.env` becomes `boatyard.values.yaml` (typed, gitignored) in the same wave:
   `BUNDLES=` becomes the `bundles:` list, `BUNDLE_OVERRIDE_*` the
   `path_overrides:` map keyed by qualified name, `PLATFORM=`/`PUBLIC_URL`
   the `platform:`/`public_url:` spellings, hole values the open map —
   atomic with the relock and the qualified-selection flip (the grammar
   flip breaks the old spellings): placeframe
   `bundles: [core-cuda, auth-keycloak, tunnel-ngrok]`; Make-it-Sing
   `bundles: [placeframe-cuda, tunnel-rathole]`; the rest theirs. A repo
   carrying `.env.sample` ships a committed `boatyard.values.yaml.sample`
   beside it (the values file itself stays gitignored). Conversion drops
   any lock-key spellings from the old `.env` — the merged-file collision
   refusal blocks them (the lock is their only source) — and only ALL_CAPS
   hole keys ride. Any CI step that materialized `.env` (a touch, an echo,
   a `PLATFORM=` export) writes the values file instead; the ambient shell
   fills nothing.
7. Bundle `ref:` entries flip digest → tag spellings (`{40-hex}` sha tags —
   published today), atomic with the relock: digest refs refuse at load under
   the new grammar. The relock writes the `_BUNDLE` lock entries; nothing is
   hand-authored.
8. Docs: `docker-devkit` → `boatyard` / `boatyard.yml` / `boatyard.lock.yml`;
   `uv run <verb>` → `uv run boatyard <verb>`; GitHub doc links to the renamed
   repo. `--lock-only` is a dead flag everywhere → `boatyard lock` (plus
   `--upgrade` where the old text said `--lock-only --upgrade`). Shell-spelled
   examples (`BUNDLES=x boatyard up`, `PLATFORM=x …`) flip to editing
    `boatyard.values.yaml`; runbook text describing the failure echo of the
    per-run env file updates to the kept-path line (contents never print —
    secrets may ride); `.env.sample` references point at the new sample file.
    The vocabulary flip rides this pass: `hole`→`slot` in consumer prose
    describing env interpolation (placeframe's `stack/AGENTS.md` is the
    densest; grep each repo — audit-time counts, whole-word noise included:
    infra-rathole 84, Make-it-Sing 59, placeframe-capture-tool 26,
    lbe-toolkit 23).
9. Consumer env reads go through `yaml.safe_load` of
    `boatyard.values.yaml` — the file is plain YAML, so no consumer-local
    dotenv grammar module exists (the earlier plan for one dissolved with
    the typed values file).
10. `stack/secrets.yaml` is required from 0.6.0 where generate-score runs —
    and it is the env contract, not only secrets: every ALL_CAPS slot a
    rendering consumes must be declared (mint: true only for real secrets;
    values-file knobs declare plain). An empty file passes only a repo whose
    renderings consume nothing; a self-skipping repo (placeframe-capture-tool
    — the self-skip fires before the secrets read) needs no file at all.
    infra-rathole's declares its six include-time slots, none minting.

Per-repo:

- **placeframe** — `scripts/preflight.py` drops its four library imports and
  `VERSION_COUPLINGS` (the coupling CI step dies with it, and so does any
  `unpinned_references` step — the literal-image teeth moved into boatyard);
  the postgres-wrapper leg rewrites onto the CLI (`boatyard build --targets
  postgres`; `boatyard tree-tags postgres` for the `POSTGRES_SHA` the
  `docker run` invocation spells); `api_auth.py`/`sweep_postprocess.py` read
  `boatyard.values.yaml` through `yaml.safe_load`, and sweep_postprocess's lock read
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
  root exact pin and the `scripts/` member floor); the config gains
  `project_name` (required from 0.6.0 — the build-only repo never carried
  one).
- **Make-it-Sing, lbe-toolkit** — standard renames and the step-6/7 flips;
  Make-it-Sing's local (never committed) `BUNDLE_OVERRIDE_PLACEFRAME_CUDA`
  becomes the `path_overrides: {placeframe-cuda: …}` map entry; lbe-toolkit's
  floating uv
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
manifests; every toolkit `uses:` ref
carries one SHA and every `--lock-only`/`--gpu` mention is gone from consumer
docs and workflows; Make-it-Sing's `up` assembles through the path override
against a sibling placeframe checkout.
