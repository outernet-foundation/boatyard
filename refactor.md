# refactor.md — the 0.7 line

Plan file for the slot/pins/values refactor. It holds the line's future only: unlanded move
specs, closed rulings, process law, the consumer transition. Present truth lives in the code
and AGENTS.md; history lives in git log; at line close, closeout.md takes the record and this
file is deleted.

## Process law

- Branch shape: over `origin/main`, exactly one commit changing AGENTS.md and exactly one tip
  commit changing closeout.md + this file together, in that order. Doc changes never join a
  code commit; AGENTS.md lines a move invalidates, and this file's updates, fold into those
  two commits by rewriting them in place. Code commits beneath stay doc-free, so the line
  reviews as pure code capped by the doc record.
- Execution: one move per session, landed as one doc-free code commit, then the doc fold,
  then the handoff; the next session takes the next move.

## Ledger

Landed moves, one line each — their specs are deleted, git log holds the detail:

- Move 1 (2026-11-07) — tree slots in committed compose artifacts: the sentinel barrier and
  `__BOATYARD_TREE__` marker family in `config/config.py`, tree fans in `up`'s env file,
  images-only build (the regeneration epilogue died).
- Move 2 (2026-11-07) — `boatyard.k8s-pins.yml` at root + `config/k8s_pins.py`; the `bump`
  verb is the sole writer, certifying HEAD's tree tags against the CI shelf shards (D2), then
  regenerating the k8s output from the file it just wrote.
- Move 3 (2026-11-07) — the `secrets:` section folds into `boatyard.yml`; `stack/secrets.yaml`
  and `SECRETS_FILE` died.
- Move 4 (2026-11-07) — values sections + load-time validation over the demand table
  (`ValuesFile.load`); the consumer-surface scan (`authored_slot_demands`) moved to config
  load; lock bundle `slots` records; the compose `validate_consumed` tail died;
  `tests/test_values.py`.
- Move 5 (2026-11-07) — values `${NAME}` interpolation: roots = flattened keys ∪ minted
  slots, grammar/marker/unresolvable/cycle checks at load, resolution once at the env-file
  render (`assembly()` answers `resolved`); `generate-score` still resolves nothing.

Next: Move 6, then Moves 7–9 in order. Move 9's name is undecided — decide before it executes.

## Closed rulings

Recorded so they are not re-litigated; reopening one is the operator's call, not a session's.

- D1 — the operator always cuts the k8s pin bump commit: post-merge, on a clean checkout of
  `dev` HEAD, then push. Merge-gate absorption stays ruled out — the unity library cache
  promotion needs the merge tree to equal the PR head tree; the gate must never modify the
  tree.
- D2 — a pin is writable only for content CI built and pushed at exactly that head: enumerate
  the shelf's `sha-{HEAD}*` shards (the build-artifact-registry's `list_build_tags`), merge
  the `{target: {ref, digest, tags}}` manifests, and every computed tree tag must appear in
  the union's `tags[]`. The enumerate-and-merge shape stays inline in boatyard until a third
  consumer appears. Ad-hoc `--push-to` builds are unpinnable by design.
- No pin bootstrap: first k8s generation without a pins file fails naming the bump verb,
  always. The lock bootstraps resolutions (recomputable, checkable); a bootstrap pin would be
  an unverified decision — records cannot bootstrap.
- The bump verb writes the whole certified head's tag set — no per-service partial bumps; with
  content-addressed tags, unchanged services are no-ops by identity.
- No filters, functions, or operators in the interpolation grammar, ever — bare references
  only. `NGROK_DOMAIN` stays hand-set: host-of-URL is legitimate divergence, a decision, not
  a derivation. Same for `GATEWAY_PORT` vs `public_url` — they correlate only under direct
  exposure.
- Reserved names (`PUBLIC_URL`, `EDGE_BASE_URL`) refuse hand-setting always and are minted
  when their declaration exists — no conditionals.
- `--local` k8s generation reads no pins: fresh working-tree tags, dirt is content.
- Unused values keys stay legal (unselected bundles still declare them); old port spellings go
  silently inert after Move 7.
- Interpolation roots are hand variables + minted slots only: marker-bearing names refuse both
  as keys and as references; typed fields participate only via their minted slots.
- Routes get no override surface, ever — per-machine upstream wants are dev-overlay concerns,
  not values concerns.
- The barrier and edge images are boatyard-owned, not base images — Move 8 enacts this; the
  `base_images` declaration of them was the category error behind both placement checks and
  the build-arg pollution.

## Line laws

The invariants this line establishes. At closeout each graduates into AGENTS.md (growth there
is the operator's call) or dies with the line.

1. Three-class principle with one carve-out: the pins file is a deploy record — a cache can be
   stale, a record can only be superseded (authored form already in AGENTS.md).
2. Slot boundary: env slots exist only for values committed artifacts consume; anything only
   the assembly consumes (edge stanza, port stanzas) is a typed config field with typed
   overrides.
3. `boatyard.yml` contains zero `${...}` spellings; interpolation lives exactly where it
   happens — score sources and committed artifacts. Move 7 adds the load-time teeth.
4. Decide-or-copy test: every values literal is a hand decision, a machine-minted slot, or an
   interpolation of the two — everything else is eliminated.
5. Values override keys mirror the config fields they override (`gateway:`,
   `ports.<service>`): unknown names fail listing the vocabulary; unselected names sit inert.
6. One file, one nature: no file mixes recomputable bytes with decision bytes.
7. Four files at root — `boatyard.yml`, `boatyard.lock.yml`, `boatyard.values.yaml`,
   `boatyard.k8s-pins.yml`; any other location is a raise. The producer-root hunt is
   `parents[3]` plus a raise.
8. Validate at the earliest moment the evidence exists; derive at the moment the product is
   needed (section checks at values load; cross-repo fan derivation only when `up` needs the
   env file).

## Moves

### Move 6 — machine-minted `EDGE_BASE_URL`

- A declared gateway mints `EDGE_BASE_URL=http://edge:8443` (reserved service name + the
  Caddyfile listen constant — boatyard-minted facts typed by zero humans) into the per-run env
  file; it is an interpolation root and unconditionally reserved against hand-setting. The
  `keycloak:8080` incident class — hand-copied boatyard-minted facts rotting in gitignored
  files — becomes unwritable.

### Move 7 — ports: config defaults, values deviations

- `gateway:` and `ports[].published` in `boatyard.yml` become plain integer literals
  (repo-owned defaults, versioned, reviewed); a load-time check fails any `${` spelling
  anywhere in the config (law 3's teeth).
- The values file gains typed overrides `gateway:` (scalar) and `ports:` (service →
  published) under law 5's vocabulary validation. Boatyard resolves default ⊕ override into
  integers in the materialized stanzas; ports leave the env file entirely.
- The `FORWARDED_PROTO`/`FORWARDED_PORT` edge-stanza derivation from `public_url` under an
  auth selection is unchanged — assembly-baked, not slots.

### Move 8 — boatyard-owned image pins

- `base_images` returns to meaning exactly Dockerfile build inputs. The barrier and the edge
  are boatyard-emitted services — no Dockerfile consumes them — so their image pins are
  boatyard-owned facts.
- New config fields `barrier_image:` / `edge_image:` in the tool-pin shape: plain `path:tag`
  literals, defaults shipped by the boatyard release, repo-overridable, no values surface
  (images are not machine-specific). The version coupling is the point — the rendered
  Caddyfile syntax moves with the Caddy major, the barrier's behavior with its image — both
  bump under the `major_minor` discipline, and relock surfaces the effective change.
- Relock resolves both into the lock as today (manifests needed at `up` and in committed
  artifacts); mirror treatment unchanged; the slot spellings (`${ALPINE_IMAGE:?err}`, the
  CADDY manifest ref) rename at Move 9; the consumer-surface scan's lock-key subtraction gains
  both keys as they leave `base_images`.
- Both placement checks die with nothing left to forget: the gateway/CADDY load check and the
  ALPINE refusal at `generate_compose` in `score_compose.py`. `ALPINE_IMAGE`/`CADDY_IMAGE`
  become reserved names in `base_images` — the hand can't declare what boatyard owns.
- Consumer churn: drop both entries from `base_images`; ships with the 0.7 churn event and
  the `requires` floor bump.

### Move 9 — tree vocabulary rename [LAST; name undecided]

- One stroke, nothing forks mid-line: the marker (`__BOATYARD_TREE__` → `__BOATYARD_{NAME}__`),
  the `tree-<oid>` tag spelling, the `tree-tags` verb, `TREE_SLOT_MARKER`/`env_tree_slot`,
  `compute_service_shas`, and the docs' "tree-derived"/"tree tag"/"tree fan" vocabulary. The
  sentinel follows the marker for free (derived spelling).
- Name bar: a cold reader of `{ENTRY}__BOATYARD_{NAME}__{SERVICE}_SHA` knows what it names
  without git-internals knowledge (`SOURCE` was rejected). Decide before this move executes.
- Ships with the churn event; bump-as-sole-writer makes the consumer re-pin one bump run.

## Transition

- Moves land as separate PRs inside the line with one consumer-churn event at the end; the
  `requires` floor bump ships with the release. After Move 9, closeout.md takes the
  consumer-repo record; the push is the operator's.
- Consumer order: bump first (creates the pins file at the current head; the k8s manifest
  stays byte-stable if its baked tags were fresh, and heals if stale) → `generate-score`
  (compose artifacts slot-ify; the secrets section migrates) → per-machine values reshuffle
  (sections; drop `PUBLIC_URL`/port spellings; AUTH block → interpolation lines) → drop
  `ALPINE_IMAGE`/`CADDY_IMAGE` from `base_images` → sample rewrite, `stack/secrets.yaml`
  deletion, committed-artifact test updates (compose assertions gain slot spellings; k8s
  unchanged).
- Toolkit: the getter's `generate-score --check` step command is unchanged; its rationale line
  rewrites (structural freshness, not tag freshness); bump-step integration depends on D1;
  toolkit AGENTS.md line ~60 update.
- Placeframe-side, not boatyard work: sample + live `AUTH_JWKS_URL` fix (literal edge spelling
  pre-refactor, `${EDGE_BASE_URL}` post); API JWKS probe as /health-reflects-JWKS (backoff
  beats restart storms; wait-for-resources may need to gate api on keycloak under the auth
  option); `loki_query.py` hardcoded container name; the Loki label-gap investigation
  (possible OTLP plumbing dropped in the bundles cutover — investigate early).
- Design docs: `precision-attribution.md` and `semver-image-bundle-lines.md` "Release and
  regeneration" sections assume committed tree tags — amend to the slots/pins/bump model.
- Tests still owed: `up` fills minted `EDGE_BASE_URL` (Move 6); ports override vocabulary
  failures (Move 7); reserved `base_images` names (Move 8); rename propagation across marker,
  verb, and docs (Move 9).

## What still dies

`${...}` spellings in `boatyard.yml` and `PUBLIC_URL`/port spellings in values files
(Move 7); the gateway/CADDY load check, the ALPINE refusal at `generate_compose`, the
`ALPINE_IMAGE`/`CADDY_IMAGE` `base_images` entries and their build-arg injection (Move 8);
the current tree vocabulary (Move 9). Everything else the line set out to kill already died
with Moves 1–5.

## This file

This file exists to be deleted: it holds the 0.7 line's future only. Present truth lives in
the code and AGENTS.md, history in git log, and the closeout record in closeout.md.

- A landed move's spec dies in the same session's doc fold — one ledger line survives, and a
  law that must outlive the line graduates into the AGENTS.md fold. Landing a move without
  shrinking this file is an incomplete landing.
- No diaries: no session narrative, no git-state snapshots (commit counts, branch ordering) —
  they rot on the next commit; the diff is the record.
- Sections are fixed, in order: identity, Process law, Ledger, Closed rulings, Line laws,
  Moves (unlanded only), Transition, What still dies, this section.
- Rulings and laws are closed: a session reopens one only by the operator's call. A new ruling
  or an amended move replaces text here; it never appends history beside it.
