# hole → slot: the env-interpolation vocabulary rename

Boatyard-local vocabulary rename, riding the `refactor` branch before the
0.6.0 release. "Hole" — the odd-`$`-parity `${NAME}` interpolation points a
compose consumer must fill — becomes "slot". Mechanism, grammar, and
artifacts are untouched: a word swap over identifiers, comments, refusal
messages, test names, and `AGENTS.md`, nothing else. No committed artifact
carries the word (the lock's `requires:` keys, values files, and compose
outputs are unaffected) — zero format churn, `lock --check` stays green.

Scope decisions (closed):

- Boatyard-local now. Consumer docs keep "hole" until the 0.6.0 sweep;
  `closeout.md` Work 4 step 8 owns their vocabulary flip. The fork is
  accepted and temporary.
- The toolkit is verified clean — its "hole" greps are all "whole"-family
  words; touch nothing there.
- `design/` is out (stale-by-design tier; a folded section adopts the term
  if it ever hardens into `AGENTS.md`).
- This file lives at the repo root deliberately: it is ephemeral by design
  and deletes itself as its final step, unlike `design/` initiative files.

## Mapping

| Today | Becomes |
|---|---|
| `required_compose_holes` | `required_compose_slots` |
| `COMPOSE_HOLE` (config, score_compose import) | `COMPOSE_SLOT` |
| `ENV_HOLE_PATTERN` | `ENV_SLOT_PATTERN` |
| `_require_port_or_hole` | `_require_port_or_slot` |
| `compose_command(..., hole_names=...)` (compose.py, up.py caller) | `slot_names=` |
| score_compose local `hole` loop var | `slot` |

Word-level: `hole`→`slot`, `holes`→`slots` in comments, test names, test
fixtures, assertion strings, and exactly two refusal messages (config.py's
"literal port number or a `${…}` env hole" and "bind to the lock or a
service sha via a `${NAME}` hole" — tests assert both).

## Steps

1. Rename the six identifiers above; chase importers by grep
   (`score_compose.py` imports `COMPOSE_HOLE`; `up.py` passes
   `hole_names=`).
2. Flip the two refusal messages and their test assertions.
3. Mechanical word swap over remaining comments and test
   names/fixtures/assertions (~85 test-side matches across eight files at
   audit time; `test_lock.py` and `test_score_generate.py` carry most).
4. Battery: `uv run pytest`, `uv run ruff check`, `uv run basedpyright`.
   One code commit; prose never rides it.
5. `AGENTS.md` lockstep — ~24 lines across the Lock (bundle-`requires` law),
   module map (names `COMPOSE_HOLE` and `hole_names` verbatim), values-file,
   compose-assembly, secrets, publish, and markers sections. Identifiers and
   refusal phrasings rename verbatim — they are what agents grep. Prose
   commit.
6. `closeout.md`: strike the Work-2 precondition line this initiative added,
   in this same session, once the rename has landed (that file's spec-first
   law). May share step 5's prose commit.
7. Verify: `rg -in "hole" src/ tests/ AGENTS.md closeout.md` returns only
   whole-family words ("whole", "wholesale"). `markers.py` carries no
   genuine hit today — keep it that way.
8. Delete this file; commit the deletion. The rename is done when its plan
   is gone.
