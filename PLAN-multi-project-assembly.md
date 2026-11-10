# Execution Plan: Multi-Project Assembly — boatyard 0.8.0 Wave

Five repos change in one pass, strictly ordered: boatyard first (released as 0.8.0 on PyPI), then the three producers (independent of each other), then Make-it-Sing, then end-to-end verification on the deploy host. Every step lists repo, branch, files, exact edits, and verify commands. Execute steps in order within a phase; do not skip verifies. The decision record is `ADR-multi-project-assembly.md` in this repo; nothing below requires it to be understood.

**Standing rules (apply to every step):**
- Working branches: boatyard `dev`, infra-rathole `dev`, placeframe `dev`, lbe-toolkit `compose-bundles`, Make-it-Sing `compose-bundles`. One branch per repo, continuing on the current integration branch.
- Commit at each step boundary. Prose (`*.md`, `*.sample`) and code never share a commit. Regenerated score artifacts always land as a dedicated commit with the exact message `Run generate-score` (no body). One subject line under 72 chars, no trailers.
- The agent never pushes, never merges, never runs `pulumi`. At each phase end, hand the operator the branch name + HEAD SHA; the operator pushes and merges. Producer CI publishes bundles at the merge (PR-head) SHA tag automatically.
- No secret ever enters a file that is committed or this plan. The rathole token lives only in the gitignored `boatyard.values.yaml` and in `pulumi stack output engineers --show-secrets` (infra-rathole, operator-run).
- House code style throughout: no docstrings, comments rare and self-contained, callers before callees, classes at top, full-word variable names, typer `Annotated` params, `bashrun` `bash()`/`bash_check()`/`bash_handoff()` only, no inline imports, no closures, line length 120, `extra="forbid"` on all pydantic models.

---

## Phase 0 — boatyard (repo `/workspace/boatyard`, branch `dev`)

Boatyard's quick loop is `uv run pytest` + `uv run ruff check`; full gate `uv run --locked scripts/preflight.py`. Every sub-phase commit leaves both green. Where a rename sweeps a module owned by a later sub-phase, the mechanical sweep belongs to the earlier commit (ripgrep the old identifier to zero occurrences across `src/` and `tests/`); the restructuring belongs to the owning sub-phase.

### 0a — values/config schema

**Files:** `src/boatyard/models/values_file.py`, `src/boatyard/models/config_file.py`, sweep in `src/boatyard/resolve.py`, `src/boatyard/verbs/up.py`, `src/boatyard/lock.py`, `tests/test_values.py`, `tests/test_config.py`, `tests/test_assembly.py`.

1. `models/values_file.py`:
   - Delete the `PublicUrl` class (lines 139–163) and the `public_url` field (line 37).
   - Replace with four optional keys:
     ```python
     public_host: str | None = None
     public_scheme: Literal["http", "https"] | None = None
     public_port: PortNumber | None = None
     public_url_path: str | None = None
     ```
   - Rename `gateway: PortNumber | None` (line 39) to `gateway_port: PortNumber | None`.
   - Add `public_url_path` validator: when set, must be `/`-rooted, carry no whitespace, and not end with `/` (refuse the bare `/` too; the default empty path is the spelling for "no path").
   - New slot constants beside `PUBLIC_URL_SLOT` (line 26): `PUBLIC_HOST_SLOT = "PUBLIC_HOST"`, `PUBLIC_SCHEME_SLOT = "PUBLIC_SCHEME"`, `PUBLIC_PORT_SLOT = "PUBLIC_PORT"`, `PUBLIC_URL_PATH_SLOT = "PUBLIC_URL_PATH"`.
   - `_refuse_key_shape` (line 126): replace the single `PUBLIC_URL_SLOT` branch with a refusal covering all five `PUBLIC_*` slots plus `EDGE_BASE_URL_SLOT` ("not hand-settable (minted from the public face)").
2. `models/config_file.py`:
   - Line 61: `EDGE_SERVICE_NAME = "edge"` becomes `GATEWAY_SERVICE_NAME = "boatyard-gateway"`. Sweep all users (`lock.py:13,116,117`; `resolve.py:9,200,201`; `verbs/up.py:18,95`).
   - Line 88: `gateway: PortNumber | None = None` becomes `gateway_port: PortNumber | None = None`. Sweep users (`config_file.py:159,291`; `resolve.py:62`; `verbs/up.py:80,83`).
   - Add `ENTRY_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")` and validate in `_validate_services`: every `services:` key and every `options:` key must match, and `project_name` must match the same pattern (both feed compose project names and the shared network name).
3. `resolve.py` `_minted` (lines 248–254) respells now (full restructure comes in 0d):
   - When `values.public_host is not None`: mint `PUBLIC_HOST` (host), `PUBLIC_SCHEME` (explicit key else `"http"`), `PUBLIC_URL_PATH` (explicit key else `""`), `PUBLIC_PORT` (explicit `public_port`, else effective `gateway_port` — config value or values override — else the scheme default 80/443; always a concrete number), and `PUBLIC_URL` as `scheme://host[:port][/path]`, the port elided when it equals the scheme default.
   - `EDGE_BASE_URL_SLOT` mints `f"http://{GATEWAY_SERVICE_NAME}:{EDGE_LISTEN_PORT}"` when a gateway is declared.
4. `verbs/up.py` sweep for greenness: the auth-mode branch (lines 91–94) now derives the forwarded pair from the effective public face and requires `public_host` when an auth option is selected (the literals switch and the stanza deletion happen in 0g).

**Verify:** `uv run pytest tests/test_values.py tests/test_config.py tests/test_assembly.py` green; `rg -n 'public_url|EDGE_SERVICE_NAME|config\.gateway|values\.gateway' src/ tests/` returns only intentional hits (none in src).
**Commit:** `Decompose public_url into four keys and rename gateway to gateway_port`.

### 0b — workload lens annotations

**Files:** `src/boatyard/models/workload.py`, `src/boatyard/models/config_file.py`, `tests/test_config.py` (or new workload-lens cases in it).

1. `workload.py` constants after line 19: `ROUTE_ANNOTATION = "outernet.internal/route"`, `PUBLISH_ANNOTATION = "outernet.internal/publish"`.
2. Classes at top (below existing classes, above first function):
   ```python
   class WorkloadRoute(NamedTuple):
       path: str
       upstream: str
       strip_prefix: bool = False

   class WorkloadPublish(NamedTuple):
       published: int
       target: int
       protocol: str = "tcp"
   ```
3. Declare the grammar regexes here (module constants): `ROUTE_PATH_PATTERN` (the `/`-rooted segment chain, optionally `/*`-wildcarded — the same shape `RouteConfig` validates at `config_file.py:420`) and `ROUTE_UPSTREAM_PATTERN` (`[a-zA-Z0-9][a-zA-Z0-9_.-]*:[0-9]+`). `config_file.py` imports them from `workload` (that import direction already exists); `RouteConfig._validate` reuses them so the two spellings cannot drift.
4. Module-level parse helpers (bottom of file): `parse_route_annotation(value: str) -> list[WorkloadRoute]` and `parse_publish_annotation(value: str) -> list[WorkloadPublish]`.
   - Route grammar: comma-separated `<path>=<host>:<port>` entries; a bare token `strip` attaches to the preceding entry setting `strip_prefix=True`; `strip` as the first token, padded entries, empty entries, or a path not ending `/*` combined with `strip` all refuse.
   - Publish grammar: comma-separated `<published>:<target>[/<protocol>]`, ports 1–65535, protocol `tcp` (default) or `udp`.
5. `_validate_deployment_surface_annotations` (line 44) parses both annotations so malformed grammar refuses at workload load.
6. `WorkloadDocument` gains `routes` and `publishes` properties returning the parsed lists.

**Verify:** `uv run pytest tests/test_config.py` green with new grammar cases (valid multiples, `,strip`, bad protocol, out-of-range port, `strip` without `/*`).
**Commit:** `Add outernet.internal route and publish annotation grammar to the workload lens`.

### 0c — lock schema + relock

**Files:** `src/boatyard/models/lock_file.py`, `src/boatyard/lock.py`, `src/boatyard/models/config_file.py`, `tests/test_lock.py`.

1. `models/lock_file.py` `ConfigLockBundle` (line 136) gains four **required** fields (no defaults — a pre-change lock refuses at parse):
   ```python
   services: list[str]
   routes: list[RouteConfig]
   ports: list[PortDeclaration]
   container_names: list[str]
   ```
   importing `RouteConfig`, `PortDeclaration` from `config_file` (declared import direction). `reference` and `slots` unchanged.
2. `lock.py` relock bundle loop (lines 102–118):
   - New module-level extractor `extract_bundle_records(texts: list[str]) -> BundleRecords`, a `NamedTuple` holding `services` (bare names, first-appearance order across the texts), `routes` (adopted `WorkloadRoute`s converted to `RouteConfig`, artifact order), `ports` (adopted `PortDeclaration`s, artifact order), `container_names` (sorted services whose stanza declares `container_name`), and `declared_ports` (per-service `ports` lists parsed from the texts, for the up-time dedupe). Annotations are read from each service stanza's `labels`/`annotations` mapping via `ROUTE_ANNOTATION`/`PUBLISH_ANNOTATION`, parsed with the workload-lens helpers; adopted routes'/ports' service references must be among the extracted `services` (loud refusal naming the annotation and the missing service).
   - The reservation check (lines 116–117) uses `GATEWAY_SERVICE_NAME` over the extracted service names.
   - `ConfigLockBundle(reference=pinned, slots=..., services=..., routes=..., ports=..., container_names=...)`.
3. `config_file.py` `refuse_unresolved_references` (line 752): delete the `prefix` parameter; references key on bare `route.upstream.partition(":")[0]` and `port.service`. Sweep callers in `lock.py:115` and `resolve.py:197` (drop `prefix=f"{name}-"`).

**Verify:** `uv run pytest tests/test_lock.py` green, including: a lock YAML without the four new fields refuses at `LockFile.load`; a fetched text defining service `boatyard-gateway` refuses at relock; annotations extract in artifact order with the `/*` catch-all last-authored preserved.
**Commit:** `Record services, adopted routes and ports, and container names in bundle locks`.

### 0d — resolve

**Files:** `src/boatyard/resolve.py`, `tests/test_assembly.py`, `tests/test_values.py`.

Replace `AssemblyPlan`/`IncludedBundle` (lines 33–50) with:

```python
class BundleProject(NamedTuple):
    entry_name: str
    project_name: str
    bundle_path: Path | None
    pinned_reference: str | None
    services: list[str]
    effective_routes: list[RouteConfig]
    effective_ports: list[PortDeclaration]
    container_names: list[str]

class PublicFace(NamedTuple):
    host: str
    scheme: str
    port: int
    url_path: str

class GatewayPlan(NamedTuple):
    port: int
    public_face: PublicFace | None
    routes: list[tuple[str, RouteConfig]]

class AssemblyPlan(NamedTuple):
    bundles: list[BundleProject]
    gateway: GatewayPlan | None
    env: dict[str, str]
    edge_image: str | None
```

Semantics:
1. Selection fold, unknown-name refusals (bundles/path_overrides), `path_overrides` producer-root lookup, orphan-value refusal, reference resolution, required-slot refusal, optional empties, and lock/tree env fans for local and override entries are unchanged. `includes` and the include-time `oci://` prefix are gone entirely.
2. Adoption per winner: remote entries take `services`/`routes`/`ports`/`container_names` from `lock.require_bundle(...)`; local and override entries take them from `extract_bundle_records([text])` over the artifact text read in the slot pass.
3. Precedence: config-authored `routes:`/`ports:` (the flattened entry's authored effective set) replace the adopted set wholesale when non-empty; otherwise the adopted set stands.
4. Combined-set validations (over declared + adopted of all winners): route path uniqueness (two producers claiming one path with the consumer overriding neither refuses); the auth group's selected option carries exactly one effective route; any effective route requires `config.gateway_port is not None`. The load-time authored-only checks in `config_file.py` stay.
5. `values.ports` re-validation against bundle records: a key must name a service defined by some selected bundle, and that service must carry exactly one effective port (a scalar cannot disambiguate a multi-port service like livekit's 6178+5957). Unknown service names refuse. The old "not among the port-declaring entries" check (lines 71–75) is replaced by this one.
6. `values.gateway_port` set while the config declares no gateway refuses (renamed from line 62).
7. Public-face refusal: with `values.public_host` unset, any selected surface whose slot vocabulary contains one of the five `PUBLIC_*` slots refuses, naming the slot and the entry.
8. `_minted` per 0a, with `PublicFace` computed once (`PublicFace` present iff `public_host` is set) and `GatewayPlan.port` = `values.gateway_port` else `config.gateway_port`. `EDGE_BASE_URL_SLOT` mints `http://boatyard-gateway:8443` when a gateway is declared.

**Verify:** `uv run pytest tests/test_assembly.py tests/test_values.py` green with new adoption/precedence/public-face cases.
**Commit:** `Resolve the assembly as per-bundle projects plus a gateway plan`.

### 0e — score_compose bare generation

**Files:** `src/boatyard/score_compose.py`, `tests/test_compose.py`, `tests/test_score_generate.py`.

In `generate_compose` (lines 203–263):
1. Delete the per-bundle network: remove `network = f"{name}-net"`, `document.networks = {network: {}}`, and `service.networks = {network: {"aliases": [collapsed_name]}}`. The artifact declares no networks at all.
2. Bare service names: `document.services[f"{name}-{collapsed_name}"] = service` becomes `document.services[collapsed_name] = service`. The claim-site refusal (two workloads claiming one collapsed name) is retained verbatim.
3. `depends_on` rewrite drops the `f"{name}-"` prefixing both in dict and list forms — dependency keys are already bare collapsed names and stay unchanged.
4. Bind-volume inlining: configs keys drop the entry prefix — `document.configs[path.name]` and `ComposeConfigReference(source=path.name, ...)` (was `f"{name}-{path.name}"`).
5. Untouched: `_refuse_colliding_spellings`, the barrier image pin to `${ALPINE_IMAGE:?err}`, `double_dollars`/consumer-interpolated handling, the tree-sentinel rewrite, and `_namespace_slot` lock-slot namespacing.

**Verify:** `uv run pytest tests/test_compose.py tests/test_score_generate.py` green; assertions updated to bare service keys, no top-level `networks:`, no aliases, bare configs keys, namespaced lock slots intact.
**Commit:** `Generate bare compose bundles without entry prefixes or networks`.

### 0f — compose_command

**File:** `src/boatyard/compose.py`, `tests/` sweep.

```python
def compose_command(
    root: Path,
    compose_files: list[Path],
    *arguments: str,
    project_name: str,
    env_file: Path | None = None,
    slot_names: Iterable[str] = (),
) -> str:
```
Spelling: `docker compose -p <project> -f <file> [-f <file>...] --project-directory <root> [--env-file <env>] <args>`, each file/env shlex-quoted, the `env -u` scrub over sorted slot names unchanged. An empty `compose_files` list emits no `-f` (the `down` shape).

**Verify:** `uv run pytest tests/test_assembly.py` invocation-spy cases updated to the new spelling.
**Commit:** `Spell the compose invocation with an explicit project and file list`.

### 0g — up

**File:** `src/boatyard/verbs/up.py` (rewrite), `src/boatyard/lock.py` (imports only), `tests/test_assembly.py`.

Structure (callers before callees; the verb first, renderers and unwind helpers below):

1. Load config/lock/values exactly as today (lines 46–52, including the values-file-exists check). `plan = resolve(config, lock, config_values)`.
2. Shared network: `shared_network_name = f"{config.project_name}-shared"`; create before any project when at least one project exists — `bash_check(f"docker network inspect {shared_network_name}")` else `bash(f"docker network create {shared_network_name}")`. A concurrent racing create fails loudly; accepted.
3. `ExitStack` holding every temp artifact: one env file (`render_env_entries(plan.env)`, unchanged, passed to every invocation with `slot_names=list(plan.env)`); per remote bundle one temp file per fetched compose text (`install_crane()` first, then `OciArtifactManifest.fetch_compose_texts(pinned)` per bundle — crane runs at up, not resolve); per bundle one override-document temp file; the gateway document (safe_dump — no `!reset` needed) with the Caddyfile as `configs.content` under `EDGE_CONFIG_NAME`, `double_dollars` applied. On failure the error reports every retained path, contents never (secrets). Replace `lock_env_unwind` with one shared unwind covering all temp files: unlink on success, keep + print paths on failure.
4. Override document, hand-rendered (never `yaml.safe_dump` — it may carry `!reset`), one per bundle. Shape (Make-it-Sing examples; `render_override_document(shared_network_name, bundle, values, declared_ports)`):
   ```yaml
   networks:
     makeitsing-shared:
       external: true
   services:
     livekit:
       networks:
         makeitsing-shared:
           aliases: [makeitsing-livekit]
       ports:
         - {published: 6178, target: 6178}
         - {published: 5957, target: 5957, protocol: udp}
       container_name: !reset null
   ```
   Emission rules: a stanza per name in `bundle.services`; the alias is `<entry>-<service>` (entry names are unique config keys); the `ports:` list carries the bundle's effective ports except those the bundle's own text already declares (dedupe key: published/target/protocol, over `declared_ports` from `extract_bundle_records`); `published` honors a `values.ports` scalar override (single-port services only, validated at resolve); the `container_name: !reset null` line appears only for services in `bundle.container_names`.
5. Orchestration: `if build: run_build(config=config)` before the loop (unchanged). Then bundles in declaration order, gateway last:
   - Bundle invocation: `bash(compose_command(root, [*compose_files, override_file], "up", "--pull", "missing", *(["--quiet-pull"] if quiet_pull else []), "-d", project_name=f"{config.project_name}-{bundle.entry_name}", env_file=env_file, slot_names=list(plan.env)))`. `bash()` per invocation — never `bash_handoff` in the loop (it is `os.execvpe` and never returns).
   - Gateway invocation: project name `config.project_name`, files `[gateway_file]`, same flags. Detached via `bash()` normally; `--attached` means bundles detached and the gateway foreground via `bash_handoff` on this final invocation.
   - The whole orchestration sits in one `try` at the verb's top-level boundary catching `CalledProcessError` and raising `SystemExit` naming the failing project and every retained temp path. First failure stops the run; already-up projects stay up (accepted).
6. Gateway document content: service `boatyard-gateway` (`GATEWAY_SERVICE_NAME`) with `image: plan.edge_image` (refuse when a gateway is declared but the lock lacks the `CADDY_IMAGE` pin — unchanged), the Caddyfile config mount, `ports: [{target: 8443, published: plan.gateway.port, protocol: tcp}]`, `networks: {shared: null}` — shared net only, and **no `environment:` stanza at all**. Top level: `name`, `networks: {shared: {external: true}}`, `configs`.
7. Caddyfile render: unchanged mechanism (declaration order, `handle`/`handle_path` per `strip_prefix`, raw blocks verbatim) with `upstream = f"{entry_name}-{route.upstream}"` (the entry-prefixed alias; today's spelling at up.py:107). The auth route's first block bakes literals: `header_up X-Forwarded-Proto <public_face.scheme>` and `header_up X-Forwarded-Port <public_face.port>` — the effective public port (58080 in tunnel mode), never the listen port. An auth selection requires `plan.gateway.public_face` (i.e. `public_host` set). `FORWARDED_PROTO`/`FORWARDED_PORT` exist nowhere.
8. Zero selection renders the gateway project alone (today's edge-only behavior).

**Verify:** `uv run pytest` green: N+1 invocations in order (bundles then gateway), per-project `-p` names, override-document bytes (ports dedupe, `!reset` only when declared, aliases), literal forwarded headers, fail-stop exit naming the project, attached-mode handoff on the final invocation only.
**Commit:** `Orchestrate up as one project per bundle plus the gateway project`.

### 0h — down

**File:** `src/boatyard/verbs/down.py` (rewrite), `tests/test_assembly.py`.

Config only — no values, no lock, no selection (treaty unchanged):
1. Candidates: `f"{config.project_name}-{row.name}"` for **every** declared entry in `config.service_entries` (selected or not), plus `config.project_name` itself (which also removes any legacy single-project assembly).
2. For each: `bash(compose_command(root, [], "down", "--remove-orphans", *(["-v"] if volumes else []), project_name=candidate))`. No `-f`, no env file. Absent projects are harmless.
3. Then remove the shared network: `if not bash_check(f"docker network inspect {shared}"): return` else `bash(f"docker network rm {shared}")`. Bundles first, network last. Named volumes re-create empty under the new project names (accepted).

**Verify:** `uv run pytest` green: the loop derives from the config's full entry set, `-v` rides every invocation, network removal trails the loop.
**Commit:** `Tear down every config-derived project then the shared network`.

### 0i — tests (dedicated churn)

**Files:** `tests/test_assembly.py` (wholesale rewrite of the root-document class), `tests/test_values.py`, `tests/test_lock.py`, `tests/test_compose.py`, `tests/test_score_generate.py`, `tests/test_publish_compose.py` (namespaced assumptions only).

Rewrite `TestRenderCaddyfile`/`TestPublicUrlDerivation`/`TestEdgeStanza` into: `TestPublicFaceMinting` (four-key composition, port elision on 80/443, `PUBLIC_PORT` always concrete, five-slot + `EDGE_BASE_URL` hand-set refusal, `public_host`-unset consumption refusal), `TestGatewayProject` (service `boatyard-gateway`, shared net only, no environment, published `gateway_port`), `TestOverrideDocument`, `TestUpProjects` (order, fail-stop, attached handoff), `TestDownLoop`, `TestAdoption` (replace-wholesale, combined path uniqueness, auth exactly-one over combined, values-ports single-port rule, unknown-service refusal), and Caddyfile cases re-anchored on entry-prefixed upstreams and literal forwarded headers. Update `AssemblyCapture` to capture per-project documents.

**Verify:** `uv run pytest` and `uv run ruff check` fully green.
**Commit:** `Rewrite the assembly tests for multi-project up and the public face`.

### 0j — AGENTS.md (prose)

**File:** `AGENTS.md`. Trades, one for one, keeping the fixed shape and ≤100-column lines:
- Replace the `down` treaty bullet with: `down` reads config only and derives every candidate project name from the config's full entry set plus the bare project name, removing the shared network last.
- Replace the `verbs/up.py` Modules line to name the per-bundle projects, the shared network, and the override document.
- Replace the edge-no-token-validation bullet's first clause to name `boatyard-gateway` (keep the oauth2-proxy sentence).

**Commit:** `Update the boatyard doc for multi-project up and down`.

### 0k — release 0.8.0

1. `pyproject.toml`: `version = "0.7.2"` → `"0.8.0"`.
2. `publish.yml`: `major_minor: "0.7"` → `"0.8"`.
3. Full gate: `uv run --locked scripts/preflight.py`.

**Commit:** `Release 0.8.0`.
**Hand off:** operator pushes `dev` and merges; the release workflow publishes `boatyard==0.8.0` to PyPI. **Phases 1–3 may not start their CI-green runs until `uv pip install boatyard==0.8.0` resolves.** Phases 1, 2, 3 are mutually independent — run them in parallel or any order.

---

## Phase 1 — infra-rathole (repo `/workspace/infra-rathole`, branch `dev`)

1. `stack/score/rathole-client.yaml` line 26: `$${TUNNEL_GATEWAY_ADDR:-gateway:8443}` → `$${TUNNEL_GATEWAY_ADDR:-boatyard-gateway:8443}`. Lines 29/32 (`livekit:6178`, `livekit:5957`) are already correct under collapsed naming — leave them.
2. `boatyard.yml`: `requires: ">=0.7.1"` → `">=0.8"`.
3. `pyproject.toml` dev group: `"boatyard>=0.7.1"` → `"boatyard>=0.8"`; then `uv sync`.
4. Regenerate: `uv run boatyard generate-score`. Expect in `stack/generated/compose/compose.rathole-client.yaml`: service `rathole-client` (bare), no `rathole-client-net`, no aliases, configs key `rathole-client-files-client.toml`, lock slot `RATHOLE_CLIENT__BOATYARD_LOCK__RATHOLE_IMAGE` intact, slots unchanged.
5. **Commit (code):** `Retarget the tunnel gateway default at boatyard-gateway and require boatyard 0.8`.
6. **Commit (generated):** exactly `Run generate-score`.
7. Prose: `AGENTS.md` bundle section — replace the namespaced-shape sentences (`rathole-client-rathole-client`, the `{entry}-` prefix, bare-alias mechanics) with the bare shape; update the pinned-release mention (`==0.7.0` → `==0.8.0` where it describes the pinning posture). `README.md` if it spells the service names.
8. **Commit (prose):** `Update the docs for the bare bundle shape`.

**Verify:** `uv run ruff check src/`, `uv run ruff format --check src/`, `uv run basedpyright src/` clean; `uv run boatyard generate-score --check` no-op; `uv run boatyard docker-build-matrix` prints `[]`; `uv run boatyard publish-compose --plan` prints `[{"bundle": "rathole-client"}]`.
**Hand off:** operator pushes/merges; CI's publish-compose ships `ghcr.io/outernet-foundation/infra-rathole/rathole-client:<merge-sha>`. Record that SHA for Phase 4.

---

## Phase 2 — placeframe (repo `/workspace/placeframe`, branch `dev`)

1. `boatyard.yml`:
   - `requires: ">=0.7.1"` → `">=0.8"`; `gateway: 8443` → `gateway_port: 8443`.
   - Delete `routes:` from `services.auth.options.keycloak` and from `services.core` (both move to workload annotations).
   - Keep every `ports:` block verbatim (api/lease-server localhost debug, seaweedfs-admin, ngrok 4040 admin — debug/admin ports stay consumer-side).
2. Workload annotations:
   - `stack/score/keycloak.yaml` metadata.annotations add: `outernet.internal/route: /auth/*=keycloak:8080,strip`.
   - `stack/score/api.yaml` metadata.annotations add: `outernet.internal/route: /grafana/*=grafana:3000,/loki/*=loki:3100,/*=api:8000`.
   - `stack/score/ngrok.yaml` command: `http://edge:8443` → `http://boatyard-gateway:8443`.
3. Slot dev-defaults in `stack/score/compose/postgres.yaml`, `seaweedfs.yaml`, `observability.yaml` — every listed `:?err` becomes a `:-` dev default (single occurrence semantics, all sites in each file):
   - `POSTGRES_ADMIN_USER` → `:-postgres`; `POSTGRES_ADMIN_PASSWORD` → `:-password`
   - `DATABASE_OWNER_PASSWORD`, `DATABASE_API_USER_PASSWORD`, `DATABASE_AUTH_USER_PASSWORD`, `DATABASE_ORCHESTRATION_USER_PASSWORD` → `:-password`
   - `S3_ACCESS_KEY` → `:-admin`; `S3_SECRET_KEY` → `:-password`
   (`DEPLOYMENT_ENVIRONMENT` already carries `:-development` in `config.yaml`.)
4. `scripts/pyproject.toml`: `"boatyard>=0.7.1"` → `"boatyard>=0.8"`; `uv sync --all-packages`.
5. Rewrite `scripts/tests/test_score_compose_generation.py` to the bare shape: bare service keys (`CORE_SERVICES` becomes the asserted key set directly), no `{entry}-net`/aliases, annotations riding as labels on `api`/`keycloak` (`labels["outernet.internal/route"]` spelled as authored), `depends_on` bare, ngrok command asserting `http://boatyard-gateway:8443`, lock-pinned images unchanged, keycloak `KC_HOSTNAME` still `${PUBLIC_URL:?err}/auth`.
6. **Commit (code):** `Author routes as annotations, respell the relay target, and default the dev slots`.
7. Regenerate from a clean clone of the branch tip (dirt hashes — the `stack/AGENTS.md` procedure): `git clone --branch <branch> . /tmp/opencode/pf-clean`, `uv sync` + `uv run boatyard generate-score` there, copy `stack/generated/**` back. **Commit:** exactly `Run generate-score`.
8. `boatyard.values.yaml.sample` → four-key schema: drop `public_url:`; add `public_host: <your-host>`, `public_scheme: https` (ngrok shape), `gateway: 8443` → `gateway_port: 8443`; keep `bundles:` and the `values:` block (the dev-defaulted Postgres/S3 keys may be dropped or commented as overrides; `AUTH_*` references keep `${PUBLIC_URL}`/`${EDGE_BASE_URL}` interpolation).
9. Prose: `stack/AGENTS.md` (the "publish annotation is dead" stance flips to the live `outernet.internal/publish` grammar; namespaced-with-alias sentences → bare; `gateway:` → `gateway_port:`), `AGENTS.md` Initial Setup item 1 (`public_url` → `public_host`/`gateway_port`), `README.md` ngrok/air-gap sections.
10. **Commit (prose):** `Update the docs and sample for the four-key values schema`.

**Verify:** `uv run pytest scripts/tests/test_score_compose_generation.py`; then the full `uv run --locked scripts/preflight.py` (runs the python battery, database battery on `pf-preflight-postgres`, codegen staleness); `uv run boatyard generate-score --check` no-op from a clean checkout.
**Hand off:** operator pushes/merges; CI publishes `core-cuda`, `core-rocm`, `auth-keycloak`, `tunnel-ngrok` under `ghcr.io/outernet-foundation/placeframe/` at the merge SHA tag. Record it.

---

## Phase 3 — lbe-toolkit (repo `/workspace/lbe-toolkit`, branch `compose-bundles`)

1. `boatyard.yml`: `requires: ">=0.7"` → `">=0.8"`; `gateway: 8443` → `gateway_port: 8443`; delete the option's `routes:` and `ports:` blocks (both adopted from annotations now). The group/option spelling stays `livekit`/`tier` — the published artifact path is the entry name `livekit-tier`.
2. `stack/score/livekit.yaml` metadata.annotations add: `outernet.internal/publish: 6178:6178,5957:5957/udp`.
3. `stack/score/livekit-token.yaml` metadata.annotations add: `outernet.internal/route: /livekit-token/*=livekit-token:8000`.
4. `pyproject.toml` dev group: `"boatyard>=0.7.1"` → `"boatyard>=0.8"`; `uv sync`.
5. **Commit (code):** `Adopt the tier's route and publishes from annotations and require boatyard 0.8`.
6. Regenerate via the clean-clone procedure (the livekit-token tree slots demand it), copy `stack/generated/**` back. **Commit:** exactly `Run generate-score`. Expect `stack/generated/compose/compose.livekit-tier.yaml` with bare services `livekit`, `livekit-token`, `wait-for-resources`, no `livekit-tier-net`, labels carrying the two annotations.
7. Prose: `README.md` sections naming the route/publish config or the namespaced shape.
8. **Commit (prose):** `Update the README for annotation-adopted routes and ports`.

**Verify:** `uv run ruff check .`, `uv run ruff format --check .`, `uv run basedpyright` clean; `uv run pytest` (livekit-token tests) green; `uv run boatyard generate-score --check` no-op; `uv run boatyard publish-compose --plan` prints `[{"bundle": "livekit-tier"}]`.
**Hand off:** operator pushes/merges; CI publishes `ghcr.io/outernet-foundation/lbe-toolkit/livekit-tier:<merge-sha>`. Record it.

---

## Phase 4 — Make-it-Sing (repo `/workspace/Make-it-Sing`, branch `compose-bundles`)

Requires: boatyard 0.8.0 on PyPI and all three producer PRs merged with artifacts published at their merge-SHA tags (fill `<…_SHA>` below with those tags).

1. `boatyard.yml` — final literal content (`livekit` stays a bare entry under `services:`: it rides in every assembly unconditionally and is never named inside `bundles:`):

   ```yaml
   requires: ">=0.8"
   project_name: makeitsing
   platforms:
     - linux/amd64
   gateway_port: 8443
   services:
     auth:
       options:
         keycloak:
           ref: oci://ghcr.io/outernet-foundation/placeframe/auth-keycloak:<PLACEFRAME_SHA>
     core:
       options:
         cuda:
           ref: oci://ghcr.io/outernet-foundation/placeframe/core-cuda:<PLACEFRAME_SHA>
         rocm:
           ref: oci://ghcr.io/outernet-foundation/placeframe/core-rocm:<PLACEFRAME_SHA>
     rathole:
       options:
         client:
           ref: oci://ghcr.io/outernet-foundation/infra-rathole/rathole-client:<INFRA_RATHOLE_SHA>
     livekit:
       ref: oci://ghcr.io/outernet-foundation/lbe-toolkit/livekit-tier:<LBE_TOOLKIT_SHA>
   ```

   (Every `routes:`/`ports:` block is deleted — each bundle carries its own declarations; the lbe ref repoints from the `livekit` path to `livekit-tier`.)
2. `pyproject.toml` dev group: `"boatyard>=0.7.2"` → `"boatyard>=0.8"`; `uv sync`.
3. `uv run boatyard lock` — relock records the new digests plus per-bundle `services`, `routes`, `ports`, `container_names` for all five bundles. Verify the lock key spelling for the bare `livekit` entry against `bundle_lock_key("livekit")`.
4. **Commit (code):** `Repoint the bundles at the 0.8 artifacts and relock`.
5. `boatyard.values.yaml.sample` — final literal content:

   ```yaml
   # Machine values — copy to boatyard.values.yaml (gitignored) and fill in.
   # The ambient shell fills nothing; this file is the only machine-values source.

   # The public face — the address the world types. In tunnel mode it is the
   # engineer's tunnel floating IP; behind a proxy, the proxy's domain; on a
   # direct LAN, the host's own IP. The five PUBLIC_* slots mint from these four
   # keys and values may reference them (${PUBLIC_HOST}, ${PUBLIC_URL}, ...).
   public_host: <your-floating-ip>
   public_scheme: http
   # The port the world dials. In tunnel mode it belongs to the relay's service
   # registration (gateway 58080 tcp), not to the deploy host. On a direct LAN
   # leave public_port unset and set gateway_port — the two coincide.
   public_port: 58080
   # public_url_path: /makeitsing   # optional, default empty

   # Assembly selection: bundles: names options by qualified group-option
   # spelling — one compute leg (core-cuda, or core-rocm on an AMD host),
   # optionally keycloak (auth-keycloak; the auth edge), optionally the tunnel
   # (rathole-client). The livekit tier is a bare entry and rides
   # unconditionally — it is never named here. Known-good sets:
   # [core-cuda, rathole-client] (anonymous edge, the default),
   # [core-cuda, auth-keycloak, rathole-client] (authenticated edge — set the
   # four AUTH_* URLs and the KEYCLOAK_* block below), [core-cuda] (air-gapped:
   # no tunnel). Never carry AUTH_ISSUER_URL without auth-keycloak selected —
   # the api treats issuer presence as keycloak mode and rejects the anonymous
   # edge.
   bundles: [core-cuda, rathole-client]

   # The gateway's published host port for direct-LAN access (config default
   # 8443). The tunnel path does not use it — the relay dials the gateway
   # container-to-container over the shared network.
   gateway_port: 8443

   # Local dev: override core-cuda with a sibling checkout's generated compose
   # output. Uncomment and adjust the path for your checkout.
   # path_overrides:
   #   core-cuda: ../placeframe/stack/generated/compose/compose.core-cuda.yaml

   # Machine values: one flat ALL_CAPS list under values; a key no assembly
   # surface consumes refuses.
   values:
     DEPLOYMENT_ENVIRONMENT: development

     # Stack connection facts — the engineer registry lives in the infra-rathole
     # project makeitsing-tunnel: `pulumi stack output engineers --show-secrets`.
     ENGINEER_NAME: <your-name>

     # Relay control endpoint. Paste the stack output's control_endpoint value
     # verbatim — it already carries the port. Use the relay's floating IP
     # (fip-control in the output), never the relay server's primary IPv4: both
     # answer :2333, but only the FIP survives a server replace.
     TUNNEL_CONTROL_ENDPOINT: <relay-fip>:2333
     TUNNEL_TOKEN: <your-rathole-token>

     # No TUNNEL_*_ADDR dials remain: the tunnel reaches the gateway
     # (boatyard-gateway:8443) and LiveKit (livekit:6178, livekit:5957) by bare
     # service name over the shared network — the bundle's baked defaults.

     # Set all four for OIDC; unset for anonymous. ${PUBLIC_URL} interpolates
     # the composed public-face slot.
     # AUTH_ISSUER_URL: ${PUBLIC_URL}/auth/realms/placeframe-dev
     # AUTH_JWKS_URL: http://keycloak:8080/realms/placeframe-dev/protocol/openid-connect/certs
     # AUTH_AUTHORIZATION_ENDPOINT: ${PUBLIC_URL}/auth/realms/placeframe-dev/protocol/openid-connect/auth
     # AUTH_TOKEN_ENDPOINT: ${PUBLIC_URL}/auth/realms/placeframe-dev/protocol/openid-connect/token
     # KEYCLOAK_ADMIN_PASSWORD: password
     # KEYCLOAK_ACCESS_TOKEN_LIFESPAN: 3600
     # KEYCLOAK_SSO_SESSION_IDLE_TIMEOUT: 2592000
     # KEYCLOAK_SSO_SESSION_MAX_LIFESPAN: 2592000
     # SEED_TEST_USERS: user:password

     # Postgres/S3 dev defaults ride inside the bundles as :- fallbacks; set
     # these only to override them.
     # POSTGRES_ADMIN_PASSWORD: password
     # S3_SECRET_KEY: password

     # LiveKit tier knobs — the bundle bakes all image pins; the connection
     # facts below are consumer-side. Signaling/RTC advertise the public face.
     LIVEKIT_API_KEY: devkey
     LIVEKIT_API_SECRET: devsecretlongenoughforhs256signing
     LIVEKIT_SIGNALING_IP: ${PUBLIC_HOST}
     LIVEKIT_SIGNALING_PORT: 6178
     LIVEKIT_RTC_IP: ${PUBLIC_HOST}
     LIVEKIT_RTC_PORT: 5957
   ```

6. `boatyard.values.airgapped.yaml.sample` — final literal content:

   ```yaml
   # Air-gapped LAN values — copy to boatyard.values.yaml (gitignored) for an
   # air-gapped deploy. No tunnel: the relay is unreachable, so rathole-client
   # is omitted from bundles and the TUNNEL_* values are gone entirely (the
   # client would only retry-fail). Re-adding is a one-line bundles edit plus
   # the TUNNEL_CONTROL_ENDPOINT/TUNNEL_TOKEN pair from
   # boatyard.values.yaml.sample if the router gains egress.
   public_host: 192.168.8.10
   public_scheme: http

   bundles: [core-cuda]

   # Direct LAN: the public port and the gateway's published port coincide —
   # set gateway_port and leave public_port unset.
   gateway_port: 58080

   values:
     DEPLOYMENT_ENVIRONMENT: development
     ENGINEER_NAME: airgap

     LIVEKIT_API_KEY: devkey
     LIVEKIT_API_SECRET: devsecretlongenoughforhs256signing
     LIVEKIT_SIGNALING_IP: ${PUBLIC_HOST}
     LIVEKIT_SIGNALING_PORT: 6178
     LIVEKIT_RTC_IP: ${PUBLIC_HOST}
     LIVEKIT_RTC_PORT: 5957
   ```

7. `AIRGAP-ROUTER-RUNBOOK.md`: the note at lines 38–39 retargets — the port 58080 now comes from `gateway_port:` in `boatyard.values.yaml` (direct mode) with `public_host` the laptop IP; the baked device URL `http://192.168.8.10:58080` is unchanged.
8. **Commit (prose):** `Rewrite the values samples and runbook for the public face`.
9. Live values file `boatyard.values.yaml` (gitignored, deploy-host working tree — **not committed**): rewrite to the tunnel-mode shape with the real facts — `public_host: 5.78.28.175`, `public_scheme: http`, `public_port: 58080`, `bundles: [core-cuda, rathole-client]`, `gateway_port` omitted (config default 8443 suffices), `ENGINEER_NAME: tyler`, `TUNNEL_CONTROL_ENDPOINT: 5.78.27.198:2333`, `TUNNEL_TOKEN:` carried over unchanged from the existing file (source of truth: the operator's `pulumi stack output engineers --show-secrets` in infra-rathole), all three `TUNNEL_*_ADDR` lines deleted, `LIVEKIT_SIGNALING_IP: ${PUBLIC_HOST}` and `LIVEKIT_RTC_IP: ${PUBLIC_HOST}`, ports 6178/5957, the Postgres/S3 block dropped (bundle defaults apply) or kept verbatim.

**Verify:** `uv run boatyard lock --check` no-op; `uv run ruff check .`, `uv run basedpyright`, `uv run pytest` (repo battery) green.
**Hand off:** operator pushes/merges.

---

## Phase 5 — end-to-end verification (deploy host)

Facts: engineer **tyler**; relay control **5.78.27.198:2333**; tyler's tunnel FIP **5.78.28.175**; relay service registrations: gateway **58080/tcp**, signaling **6178/tcp**, rtc **5957/udp**. The token never leaves the gitignored values file.

1. On the deploy host: pull the merged Make-it-Sing branch; `uv sync`; confirm `uv run python -c "import importlib.metadata; print(importlib.metadata.version('boatyard'))"` prints `0.8.0`.
2. Confirm `boatyard.values.yaml` matches Phase 4 step 9 (no `TUNNEL_*_ADDR` lines, `LIVEKIT_*_IP: ${PUBLIC_HOST}`).
3. `uv run boatyard lock --check` — no-op (fresh relock already committed).
4. Teardown with the new code before the first new up (also removes the legacy single-project assembly): `uv run boatyard down`. Expect per-project `down --remove-orphans` runs for every config-derived project name, then `makeitsing`, then the shared network removal.
5. `uv run boatyard up --quiet-pull`. Watch: crane fetches per remote bundle, then N+1 project invocations in order (`makeitsing-core-cuda`, `makeitsing-rathole-client`, `makeitsing-livekit`, …, `makeitsing`).
6. Verify:
   - `docker compose ls` lists `makeitsing-core-cuda`, `makeitsing-rathole-client`, `makeitsing-livekit`, `makeitsing` (N+1 projects).
   - `docker network inspect makeitsing-shared` shows members from every bundle; service aliases include `makeitsing-livekit`, `makeitsing-core-cuda-api`, etc.
   - `docker logs` on the rathole client container: all three tunnels (`tyler-gateway`, `tyler-signaling`, `tyler-rtc`) connect to `5.78.27.198:2333` with local dials `boatyard-gateway:8443`, `livekit:6178`, `livekit:5957` — the baked defaults resolving over the shared net.
   - From an external machine: `curl http://5.78.28.175:58080/` answers through the relay (gateway route to the api); TCP 5.78.28.175:6178 reachable (signaling); a LiveKit client build connects and completes an ICE handshake over UDP 5957.
   - Direct-LAN path: `curl http://<host-lan-ip>:8443/` answers (gateway_port publish).
   - `PUBLIC_URL` sanity: the values `${PUBLIC_URL}` mint equals `http://5.78.28.175:58080` (port 58080 ≠ scheme default, so it is not elided).
7. Config-derived-change policy check (the compose staleness rule): change `public_port` in the values file, `uv run boatyard down && uv run boatyard up --quiet-pull`, confirm the Caddyfile's `X-Forwarded-Port` literal moved — down/up is the mechanism, never `up` alone.

**Report:** every error encountered and its resolution; any partial-failure state is left in place and named (no rollback of already-up projects).

---

## Reference shapes boatyard renders (Phase 0 acceptance targets)

Override document (per bundle, hand-rendered, second `-f`):

```yaml
networks:
  makeitsing-shared:
    external: true
services:
  livekit:
    networks:
      makeitsing-shared:
        aliases: [makeitsing-livekit]
    ports:
      - {published: 6178, target: 6178}
      - {published: 5957, target: 5957, protocol: udp}
    container_name: !reset null      # emitted only when the bundle text declares one
```

Gateway document (single project, `name:` inert under `-p`):

```yaml
name: makeitsing
networks:
  makeitsing-shared:
    external: true
services:
  boatyard-gateway:
    image: <lock CADDY_IMAGE platform manifest>
    configs:
      - {source: devkit-edge-caddyfile, target: /etc/caddy/Caddyfile}
    ports:
      - {target: 8443, published: 8443, protocol: tcp}
    networks:
      makeitsing-shared: null
configs:
  devkit-edge-caddyfile:
    content: |
      :8443 {
      	handle /auth/* {
      		reverse_proxy auth-keycloak-keycloak:8080 {
      			header_up X-Forwarded-Proto http
      			header_up X-Forwarded-Port 58080
      		}
      	}
      	handle /grafana/* {
      		reverse_proxy core-cuda-grafana:3000
      	}
      	handle /livekit-token/* {
      		reverse_proxy livekit-livekit-token:8000
      	}
      	handle {
      		reverse_proxy core-cuda-api:8000
      	}
      }
```

(`$`-runes inside Caddyfile content are `double_dollars`-doubled as today; the forwarded literals above show tunnel-mode values.)
