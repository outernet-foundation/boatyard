# refactor.md — the 0.7 line

Plan file for the slot/pins/values refactor. It holds the line's future only: unlanded move
specs, closed rulings, process law, the consumer transition. Present truth lives in the code
and AGENTS.md; history lives in git log; at line close, closeout.md takes the record and this
file is deleted.

## Process law

- Execution: one move per session, landed as one doc-free code commit, then the doc fold as its
  own commit, then the handoff; the next session takes the next move. Doc changes never join a
  code commit. History is never rewritten after the fact — no squashes, no reordering.

Moves 1–10 landed; their specs are deleted and git log holds the detail (one commit per move).
What remains: Move 9, the tree vocabulary rename — it lands last. Its name is still undecided;
decide before it executes.

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
- Unused values keys stay legal (unselected bundles still declare them); old port spellings
  sit silently inert.
- Interpolation roots are hand variables + minted slots only: marker-bearing names refuse both
  as keys and as references; typed fields participate only via their minted slots.
- Routes get no override surface, ever — per-machine upstream wants are dev-overlay concerns,
  not values concerns.
- The barrier and edge images are boatyard-owned, not base images — the `base_images`
  declaration of them was the category error behind both placement checks and the build-arg
  pollution.

## Line laws

The invariants this line establishes. At closeout each graduates into AGENTS.md (growth there
is the operator's call) or dies with the line.

1. Three-class principle with one carve-out: the pins file is a deploy record — a cache can be
   stale, a record can only be superseded (authored form already in AGENTS.md).
2. Slot boundary: env slots exist only for values committed artifacts consume; anything only
   the assembly consumes (edge stanza, port stanzas) is a typed config field with typed
   overrides.
3. `boatyard.yml` contains zero `${...}` spellings; interpolation lives exactly where it
   happens — score sources and committed artifacts.
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

### Move 9 — tree vocabulary rename [NEXT; name undecided]

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
- Tests still owed: rename propagation across marker, verb, and docs (Move 9).

## What still dies

The current tree vocabulary (Move 9). Everything else the line set out to kill — including the
cross-file validator plumbing, `choices` state, pydantic context threading, and the
load-then-resolve two-phase constructor — died with Moves 1–10.

## This file

This file exists to be deleted: it holds the 0.7 line's future only. Present truth lives in
the code and AGENTS.md, history in git log, and the closeout record in closeout.md.

- A landed move's spec dies in the same session's doc fold and its number retires; anything
  that must outlive the line graduates into the AGENTS.md fold. Landing a move without
  shrinking this file is an incomplete landing.
- No diaries: no session narrative, no git-state snapshots (commit counts, branch ordering) —
  they rot on the next commit; the diff is the record.
- Sections are fixed, in order: identity, Process law, cursor, Closed rulings, Line laws,
  Moves (unlanded only), Transition, What still dies, this section.
- Rulings and laws are closed: a session reopens one only by the operator's call. A new ruling
  or an amended move replaces text here; it never appends history beside it.
