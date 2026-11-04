# Refactor session record: assembly pinning and validation

Resume point for the next session: read this file, then `git log --oneline refactor`.
Exactly one decision remains open (D8); the session opens by presenting it fresh.

## Ground truth: how pinning actually works

Re-derived from code after an earlier analysis error; do not re-derive from memory.

**Local score outputs** (`stack/generated/compose/compose.{name}.yaml`, committed per
producer repo) are templates, not runnable compose: `${ALL_CAPS}` holes throughout —
third-party images as bare lock-key holes (the barrier literally `${ALPINE_IMAGE:?err}`,
score_compose.py:187), first-party images as `${SERVICE}_SHA`-style holes. At `up`, a
per-run env file fills them: the consumer's own lock for its own entries (`PLATFORM=`
selects the platform manifest), and a path-override producer's lock for overridden
entries (read off the producer tree three parents up, un-pinned — the dev-flow
reproducibility loose end).

**Published bundles** (OCI artifacts) are self-contained except the consumer surface.
At publish, every lock-key hole is substituted with the **index-form** reference from
the producer's lock plus tree-sha tags for first-party images, and `finalize_baked`
digest-pins `{repository}/` refs (publish.py:86-89, verbs/publish.py:92-118). Surviving
holes are consumer-side facts only: gateway/ports, secrets, consumer-interpolated
files. Platform resolution differs by form: local outputs pick manifests via the env
file at `up`; published bake the index and the daemon resolves per arch at pull.

Consequences: a consumer's lock pins a remote bundle's internals transitively (one
artifact digest covers everything inside); two remote bundles needing different digests
of the same third-party image is already expressible (each bakes its own); bare shared
hole names exist only in the local-output flows.

**The fence** (`write_assembly_env_file`'s two-locks refusal, compose.py) guards exactly
those flows. Every compose-scoring repo must carry `ALPINE_IMAGE` in its lock (score
refuses generation without it), so any consumer with local compose entries can never
path-override any sibling's local output. Dogfood override — confirmed first-class
workflow — is structurally blocked.

Errors from the earlier attempt (do not repeat): "bundles-only consumers hand-pin
`.env`" (wrong — remote bundles are baked); "divergent third-party images across remote
bundles are unrepresentable" (wrong — baking handles it); "namespacing holes breaks
published artifacts family-wide" (wrong — published artifacts carry no image holes; the
rename touches only the local-output template layer). A carried-pins/OCI-annotation
design built on those premises was deleted; it solved non-problems.

## Open: D8 — the fence's disposition

Setup: the ground-truth fence paragraph above. Three moves:

- **Keep.** Dogfood stays blocked for consumers with image locks.
- **Demote to consumer-wins + warn.** A few lines: shared key takes the consumer's
  value, a warning names both locks. Bare names + one env file make precedence
  name-global — the override always runs the consumer's pins, never the producer's.
  Not a pure deletion: two path overrides from different producers sharing a key still
  have no local arbiter; that corner keeps refusing.
- **Namespace local-output holes.** Score emits `{QUALIFIED_NAME}_NAME` spellings (the
  `bundle_lock_key` law) via one rewrite pass over lock-key-shaped holes;
  consumer-surface holes (gateway, secrets, consumer-interpolated) stay bare — the
  lock's key set draws the line. Publish substitution keys prefix mechanically at bake.
  The env writer fans consumer lock keys per selected entry and takes producer-lock
  values for overridden entries. Fence becomes structurally impossible and is deleted.
  Embedded consequence, stated openly: overrides default to producer-wins for that
  entry's keys (the override's purpose); one line to flip. Corrected cost: every
  producing repo's `generate-score --check` fails once until a regeneration commit —
  the loud forcing function already in CI. No republish, no relock, old digests
  unaffected.
- Recommend: **namespace** — the codebase's signature move for this collision class
  (services, config keys, networks are already namespaced per entry for this reason);
  deletes rather than softens the guard. Demote if dogfood must unlock this week and
  the override never running the producer's pins is acceptable.

## Closed this session

- D1 — route/port refusal text mentioning profiles: no change. Text accurate;
  phrasings byte-stable; hint would be noise in the common case.
- D2 — dedup identical producer/consumer lock paths: no change. No user story exists
  (your own repo is selected via `BUNDLES=`, never overridden); the fence catches the
  accident fail-closed; no code anywhere guards the case deliberately.
- D3 — `--profile` flags in validation calls: no change. Both carriage routes (shell
  env, `.env`) verified working live; `up`'s explicit flags exist for the human-visible
  handoff, a property internal validation doesn't share; the change only adds lines.
- D4 — extract `render_caddyfile`: keep inline. The AGENTS-SHARED
  inline-single-call-helpers letter vs compose.py's existing single-call helpers
  divergence is accepted debt.
- D5 — groups keep routes/ports at group level: keep. Group routes make drift loud at
  `up`; routes-only-on-entries would silence it (per-option surface drift authorable
  and invisible) plus a format break.
- D6 — score-time group-aware route/port validation: declined. The up-time teeth are
  the only guard by choice; local-entry authoring bugs stay first-`up`-caught.
- D7 — lock-time validation of remote bundles: no change. Lock's contract stays digest
  probes; content inspection duplicates a check `up` must keep anyway.
- Profile-name validation: no change, closed earlier — profile names live in bundle
  artifacts, selection in per-machine `.env`, they coexist only at `up`; compose
  silently no-ops unknown profiles and the check converts that to a loud refusal.
- Route/port reachability facts, recorded for any future re-opening of D6/D7: the real
  stories are group-route x option mismatch, remote drift across relock, typo,
  override-in-development, plus profile-hidden upstreams (`config --services` is
  profile-sensitive, verified live; `COMPOSE_PROFILES` reaches it from shell env and
  `--env-file` alike).

## Landed (branch `refactor`, ahead of origin)

- `Collapse assembly render: model_dump port stanzas, drop identity copies` — includes
  the pyright lesson: the two "identity copy" comprehensions are inference anchors
  (bidirectional typing to `list[Yaml]`/`dict[str, Yaml]` under invariance); they carry
  why-comments. Do not "simplify" them again.
- `Fold the three compose validation refusals into one helper` — `_refuse_unknown`
  with named byte-stable refusal-template constants (RUF027 rejects inline braced
  template literals; constants are the sanctioned shape).
- Fence-comment rewrite and the session-record commits.
