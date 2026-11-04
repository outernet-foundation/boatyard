# Refactor session record: validation follow-ups

In progress, spanning sessions. "Locked" entries are decisions; open questions are
ordered cheapest-first. This file is the resume point: read it, then
`git log --oneline refactor`. The namespaced-holes/carried-pins ADR was punted whole
into `design/namespaced-bundle-pins.md` (parked, trigger-gated); the fence it would
delete carries a pointer comment in `compose.py`.

## Locked: validation analysis outcomes

From the independent reachability audit of the four `render_compose` validations:

- Profile validation: correctly placed and shaped. Profile names live in bundle
  artifacts, selection in per-machine `.env`; they coexist only at `up`. Compose
  silently no-ops unknown profiles; the check converts that into a loud refusal.
  Closed: no change.
- Route/port validation stories (all real): group-route x option mismatch (a group's
  routes ride with whichever option is selected, so group routes must hold for every
  option's bundle); remote bundle drift across relock; typo; override-in-development;
  profile-hidden upstreams (verified live: `config --services` is profile-sensitive —
  the refusal text conflates "no such service" with "hidden behind an unselected
  profile"; text ruled accurate enough, no change).
- The obviously-correct earlier tier: score-time validation of every local entry's
  routes/ports against its own rendered service set, at full group-aware scope
  (each option's rendering validated against `option.routes + group.routes`, the fold
  `Config.resolve_services` performs). Makes up-time route/port failure for local
  entries structurally impossible behind the `--check` freshness gate. NOT yet
  implemented — awaiting authorization (Q1).

## Landed this session (branch `refactor`, ahead of origin)

- `Collapse assembly render: model_dump port stanzas, drop identity copies` — audit
  round 1. Includes the pyright lesson: the two "identity copy" comprehensions are
  inference anchors (bidirectional typing to `list[Yaml]`/`dict[str, Yaml]` under list/
  dict invariance); kept as comprehensions with one-line why-comments. Do not
  "simplify" them again.
- `Fold the three compose validation refusals into one helper` — `_refuse_unknown`
  with named byte-stable refusal-template constants (RUF027 rejects inline braced
  template literals; constants are the sanctioned shape and make the phrasing law
  greppable).

## Open questions, cheapest-first

- **Q1 (authorization): implement score-time group-aware route/port validation now?**
  Independent of anything parked; implementable immediately; also de-fangs Q2.
  Implementation surface: the score render loop iterates `local_service_entries()`,
  which drops group context — the group-aware walk (the fold `resolve_services`
  performs, applied to every option rather than a selection) must be plumbed into the
  loop; validate each option's rendering against its leaf routes/ports; `compose.py`'s
  up-time teeth remain for remote drift and path overrides.
- **Q2 (schema fork, mostly de-fanged by Q1): groups keep routes?** Alternative is
  routes-only-on-entries (each option restates its surface; the cross-option contract
  disappears). With Q1 landing, group x option mismatch is CI-caught, so keeping
  groups costs little. Lean: keep.
- **Q3 (housekeeping batch, propose close-all as no-change):** route/port refusal text
  mentioning profiles (text accurate; byte-stable law); dedup of identical
  producer/consumer lock paths (unreachable in practice); `--profile` flags into the
  validation call (verified behaviorally redundant — env/`.env` carriage already
  activates profiles during `config --services`).
- **Q4 (session residue, independent): extract `render_caddyfile`, and decide the
  rule-text line it exposes.** The 17-line Caddyfile render block inside
  `render_compose` is the clearest extraction, but AGENTS-SHARED's "inline single-call
  helpers" rule as written forbids it, while compose.py already carries single-call
  helpers owning coherent units (`write_assembly_env_file`, `namespaced_route`). One
  deciding line either way: extract + soften the rule text to target trivial
  pass-throughs, or keep inline. Cheap, zero dependencies, pure readability.
