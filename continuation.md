# Continuation: parallel up wall + rich renderer

Implementation handoff for the next session. Two code commits and one prose commit,
in order: (A) parallelize `up` with dumb milestone output, (B) add a rich-based
per-project tree renderer, (C) record the change in the ADR and AGENTS.md. Do not
implement anything outside this document. Delete this file (or fold it per the
operator's ruling) once both commits land. The decision record is
`ADR-multi-project-assembly.md`; this document supersedes its "N+1 invocations;
slower than one" accepted limitation and its fail-stop failure semantics — the ADR
amendment in step C makes that official.

## Problem and constraints (why this design)

`up` currently runs N+1 compose invocations strictly serially (declaration order,
gateway last). Each invocation blocks on its project's full convergence (pull per
`--pull missing`, create, start, intra-project `depends_on`). The single-file era
was one invocation with compose-internal parallel pulls and dependency-ordered
starts; the goal is wall-clock parity with that.

Hard constraints discovered and settled in the design session (do not re-litigate):

- One `docker compose` invocation drives exactly one project. `-p` is single-valued;
  there is no multi-project `up`. `include:` merges everything into one namespace
  (name conflicts refuse, no rename) — that is the mechanism the multi-project ADR
  already rejected. Docker's own documented multi-project pattern is an external
  shared network plus separate `up` per project, which is what boatyard does.
- A child process inherits a byte stream, not a screen region. Two concurrent
  compose TUIs cursor-addressing one terminal trample each other. Therefore: when
  invocations run in parallel, their output must be piped (compose then
  self-disables its TUI and emits plain lines — same bytes as
  `docker compose up 2>&1 | cat`) and a single owning renderer draws the screen.
  This is exactly the architecture buildkit's `progressui`, compose's `[+] Running`
  display, and pulumi's display independently use: children emit status text/events,
  one process owns the terminal.
- `docker compose pull` as a separate parallel phase (phase-split alternative) was
  rejected: it contacts the registry unconditionally and `tree-` tags exist only
  locally, so local/path-override projects would fail every pull phase. Keeping
  `up --pull missing` inside parallel workers preserves tree-tag semantics for free.
- `bash()` and `bash_output()` are synchronous; parallelism comes from worker
  threads. All subprocess launch still goes through bashrun — no raw subprocess.

Standing house rules apply throughout: no docstrings, comments rare and
self-contained, callers before callees, classes at top of file, full-word variable
names, typer `Annotated` params, bashrun helpers only, no inline imports, no
closures (module-level workers with explicit args), line length 120, prose and code
in separate commits, one subject line under 72 chars, no trailers, never push (the
operator pushes).

## Design rulings (binding)

1. **The wall**: per-bundle work — remote fetch via crane (if remote), temp-file
   materialization of compose texts, override-document render, the
   `up -d --pull missing` invocation — runs concurrently in one worker thread per
   bundle. The shared-network create, `run_build` (`--build`), the env file, and
   the gateway document stay on the main thread before the wall, unchanged.
2. **Gateway strictly last, serial, via `bash()`** (or `bash_handoff` when
   `--attached`), after the wall drains. The gateway is skipped entirely when any
   bundle failed (routing into a half-up assembly is wrong; today's first-failure
   stop already prevented it).
3. **Failure semantics**: the wall drains — no sibling termination on first
   failure. Every failed project is reported (completion order) with its captured
   compose output replayed. Already-succeeded projects stay up. This replaces the
   ADR's "first failing project named; run stops" — the ADR amendment records the
   flip.
4. **Ctrl-C**: SIGINT lands on the main thread only, but compose children sit in
   the same foreground process group and receive the terminal's SIGINT directly;
   they exit non-zero, bashrun raises in the worker, the worker records failure.
   The main thread's `KeyboardInterrupt` path (currently up.py's bare
   `except BaseException` printing `_retained_report`) must first join all workers
   with a timeout, then print the retained report, then re-raise.
5. **N=1 degenerate case**: Phase A adds a shortcut — a single selected bundle runs
   through plain `bash()` exactly as today (full compose UX preserved while the
   real renderer doesn't exist). Phase B removes the shortcut so every `up` goes
   through the wall and gets the tree. Zero bundles (gateway-only) never enters
   the wall.
6. **Worker invocations pin `--progress plain`** (global compose flag:
   `docker compose --progress plain -p ...`). Piping already forces plain output;
   pinning makes it deterministic and gives the Phase B parser a stable surface.
7. **`install_crane()` is hoisted** to the main thread before the wall (once, if
   any remote bundle exists) — N workers racing the installer's binary write is a
   race nobody wants.
8. **`_failing_project` (the `-p (\S+)` regex over `error.cmd`) dies.** With
   workers, attribution is exact by construction — each worker knows its project.
9. **Temp files**: the env file and gateway document are materialized on the main
   thread before the wall; bundle texts and override documents are materialized
   inside workers, which return their paths; the main thread owns the
   `materialized` list (workers never mutate shared state — explicit returns only).
10. **rich** becomes a dependency in Phase B only (`rich>=13`; pick the floor at
    implementation time against the lockfile resolution). MIT, Textualize; the
    de-facto standard for this exact job (Live + Layout + Tree + Progress).
11. Output ownership: while the wall runs, workers never write to the terminal.
    Phase A: workers print one-line milestones under a module-level print lock.
    Phase B: workers `queue.put((project_name, line))`; the UI owns all drawing.
    Non-TTY stdout (CI, pipes, `| tee`) never starts `Live` — the milestone
    renderer remains as the permanent non-TTY fallback (Phase A code is not
    throwaway).

## Phase A — the wall, milestone output (one code commit)

Files: `src/boatyard/verbs/up.py`, `tests/test_assembly.py`.

1. Restructure `up()`:
   - Load config/lock/values, `resolve`, the values-file existence check —
     unchanged.
   - Materialize env file and gateway document on the main thread (the gateway
     render's auth/public-face `ValueError` must keep firing before any
     convergence starts).
   - `install_crane()` once if any bundle lacks `bundle_path`.
   - Create the shared network, run `run_build` if `--build` — unchanged order.
   - N bundles selected: start one `threading.Thread` per bundle
     (module-level worker, explicit args), declaration-order start; join all;
     collect results; then the gateway per ruling 2/3. One bundle selected: the
     shortcut (ruling 5) — run the same worker logic inline via `bash()` with
     live output instead of capture. Zero bundles: today's gateway-only path.
2. The worker, sketch (adapt names as the file demands, keep the shape):

   ```python
   def _up_bundle_project(
       bundle: BundleProject,
       root: Path,
       env_file: Path,
       slot_names: list[str],
       values: ValuesFile,
       shared_network_name: str,
       quiet_pull: bool,
       report: _MilestoneReport,
       collect: list[_WorkerResult],
   ) -> None:
   ```

   It does: fetch remote texts (crane fetch only — install is hoisted),
   `_materialize` the texts + override document, run
   `bash_output(compose_command(root, files, "--progress", "plain", "up", "--pull",
   "missing", *(["--quiet-pull"] if quiet_pull else []), "-d", ...))`, and append a
   `_WorkerResult` (fields: `bundle`, `ok: bool`, `elapsed: float`,
   `output: str`, `error: CalledProcessError | None`, `paths: list[Path]`) to
   `collect` under the print/report lock. `collect.append` under the GIL is safe;
   the lock exists for the milestone prints.
3. Milestone output (dumb, Phase-B-replaceable except as non-TTY fallback): with a
   module-level `threading.Lock`, print
   `<entry>: up started` when the invocation begins,
   `<entry>: ready in 41.9s` on success,
   `<entry>: FAILED in 12.3s` on failure followed by the captured stdout replayed
   two-space indented (bashrun already prints the child's stderr on failure).
4. Post-wall: if every result `ok` — unlink materialized files, run the gateway
   (`bash()`, or `bash_handoff` when `--attached`) exactly as today's code does.
   If any failed — do not run the gateway; raise `SystemExit` with a report naming
   every failed project (completion order) and every retained temp path
   (`_retained_report` generalizes: one line per failure plus the retained list).
5. Ctrl-C path: wrap the wall in the existing `except BaseException` — join
   workers (timeout ~10s), then the existing print-and-reraise.
6. Tests (`tests/test_assembly.py` rewrite of the up classes):
   - Concurrency proof: inject the invocation runner as a parameter (the worker
     takes a `runner` callable defaulting to the real `bash_output` path; tests
     pass a module-level fake that blocks on a `threading.Barrier(len(bundles))`
     before recording) — a serial wall deadlocks past the barrier timeout and the
     test fails; the wall completes only under true concurrency.
   - Set-based invocation assertions (files, flags, project names) replacing
     declaration-order assertions; gateway-last still asserted; gateway skipped
     when any bundle result is a failure.
   - Two failures both reported; success paths unlink temp files; failure paths
     retain them.
   - N=1 shortcut runs without threads; zero-bundle path unchanged.
7. Verify: `uv run pytest`, `uv run ruff check` green.
8. Commit: `Converge the bundle projects concurrently in up`.

Pre-flight verification for the implementer (do this before writing code):
read bashrun's `src/bashrun/bash.py` and confirm `bash_output` has no module-level
mutable state (it should be stateless per call); if any shared state exists, report
it and wrap worker calls in a launch-only lock rather than patching bashrun. Also
confirm in a scratch dir that `docker compose --progress plain up -d` streams plain
lines under a pipe on this daemon's version, and capture real fixture lines
(container lifecycle, pull, layer-download) — Phase B's parser tests pin them.

## Phase B — the rich renderer (one code commit)

Files: `pyproject.toml` (+`uv.lock` via sync), new `src/boatyard/up_display.py`,
`src/boatyard/verbs/up.py` (wiring), `tests/` (new `test_up_display.py`,
assembly-test updates).

1. `pyproject.toml`: add `rich>=13` (floor per resolution at implementation time);
   `uv sync`.
2. `up_display.py` shape (classes at top, module-level functions below, no
   closures):

   ```python
   class ContainerRow:      # service label, state str, bytes downloaded/total
   class ProjectState:      # project name, wall-state (working/ready/failed), start time, rows dict
   class WallDisplay:       # owns the queue, the state, the Live
       def feed(project: str, line: str) -> None        # workers call; queue.put only
       def finish(project: str, ok: bool, elapsed: float) -> None
       def run(workers: list[threading.Thread]) -> None  # main-thread pump + Live
   ```

   plus module-level `parse_container_line`, `parse_pull_line`, `render_tree`.

3. Event flow: workers call `display.feed(project, line)` (explicit arg, passed
   into the worker alongside the runner). The main thread runs the pump: `Live`
   with `refresh_per_second=4`; each tick drains the queue, applies parsed
   updates, `live.update(render_tree(state))`. Workers hold no drawing state. The
   pump exits when all threads have joined and the queue is empty; then `Live`
   stops, the final tree is printed once as plain text, failures replay their
   captured output below it, and the existing report/exit rules apply.
4. The parser (regex table; pin with fixtures captured in Phase A pre-flight —
   trust captured bytes over this table where they differ):

   | Plain-line shape | Effect |
   |---|---|
   | `Container <project>-<service>-<n> <State>` | row for `<service>` → State (Created/Starting/Started/Healthy/Exited/Error/unknown passthrough) |
   | `<service> Pulling` | row for `<service>` → pulling |
   | `<digest> Downloading [==>  ] X/Y` | byte counter onto the most recent pulling row (skip entirely when `--quiet-pull`) |
   | `<service> Pulled` | row → pulled |
   | anything else | ignored (count silently) |

   Container name → service label: strip the leading `<project_name>-` prefix and
   one trailing `-<digits>` index. Unknown/odd shapes render the raw state word —
   the parser never crashes the wall on unexpected text.
5. Rendering: one `rich.tree.Tree` per project under a `Group`; project header =
   state glyph (`⠿` working / `✔` ready / `✖` failed) + project name + elapsed;
   children = one row per container: service label, state, optional byte counter.
   Status→style map is one module-level dict. Rely on rich's clipping for overflow
   (no custom height management unless it looks bad live — if needed, collapse to
   `+N more` per project, ~10 lines).
   Target (informational, not byte-binding):

   ```
   ✔ makeitsing-core-cuda            41.9s
   ├── ✔ api                started
   ├── ✔ pf-postgres        healthy
   └── ⠿ localizer-cuda     pulling  212.4MB/1.9GB
   ⠿ makeitsing-livekit              12.4s
   ├── ✔ livekit            started
   └── ⠿ livekit-token      pulling  4.1MB/31.0MB
   ```

6. Terminal ownership: `Live` runs only while the wall runs; it stops before the
   gateway invocation (ruling 2 — `bash()`/`bash_handoff` own the terminal after).
   `run_build` output precedes `Live` start. Non-TTY stdout: never start `Live`;
   the Phase A milestone renderer runs instead (ruling 11).
7. Ctrl-C inside `Live`: the `with Live(...)` context exits (final frame printed),
   the worker-join + retained-report + re-raise path from Phase A applies
   unchanged.
8. Remove the Phase A N=1 shortcut (ruling 5): every multi-zero-bundle `up` goes
   through the wall; N=1 gets its tree like everyone else.
9. Tests: parser fixtures (real captured lines; assert service extraction,
   index-stripping, byte-counter attachment, unknown-state passthrough);
   render-to-text via `Console(record=True, width=100)` asserting exported frames
   contain expected rows; an end-to-end wall test with the fake runner emitting
   fixture lines through the queue; non-TTY fallback test. Note: repo conftest
   sets `_TYPER_FORCE_DISABLE_TERMINAL=1` (a CI ANSI sharp edge) — build test
   consoles explicitly with `force_terminal=False` and a fixed width so nothing
   ambient leaks in. No sleeps, no real threads beyond the barrier test's.
10. Verify: `uv run pytest`, `uv run ruff check`, `uv run --locked scripts/preflight.py`.
11. Commit: `Render the up wall as a per-project tree with rich`.

## Phase C — prose (one commit)

1. `ADR-multi-project-assembly.md`: append an amendment section (dated, matching
   the owned-images amendment's style). Content, in the ADR's voice: bundle
   projects converge concurrently (one worker thread per bundle; fetch,
   materialization, and the compose invocation inside the worker), the gateway
   remains strictly last and serial and is skipped when any bundle fails; failure
   reporting flips from first-failure-fail-stop to drain-and-report-all (every
   failed project named, its captured output replayed, successful projects stay
   up); parallel invocations pipe compose (`--progress plain`) with a single
   owning renderer (rich) over the streams — children never share the terminal;
   the "N+1 invocations; slower than one" limitation is retired (invocation count
   unchanged, serialization gone); pre-existing limitations that survive: no fetch
   cache, `docker compose ls` shows N+1 projects, partial-failure states remain
   reported rather than rolled back.
2. `AGENTS.md`: update the `verbs/up.py` Modules line to name the concurrent wall
   and the renderer module; add a `up_display.py` Modules line. Trade, don't
   append, per this file's own shape rules — if a treaty bullet must change, the
   `down`/`up` bullets stay as-is (they describe contracts this work keeps).
   Growing AGENTS.md is a human decision; this document is the operator's
   instruction to make exactly these edits and no others.
3. Commit: `Record the parallel up wall and the rich renderer in the docs`.

## Sharp edges checklist (each one verified-or-cleared in the session report)

- bashrun `bash_output` thread-safety (Phase A pre-flight step).
- Real `--progress plain` fixture capture before writing the parser.
- SIGINT mechanics: confirm children die with the terminal's Ctrl-C (process
  group), workers record failures, main-thread join drains, report prints once.
- `Live` vs `bash()` ownership boundary (gateway strictly after `Live` stops).
- `sys.stdout.isatty()` gate for `Live`; milestone fallback under pipes/CI.
- Container-name index stripping on services with trailing digits (`api2`,
  `s3-initializer`-style digit-bearing stems — a recorded trap in this repo).
- `install_crane()` hoisted (no worker race).
- Env scrub (`env -u` slot names) unchanged on every worker invocation.
- Multi-layer remote artifacts: all fetched texts + the override doc ride ONE
  compose invocation as successive `-f` files, exactly as today.
- Release ordering: `0.8.0` is not yet on PyPI and the branch carries unpushed
  work; whether this rides 0.8.0 or 0.8.1+ is the operator's cut, not the
  implementer's. Consumers are path-pinned to `../boatyard` and are unaffected
  either way. No schema, lock, or artifact format changes here — no consumer
  regeneration is caused by this work.

## Deliberately out of scope

- Crane fetch caching (the ADR's no-fetch-cache limitation stands).
- Running the gateway inside the wall or parallelizing `down`.
- Terminating sibling workers on first failure.
- Per-layer full detail (row byte counters only), tmux/pty terminal emulation,
  and any streaming helper added to bashrun (the queue pump needs none).
