# Session compaction: placeframe auth/JWKS incident → boatyard values-file architecture

Written 2026-11-07 (~19:30 UTC) at the end of a placeframe backend debugging + design session.
Audience: the parallel boatyard session consolidating the two workstreams into one.
The ZED/capture-tool debugging is condensed to only what justifies the boatyard realizations;
the design conversation is recorded in full.

## Repo state at handoff

- `/workspace/placeframe` (host mount `/placeframe`): clean tree at `cdc88fde` "Repin
  boatyard 0.6.6 and raise the requires floor". Stack RUNNING since ~18:58 UTC 2026-11-07
  (bundles: `[core-cuda, auth-keycloak, tunnel-ngrok]`), containers healthy, **auth broken**
  (see incident). Current public URL: `https://saddling-dedicate-important.ngrok-free.dev`
  (changed by this session's restart; ngrok free re-randomizes per container start).
- `/workspace/boatyard` (host mount `/boatyard`): branch `closeout-0.6.6`, **ahead 5 of
  origin/main**, dirty (`pyproject.toml`, `uv.lock` modified), version `0.0.0.dev0` (dev
  sentinel bypasses the `requires` self-check). Placeframe consumes boatyard **0.6.6 via a
  PyPI pin** — a boatyard change reaches placeframe only through release +
  `uv lock --upgrade-package boatyard` in placeframe. Local commits in `/workspace/boatyard`
  are fine; push/merge is the operator's job (sandbox GitHub App token is Contents:read;
  PRs can be opened via `GH_TOKEN=$(uv run mint-github-app-token) gh ...`).

## Part 1 — The incident (condensed)

User completed a ZED-box capture; the phone app's captures list was empty.

- The capture itself SUCCEEDED: `POST zed-box/capture_sessions/stop` → 201; box enumerates
  `count=1` (capture name `bgcch`). The tar is still on the box; nothing was ever uploaded.
  `capture_sessions` table: 0 rows. Phone→backend connectivity fine (phone pushes both its
  own logs and drained `zed-capture` logs to Loki through the new ngrok URL).
- The list is empty because every `GET /capture_sessions/expanded` poll 401s. First 401s:
  API logged `JWKS fetch failed: ConnectError: [Errno -3] name resolution`. After those,
  the app silently dropped its credentials; every subsequent 5s poll is
  `401 'Missing bearer token'`. (App-side amplifier bug, capture-tool repo: 401 → silent
  logout + empty list instead of a re-login prompt.)
- **Root cause**: after the compose-bundles cutover, `api` rides only
  `placeframe_core-cuda-net`, `keycloak` only `placeframe_auth-keycloak-net`; only the
  boatyard-emitted `edge` straddles all nets. `/placeframe/boatyard.values.yaml:8` still
  says `AUTH_JWKS_URL: http://keycloak:8080/realms/placeframe-dev/protocol/openid-connect/certs`
  — the `keycloak` alias is unresolvable from the api container FOREVER (reproduced
  post-boot; not a restart transient). The sample file teaches the same broken spelling,
  which is where the live values file copied it from.
- Verified fix path: `http://edge:8443/auth/realms/placeframe-dev/protocol/openid-connect/certs`
  returns 200 from inside the api container (the edge routes `/auth/*` → `keycloak:8080`,
  strip_prefix, per `/placeframe/boatyard.yml:31-37`).
- **The immediate unblock has NOT been applied** (session was diagnose-only by convention):
  edit `AUTH_JWKS_URL` in `/placeframe/boatyard.values.yaml` to the edge spelling, restart
  the stack (or just the api container), log in again on the phone. The values file is
  host-mounted with real credentials — edit the one line, never overwrite the file.
  After re-login, verify the list shows the capture and the upload path actually fires
  (never exercised during the incident; the app never got past 401).

## Part 2 — Mapping the failure onto boatyard's own laws

The failure was not a placeframe bug so much as an undeclared cross-bundle coupling that
boatyard's namespacing law deliberately killed, with a stale values file as the casualty:

- Namespacing law (boatyard AGENTS.md, "Compose assembly and selection"): score renders
  every bundle under `{entry}-` on a per-bundle network carrying the bare name as alias —
  "accidental cross-bundle DNS fails instead of silently resolving; cross-bundle coupling
  rides the consumer's declared surface." `http://keycloak:8080` from api was exactly such
  an accident; the edge routes ARE the declared surface. The cutover did the right thing.
- The edge: service name `edge` is reserved (`EDGE_SERVICE_NAME`); the Caddyfile boatyard
  emits listens on internal `:8443` (constant; `GATEWAY_PORT` is only the published host
  port — live values has 58080, sample 8443 — do not confuse the two); the edge is attached
  to every selected bundle's `{entry}-net`; boatyard already derives
  `FORWARDED_PROTO`/`FORWARDED_PORT` for the edge stanza from `public_url` under a selected
  auth option (precedent for derived env at assembly time).
- Values file law: `boatyard.values.yaml` is the only machine-values source; typed fields
  (`bundles`, `path_overrides`, `public_url`, `platform`) + an open ALL_CAPS `variables`
  map; keys carrying `LOCK_SLOT_MARKER` (`__LOCK__`) refuse — "the lock fills those slots,
  never a hand" (precedent + enforcement pattern for machine-filled slots).
- Leaf-service law: the auth option's route is `routes[0]`; its resolved service carries
  `auth: True` (boatyard already knows which route is THE auth route).
- History: per-bundle compose artifacts landed in placeframe `71f39a0c` "Run generate-score"
  riding the bundles cutover (`017b8e86` "Merge PR #152: Compose bundles", boatyard 0.6.3
  cutover commits, repinned 0.6.6 at HEAD). Before that, one shared compose network made
  `keycloak:8080` resolve everywhere — the values file predates the topology.

## Part 3 — Design conclusions (the part the boatyard session is consolidating)

### 3.1 Mistake class: hand-copied function outputs

Test for any values file: *does the operator decide this value, or copy it?* Copied values
are derivation waiting to happen. In placeframe's values file the DECIDED facts are: the
public URL/domain, ngrok authtoken, credentials/passwords, ports, bundles, seed users,
keycloak lifespans. The COPIED ones: `PUBLIC_URL` (dup of the typed `public_url`),
`NGROK_DOMAIN` (host of the same URL), and all four AUTH URLs (functions of the URL + a
fixed realm path). The public URL string appears SIX times in the file. One of those
copies (`keycloak:8080`) was also a topology guess, and it rotted.

### 3.2 Two-layer model — tolerance and derivation compose

```
API contract:      five env slots (issuer, jwks, auth endpoint, token endpoint, audience),
                    provider-agnostic, issuer-presence switch, partial config = loud boot
                    error (placeframe workloads/api/src/settings.py check_auth_config).
                    KEEP AS IS — provider tolerance lives here, correctly.
Assembly values:   the instantiation of those five slots for the keycloak bundle placeframe
                    ships — 100% known facts, zero operator freedom. DERIVE.
```

A swapped provider simply overrides all five explicitly (all-or-none, which the API boot
guard already enforces). "Deriving auth values conflicts with provider tolerance" is a
false conflict: tolerance is the API's contract; derivation is the assembly's job for the
provider it ships.

### 3.3 Boatyard feature A: values-file interpolation

Open variables may reference other variables (and typed fields), resolved when `up`
materializes the one per-run dotenv (`render_env_entries`). Generic — no OIDC knowledge in
boatyard. Enables the target values file:

```
public_url: https://<domain>
PUBLIC_URL:            ${public_url}                      # or another derived spelling
NGROK_DOMAIN:          <derived: host of public_url>
AUTH_ISSUER_URL:       ${PUBLIC_URL}/auth/realms/placeframe-dev
AUTH_AUTHORIZATION_ENDPOINT: ${AUTH_ISSUER_URL}/protocol/openid-connect/auth
AUTH_TOKEN_ENDPOINT:   ${AUTH_ISSUER_URL}/protocol/openid-connect/token
AUTH_JWKS_URL:         ${EDGE_BASE_URL}/auth/realms/placeframe-dev/protocol/openid-connect/certs
```

The domain is spelled once; the realm path twice (once public base, once internal base —
or derive one from the other; open). The sample file becomes: fill domain + authtoken +
passwords; the AUTH block is pure `${...}` lines an operator never edits.

### 3.4 Boatyard feature B: machine-filled `EDGE_BASE_URL` slot

`http://edge:8443` in a consumer values file is the same mistake one layer down: `edge`
and `:8443` are boatyard-minted facts (reserved service name; the emitted Caddyfile's
listen port). Boatyard should derive `EDGE_BASE_URL=http://edge:8443` into the per-run env
whenever a gateway is declared, and protect it with the `__LOCK__` marker law pattern:
the values file may interpolate `${EDGE_BASE_URL}` but cannot hand-set it. Then `edge` and
`8443` are typed by zero humans — the mistake becomes unwritable, not merely caught.

Fact/owner table agreed in session:

| Fragment                              | Minted by                    | Correct home                    |
|---------------------------------------|------------------------------|---------------------------------|
| `edge` service name                   | boatyard (reserved)          | exported slot                   |
| internal `:8443`                      | boatyard (emitted Caddyfile) | exported slot                   |
| `/auth` route prefix                  | placeframe boatyard.yml route| versioned file, spelled once    |
| `/realms/placeframe-dev/...` + OIDC suffixes | placeframe realm export / OIDC standard | versioned sample |

### 3.5 Versioned vs gitignored principle

Bare-name service references are ACCEPTABLE in versioned repo files — placeframe's
`boatyard.yml` already does it constantly (`upstream: api:8000`, `upstream: keycloak:8080`)
and those rot in the same commit as topology changes. They are TOXIC in the gitignored,
host-mounted values file, which outlives repo changes — exactly this incident. Rule: every
literal in the values file is either an operator decision (domain, secrets, ports) or a
machine-filled/interpolated slot; versioned contracts get spelled once in versioned files.

### 3.6 Why the edge is the right JWKS target (vs alternatives)

- `keycloak:8080` direct: re-couples the core bundle to the auth bundle's network; the
  namespacing law exists to refuse this; core must run authless (anonymous mode mounts no
  keycloak).
- Public issuer URL (`https://<tunnel>/...`): token validation would depend on external
  tunnel uptime — an ngrok flap becomes an auth outage plus JWKS-cooldown storms.
- Edge route: the one declared cross-bundle surface, internal, up with the stack, and the
  same `/auth` path clients already exercise. Verified working from the api container.

### 3.7 Rejected alternative: deriving inside the API (single discovery URL)

The public-issuer / internal-JWKS split is a real OIDC deployment concern; the public→
internal rewrite is exactly the fact the API cannot know — it belongs to the deployment
edge layer (boatyard/values). The five-slot API contract stays untouched.

### 3.8 Considered, deprioritized

- Exporting the auth route prefix (`routes[0]`) as a slot too — boatyard already marks it
  (`auth: True`), but `/auth` spelled once in a versioned file is fine. Over-engineering.
- Collapsing five API slots to issuer+origin-override — weakens the provider-agnostic
  contract for non-standard providers. Rejected.

### 3.9 Placeframe-side items (completeness; not boatyard work)

1. Sample file fix — floor-grade even without boatyard changes: replace the
   `keycloak:8080` JWKS line with the edge spelling. The sample is the VECTOR of the
   broken spelling.
2. API boot-time JWKS probe (or `/health` reflecting JWKS state → container unhealthy):
   provider-agnostic, catches ANY unreachable spelling including a swapped provider's;
   converts "stack green, auth silently dead" into a loud `up`-time failure. This was the
   actual detection failure in the incident — every health check was green.
3. capture-tool app: 401 → re-login prompt instead of silent credential drop + empty list.
4. Minor: `scripts/src/scripts/loki_query.py` hardcodes container `placeframe-loki-1`;
   this assembly names it `placeframe-core-cuda-loki-1` (per-bundle namespacing again —
   same law, second casualty). Fallback used: `docker exec placeframe-core-cuda-loki-1
   wget -qO- '<urlencoded query>' | jq ...`.
5. Observed discrepancy, uninvestigated: Loki `service_name` label values post-restart are
   only {capture-tool, reconstructor, zed-capture}; workloads/AGENTS.md:170-172 claims
   api/keycloak/etc. also log to Loki. api logs were read via `docker logs` instead.
   Possibly an OTLP-push env regression from the bundles cutover — worth checking in the
   consolidated workstream.

## Part 4 — Workstream shape / scope branches

- **Floor (no boatyard change, unblocks placeframe today)**: live values file + sample
  JWKS → edge spelling; restart; phone re-login. Plus placeframe items 1–2 above.
- **Correct (the boatyard release)**: feature A (values interpolation) + feature B
  (`EDGE_BASE_URL` reserved machine-filled slot) + sample rewritten to interpolation +
  placeframe boot probe. These two features are the boatyard-session consolidation target.
- Sequencing note: placeframe cannot consume A/B until a boatyard release and pin bump;
  the floor does not block on any of it.

## Part 5 — Session operational facts (sandbox/environment)

- uv: placeframe pins `==0.12.15`; sandbox default uv is 0.11.14 (`/usr/local/bin`,
  `/opt/dev-toolchains`). 0.12.15 installed at `~/.local/bin/uv` (standalone installer,
  `uv self update 0.12.15` after install). PATH does NOT persist between shell calls in
  this environment — invoke `~/.local/bin/uv` explicitly.
- Stack lifecycle: always `~/.local/bin/uv run boatyard up --quiet-pull` from
  `/placeframe` root with `timeout: 600000`; `boatyard down` (no `--volumes`) then `up`
  was this session's restart. Never bare `docker compose`.
- Container names in this assembly: `placeframe-core-cuda-{api,loki,pf-postgres,...}-1`,
  `placeframe-auth-keycloak-keycloak-1`, `placeframe-edge-1`, `placeframe-tunnel-ngrok-ngrok-1`.
- Postgres: `docker exec placeframe-core-cuda-pf-postgres-1 psql -U placeframe_owner -d placeframe`.
- Current tunnel URL via: `docker logs placeframe-tunnel-ngrok-ngrok-1 2>&1 | grep -o
  'url=https://[^ ]*' | tail -1`.
- Secrets live in `/placeframe/boatyard.values.yaml` (NGROK_AUTHTOKEN,
  KEYCLOAK_ADMIN_PASSWORD, SEED_TEST_USERS, DB/S3 passwords) — deliberately NOT
  reproduced in this document.
- The phone app was re-pointed at the new ngrok URL by the user during the session
  (logs show it hitting the new host) and held a still-valid token from the pre-restart
  keycloak (DB volume persisted across the no-volume restart).

## Part 6 — Open decisions for the consolidated session

1. Exact interpolation syntax and its schema: does `${var}` referencing ride the open
   variables map only, or can typed fields (`public_url`) be referenced too? Cycle
   detection? Refusal wording for unresolvable references?
2. Slot name (`EDGE_BASE_URL` vs `EDGE_INTERNAL_URL` vs …) and whether it appears only
   when a gateway is declared; interaction with `path_overrides`/remote bundles.
3. Whether `PUBLIC_URL`/`NGROK_DOMAIN` derivation rides feature A in placeframe's values
   file or boatyard derives them natively (NGROK_DOMAIN is placeframe-specific; likely
   stays placeframe-side interpolation).
4. Boatyard versioning: pre-1.0 (`0.0.0.dev0` dev sentinel currently), so no `major_minor`
   ceremony is strictly required, but values-file interpolation is a soft schema change —
   decide whether the `closeout-0.6.6` branch carries these or a 0.7 line opens.
5. Whether the boot-time JWKS probe belongs in `/health` (container-visible unhealthiness)
   vs startup refusal (api exits loudly when issuer set but JWKS unreachable — mind the
   keycloak-starts-after-api ordering; the assembly's wait-for-resources may need to gate
   api on keycloak when the auth option rides).
6. The Loki label gap (3.9.5) — investigate whether the cutover dropped OTLP plumbing for
   core services.

## Part 7 — Reference index

- `/placeframe/boatyard.values.yaml:8` — broken `AUTH_JWKS_URL` (live, gitignored, credentials inside)
- `/placeframe/boatyard.values.yaml.sample` — teaches the broken spelling (AUTH block)
- `/placeframe/boatyard.yml:31-37` — auth option: route `/auth/*` → `keycloak:8080`, strip_prefix; `:56-66` core routes
- `/placeframe/stack/generated/compose/compose.auth-keycloak.yaml` — keycloak on `auth-keycloak-net`, alias `keycloak`; configurator env carries `KEYCLOAK_URL: http://keycloak:8080` + `PUBLIC_URL`
- `/placeframe/stack/generated/compose/compose.core-cuda.yaml` — api on `core-cuda-net`, alias `api`; `AUTH_*` soft passthroughs (`${AUTH_ISSUER_URL:-}` etc.)
- `/placeframe/workloads/AGENTS.md:54-58` — auth model docs (five-slot contract, issuer-presence switch, "compose-network endpoint which only the API reaches")
- `/placeframe/workloads/api/src/settings.py` — `check_auth_config` (partial-config boot guard; cleartext-OAuth guard)
- `/placeframe/scripts/src/scripts/loki_query.py` — hardcoded `placeframe-loki-1`
- placeframe commits: `cdc88fde` (HEAD), `71f39a0c` (generate-score artifacts), `017b8e86` (bundles cutover merge), `3f412b1f` (boatyard 0.6.3 cutover fixup)
- boatyard AGENTS.md sections cited: "Compose assembly and selection" (namespacing law, edge stanza, per-run env file), "ConfigFile schema" (values file law, LOCK_SLOT_MARKER), deployment-surface annotations (`edge` reserved)
