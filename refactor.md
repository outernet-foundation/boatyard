# Refactor session record: assembly pinning and validation

Resume point for a multi-session effort: read this file, then `git log --oneline refactor`.
Decisions are flat — one question, one decision, no bundles. "Locked" entries are closed;
open decisions are ordered cheapest-to-answer first, heavyweight last.

## Ground truth: how pinning actually works

Re-derived from code after an earlier analysis error; do not re-derive from memory.

**Local score outputs** (`stack/generated/compose/compose.{name}.yaml`, committed per
producer repo) are templates, not runnable compose: `${ALL_CAPS}` holes throughout —
third-party images as bare lock-key holes (the barrier literally `${ALPINE_IMAGE:?err}`,
score_compose.py:187; workload image holes likewise), first-party images as
`${SERVICE}_SHA`-style holes. At `up`, a per-run env file fills them: the consumer's own
lock for its own entries (`PLATFORM=` selects the platform manifest), and a path-override
producer's lock for overridden entries (read off the producer tree three parents up,
platforms from its recorded platforms).

**Published bundles** (OCI artifacts) are self-contained except the consumer surface.
At publish, every lock-key hole is substituted with the **index-form** reference from the
producer's lock plus tree-sha tags for first-party images, and `finalize_baked` digest-
pins `{repository}/` refs (publish.py:86-89, verbs/publish.py:92-118). The holes that
survive in a published artifact are consumer-side facts only: gateway/ports, secrets,
consumer-interpolated files. Platform resolution differs by form: local outputs pick
manifests via the env file at `up`; published bake the index and the daemon resolves per
arch at pull.

Consequences:

- A consumer's lock pins a remote bundle's internals transitively — one artifact digest
  covers everything inside. The consumer's lock never records bundle internals.
- Two remote bundles needing different digests of the same third-party image is already
  expressible: each bakes its own.
- Bare shared hole names exist only in the local-output flows (own entries + path
  overrides sharing one per-run env file).

**The fence** (`write_assembly_env_file`'s two-locks refusal, compose.py) guards exactly
those flows: any key claimed by both the consumer's lock and an override producer's lock
refuses. Every compose-scoring repo must carry `ALPINE_IMAGE` in its lock (score refuses
generation without it), so any consumer with local compose entries can never path-override
any sibling's local output. Dogfood override — confirmed first-class workflow — is
structurally blocked.

Errors from the earlier attempt (recorded so they are not repeated): "bundles-only
consumers hand-pin image values in `.env`" (wrong — remote bundles are baked, bundles-only
consumers have no image holes); "divergent third-party images across remote bundles are
unrepresentable" (wrong — baking handles it); "namespacing holes breaks published
artifacts family-wide" (wrong — published artifacts carry no image holes; the rename
touches only the local-output template layer). The carried-pins/OCI-annotation edifice
designed under those premises solved non-problems and is dropped entirely.

## Locked this session

- Code, on branch `refactor` (ahead of origin): the assembly-render collapse
  (`model_dump` port stanzas, path-matched auth route, singleton list); the
  `_refuse_unknown` helper with named byte-stable refusal-template constants (RUF027
  rejects inline braced template literals — constants are the sanctioned shape).
- Pyright lesson: the two "identity copy" comprehensions are inference anchors
  (bidirectional typing to `list[Yaml]`/`dict[str, Yaml]` under invariance); they carry
  why-comments. Do not "simplify" them again.
- Profile-name validation (`config --profiles`): correctly placed and shaped — profile
  names live in bundle artifacts, selection in per-machine `.env`, they coexist only at
  `up`; compose silently no-ops unknown profiles, and the check converts that into a
  loud refusal. Closed: no change, ever reconsidered only if `.env` handling changes.
- Verified live: `docker compose config --services` is profile-sensitive, and
  `COMPOSE_PROFILES` reaches it from both shell env and `--env-file` carriage.
- Route/port validation reachability (facts, not decisions): the four real stories are
  group-route x option mismatch (a group's routes ride with whichever option is
  selected, so they must hold for every option's bundle), remote drift across relock,
  authoring typo, override-in-development; plus profile-hidden upstreams, where the
  refusal text conflates "no such service" with "hidden behind an unselected profile".

## Open decisions — flat, cheapest first

- **D1 — route/port refusal text: mention profiles?** The text ("resolves to no service
  in the selected assembly") is accurate for the profile-hidden case; refusal phrasings
  are byte-stable grep targets. Recommend: no change.
- **D2 — dedup identical producer/consumer lock paths in the fence merge?** Guards a
  consumer path-overriding its own repo's output — pointless (you select your own local
  entry) and unreachable in practice. Recommend: no change.
- **D3 — forward `--profile` flags into the validation `config` calls?** Behaviorally
  redundant: verified that env/`.env` carriage already activates profiles during
  `config --services`. Recommend: no change.
- **D4 — extract `render_caddyfile` from `render_compose`?** The 17-line Caddyfile
  render block is the clearest extraction, but AGENTS-SHARED's "inline single-call
  helpers" rule as written forbids it while compose.py already carries single-call
  helpers owning coherent units (`write_assembly_env_file`, `namespaced_route`).
  One decision, conditional consequence: extract + soften the rule text to target
  trivial pass-throughs, or keep inline and leave the rule alone. Recommend: extract,
  with the one-line rule edit in the same change.
- **D5 — do service groups keep routes/ports at group level?** The alternative
  (routes only on entries, each option restating its surface) deletes the cross-option
  contract by construction. With D6 landing, group x option mismatch is CI-caught, so
  keeping groups costs little. Recommend: keep.
- **D6 — authorize score-time group-aware route/port validation?** The score render
  loop iterates `local_service_entries()`, which drops group context; the fix plumbs
  the `resolve_services` fold (applied to every option rather than a selection) into
  the loop, validating each option's rendering against its leaf routes/ports. Makes
  up-time route/port failure for local entries structurally impossible behind the
  `--check` freshness gate; up-time teeth remain for remote drift and overrides.
  Recommend: yes.
- **D7 — lock-time route/port validation for remote bundles?** Fetch and parse each
  remote bundle at `lock` to validate upstreams/owners against its service vocabulary.
  Heavy (network + artifact parse in the lock verb), duplicates checks that must stay
  at `up` anyway (overrides, freshness). Recommend: no.
- **D8 — the fence's disposition.** The one heavyweight. Context above; options:
  - **Keep as-is.** Dogfood override stays blocked for consumers with image locks.
  - **Demote to consumer-wins + warn.** Smallest change (a few lines in
    `write_assembly_env_file`): shared keys take the consumer's value, a warning names
    both authorities. Mechanical constraint: with bare shared names, one env file gives
    one value per name assembly-wide — per-entry authority is inexpressible, so the
    override always runs the consumer's pins (announced), never the producer's.
  - **Namespace local-output holes.** The score render emits per-entry hole spellings
    (`{QUALIFIED_NAME}_NAME`, the `bundle_lock_key` law); publish's substitution keys
    namespace mechanically at bake time; the env writer fans the consumer's lock keys
    out per selected entry and takes the producer lock's namespaced values for
    overridden entries. The fence becomes structurally impossible (per-entry names
    cannot collide) and is deleted; producer fidelity for overrides becomes
    expressible. Corrected cost: every producing repo regenerates and recommits its
    score outputs — one-time churn; published artifacts and consumer locks are
    untouched (no republish, no relock, old digests unaffected). This is the
    codebase's signature move for this class of collision — services, configs keys,
    and networks are already namespaced per entry for exactly this reason.
  - Recommend: namespace (structural fix, house-style precedent, corrected cost is
    one regeneration pass); demote-to-warn if the override flow is meant to be quick
    rather than faithful.
