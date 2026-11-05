# boatyard.values.yaml — the typed machine-values file

Bridge design for the next session. Implement from this file; when the work
lands, delete it and fold the hardened laws into AGENTS.md (same lifecycle as
closeout.md's units). Everything here is decided unless listed under "Open
design questions" — do not relitigate closed rulings without their flip
conditions.

## Origin

The refactor session moved boatyard's selection law into `Environment.load`
(name tier + the one-per-group fold, producing `choices`), deleted the
empty-selection and `--build` refusals, and inlined the single-call helpers.
The move that was refused was the unfilled-requirements check: its `filled`
set counted three sources (`os.environ`, `.env` variables, the lock-derived
env entries), so a shell export could flip its verdict — not a predicate of
the (environment, config) pair. The operator ruling that followed: **`.env`
is the only valid way to fill environment holes**, and this file records the
design that makes that ruling structurally true — then goes one step further
and replaces `.env` itself with a typed YAML object.

## Closed rulings

- Interpolation sources are exactly: the machine-values file (consumer
  holes) and the per-run lock env file (lock-key holes). The ambient shell
  fills nothing.
- D1 resolved yes: selection spellings (`BUNDLES`, `PLATFORM`, `PUBLIC_URL`)
  lose the shell with everything else — the values file is their only source;
  `BUNDLES=x boatyard up` is gone.
- D2 resolved: the scrub never refuses — shell-set hole names are stripped
  and the run proceeds. The unfilled-requirements refusal alone mentions
  them, gaining a clause ("X is set in the shell and ignored") when the
  unfilled hole carries a shell value.
- D3 resolved: the name is `boatyard.values.yaml` — the conceptual
  collision with helm `values/` files is accepted; distinct paths and the
  `boatyard.` prefix carry the distinction.
- D4 resolved: the unfilled-requirements check stays inline in `up` —
  `entries` and `requirements` are the include loop's work product, and the
  original purity concern is fixed at the input level (filled = values ∪
  lock entries, no shell), not by relocating the code.
- D5 resolved: no unknown-key refusal on the open map — `:-`-defaulted
  holes are excluded from `requires` yet legitimately overridable, and the
  per-machine file keeps values for bundles not currently selected; typos
  surface through `up`'s unfilled-requirements refusal. The ALL_CAPS key
  grammar governs the open map only — typed fields (`bundles`,
  `path_overrides`, `public_url`, `platform`) are never env spellings and
  carry no case law.
- D6 resolved: a values-file key colliding with a lock entry key is a
  mistake and refuses loudly at materialization (listing the keys, naming
  the lock as their only source) — amending the materialization ruling,
  which had specified silent lock-wins in tension with the lock-filled
  ruling. With collisions impossible, the merged file carries no
  duplicates (append-vs-de-dupe is moot; no within-file last-wins
  verification needed); section comment headers label each source's
  provenance inside the file.
- D7 resolved: no secrets shape-lint — real tokens defeat any heuristic,
  and a garbage value surfaces downstream as the service's own auth error.
  The failure echo is gone entirely: `lock_env_unwind` keeps the merged
  file and prints only the kept-path line. Every entry the echo carried is
  reconstructable (lock entries from the committed lock, values-file key
  names from its recorded `requires`), the unfilled case has its own
  refusal, and the merged file may carry secrets — the residual case for
  the echo was debugging boatyard's own fan machinery, which does not
  justify a secrets-in-logs surface.
- Compose's documented precedence — shell environment beats `--env-file`,
  and no switch exists to disable it — is defeated by scrubbing the compose
  child environment (`env -u`), not by trusting compose. Verified
  2026-11-04 against the pinned binary (v5.6.1): `FOO=from_shell` +
  `--env-file` saying `from_dotenv` interpolates `from_shell`; with
  `env -u FOO` it interpolates `from_dotenv`. Docs confirm precedence as
  intended behavior; maintainer issues (#9045, #9442, #12180, #12901) show
  no escape hatch has ever been added. `COMPOSE_ENV_FILES` and
  `COMPOSE_DISABLE_ENV_FILE` do not touch precedence.
- The values file replaces `.env`: typed object, gitignored, YAML. The
  `Environment` model built this session IS its schema — `bundles` (list),
  `path_overrides` (map), `public_url` (`PublicUrl`), `platform`, plus an
  open map for hole values. The tier split (name tier + fold ride
  `Environment.load` on every verb; consumption in `up`) is unchanged.
- The materialized per-run dotenv is written by boatyard: consumer values
  + lock entries in one file, section comment headers labeling each source.
  A key carried by both sources refuses loudly at materialization — the
  values file never overrides the lock (lock-key holes are lock-filled by
  construction; filling one by hand is the forbidden hand-authored lock
  line).
- Lock-key holes (`LOKI_IMAGE`-style) stay lock-filled; a digest in the
  values file is the hand-authored lock line the design forbids.
- Published bundles stay native compose OCI artifacts — anyone with
  compose ≥ 2.40 can `include: oci://…` them with their own env file.
  Boatyard is governance/ergonomics, never a runtime dependency. Plain
  compose consumers keep their own `.env`; that surface is theirs.
- Empty-selection and `--build` refusals stay deleted (compose's
  `no service selected`, exit 1, verified against the pinned binary;
  `run_build`'s zero-derived-services refusal). The unfilled-requirements
  check keeps unique teeth: bare `${NAME}` holes (secrets consumption)
  interpolate to empty with only a warning in compose — only `:?err`
  spellings hard-fail — so it must survive somewhere.

## The change set

1. **The file.** `boatyard.values.yaml` at the consumer root, gitignored
   (consumer's `.gitignore`; same as `.env` today). Consumed spellings are
   typed and closed (`extra="forbid"`); hole values ride an open map.
   `BUNDLES` becomes a YAML list; `BUNDLE_OVERRIDE_*` becomes a map keyed by
   qualified name — the underscore/dash/upper-case derivation law dies.
2. **`Environment.load(root, config)`** reads the values file via the
   strict YAML loader (`load_yaml_file` family in `config.py`) — no dotenv
   parsing, no shell merge, `from os import environ` deleted. A missing
   file reads as the empty declaration (preserves fresh-checkout score
   generation). The name tier, fold, `choices` are untouched.
3. **Materialization.** `up`/`down` write ONE per-run dotenv:
   consumer values + lock entries under section comment headers; a key in
   both sources refuses loudly. The two-file
   `--env-file .env --env-file <per-run>` shape collapses to one argument;
   `lock_env_unwind` keeps the merged file on failure, printing only its
   path (no content echo — the file may carry secrets).
4. **The scrub.** `compose_command` gains `hole_names`; when non-empty it
   returns `env -u K1 -u K2 … docker compose …` (sorted; single command, no
   shell operators). `up` passes values ∪ lock entries ∪ requirement names;
   `down` passes values ∪ lock entries.
5. **The unfilled-requirements check** (`up`, inlined): `filled` =
   values ∪ lock entries only; message drops "shell" → "(boatyard.values.yaml
   or the lock env file)".
6. **Gates.** `require_env_file` becomes the values-file gate, same
   semantics (missing file refuses for `up`/`down`, reads empty for score).
7. **AGENTS.md.** Module map (`environment.py` line), the `.env` law → the
   values-file law, the selection-law's "(shell, …" clause, the
   `compose_command` law gains the scrub sentence, per-run-files law. Byte
   budget is already breached (28,881/26,624 at session close) — this prose
   commit must trim an equivalent elsewhere.
8. **Tests to flip** (inventory verified this session): test_compose.py:485
   (shell-overrides-BUNDLES pin — replace with its inverse), score_generate
   :327 (`setenv("PLATFORM", …)` → write the values file), assembly :277
   (`environment.environ` patch target dies), message pins at :685/:717.
   New: scrub-prefix assertions for both verbs' captured commands
   (including prefixed lock fans); end-to-end pinned-binary proof (shell
   value set, interpolation output comes from the values file).
9. **closeout.md Work 4 amendments.** Step 9 (consumer-local dotenv grammar
   modules) dissolves — consumers `yaml.safe_load` the values file. The
   `.env` → values rename+reformat rides the existing atomic cutover
   (relock + `BUNDLES=` flip). CI passing `PLATFORM=` via env must write
   the file instead. Make-it-Sing's `BUNDLE_OVERRIDE_*` locals reformat.

## Session context

Branch `refactor`, battery green at close (ruff clean; 490 passed, 2
skipped — daemon-gated). Session commits: e2114a1/f80f4d1 (name tier into
load), 4530da1/86459b4 (empty-selection + `--build` deletions), a8a3a87
(refusal inline), 2667392/b57b894 (fold + `choices` into load), e987864
(requirements-check inline). The scrub change set and the values-file
radicalization were presented and agreed but NOT implemented — this file is
the implementation contract.
