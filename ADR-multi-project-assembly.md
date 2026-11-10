# ADR: Multi-project assembly, bare artifacts, and the decomposed public face

Status: accepted (single execution pass, no staging; amended after triple audit)
Date: 2026-11-10
Scope: boatyard, infra-rathole, placeframe, lbe-toolkit, Make-it-Sing

## Context

Bringing up the Make-it-Sing backend on an engineer machine exposed a structural defect. The
rathole tunnel bundle must hand forwarded traffic to two other bundles' services by docker DNS
name (`livekit:6178`, `boatyard-gateway:8443`). Since commit 25569df ("Namespace compose bundles
under their entry with per-bundle networks"), every included bundle sits on its own network, so
cross-bundle DNS does not resolve; the only bridge is the boatyard-rendered gateway service,
which joins every bundle network. The tunnel had no bridge, and the documented workaround put
deploy-host LAN addresses into `boatyard.values.yaml` (`TUNNEL_SIGNALING_ADDR: <host-lan-ip>:6178`,
`TUNNEL_RTC_ADDR: <host-lan-ip>:5957`) so the rathole client could loop out to the host's
published ports. Those are the only machine-local values in the file and they exist solely to
compensate for the missing network reach.

Two further defects surfaced during diagnosis:

1. docker compose does not recreate a container when interpolated `configs.content` changes
   (proven minimal: flat single-file project, env-file change, `up -d` says "Running"). This
   is an upstream compose bug; boatyard's policy is to know that config-derived changes
   require down/up, not to build around it.
2. The assembly depends on bundle artifacts having boatyard-baked `{entry}-` prefixed service
   names. Arbitrary third-party OCI compose bundles cannot satisfy this, so the "any published
   compose file is consumable" property boatyard was believed to have does not exist. The same
   coupling freezes consumer entry names: renaming `core: cuda:` to anything else breaks
   routing, because the producer baked the entry name into its artifact at generate time.

## Decision

1. `up` orchestrates one compose project per selected bundle plus one gateway project, joined
   by a single external shared network. Compose `include:` — and with it `oci://` include
   passthrough — is deleted from the up path. Boatyard fetches each bundle's compose text
   itself (crane, already used at lock time), writes it to temp files unmodified, and runs
   `docker compose -p <project>-<entry> -f <bundle.yaml> -f <override.yaml> --env-file env up -d`.
2. Producers publish **bare** artifacts, where bare means: service names are the collapsed
   container names. Generation renames score's `{workload}-{container}` spelling to
   `{container}` (the claim-site refusal is retained — it is what makes collapse safe), and
   deletes the `{entry}-` service prefix, the per-bundle `{entry}-net` emission, and the
   bare-alias attach. Everything else in the generation survives untouched: bind-volume
   inlining with `double_dollars`/consumer-interpolated handling, the barrier image pin, the
   tree-sentinel rewrite, and the lock-slot namespacing.
3. The reserved gateway service is renamed `edge` → `boatyard-gateway`. Reserved names are
   namespaced by the tool that reserves them. The reservation is enforced at lock and at up
   over the recorded service names, remote bundles included (today up only checks local text).
4. The public face of the assembly is decomposed into four orthogonal values keys
   (`public_host`, `public_scheme`, `public_port`, `public_url_path`) replacing the
   `public_url` monolith. All four are minted as referenceable slots (`PUBLIC_HOST`,
   `PUBLIC_SCHEME`, `PUBLIC_PORT`, `PUBLIC_URL_PATH`) alongside the composed `PUBLIC_URL`.
5. `FORWARDED_PROTO`/`FORWARDED_PORT` are deleted; the forwarded headers are rendered as
   literals in the Caddyfile, derived from the public face.
6. The port knob `gateway:` becomes `gateway_port:` in both config and values.
7. Producers author route and publish-port declarations on their workloads as
   `outernet.internal/*` annotations; consumers adopt them as defaults and may override per
   entry in `boatyard.yml` or per machine in `boatyard.values.yaml`.
8. The lock records each bundle's service names, adopted routes, adopted ports, and any
   `container_name` declarations — all as required fields so a pre-change lock refuses at
   parse (loud-parse treaty), never silently renders a broken assembly.

## Amendment (2026-11-10): boatyard-owned images leave consumer locks

Pre-publication amendment, replacing the 0.7 owned-images design wholesale. The barrier and
gateway caddy are boatyard machinery: their pins live in boatyard's code as upstream tags
(`BARRIER_IMAGE`, `EDGE_IMAGE` in `models/config_file.py`), moved by boatyard releases — the
compatibility contract (rendered Caddyfile, barrier shell) belongs to the boatyard version
that synthesized it. Consumer locks, mirror enumeration, lock validation, and build bake
args carry neither (`owned_images` and `mirrored_base_images` are deleted; relock and mirror
run over `remote_base_images` alone). The barrier rides one global un-namespaced
`BOATYARD_BARRIER_IMAGE` slot, minted at resolve from the pin and refused as a hand-set
values key; generated artifacts spell it `${BOATYARD_BARRIER_IMAGE:?err}` at the barrier
service. The gateway document renders `EDGE_IMAGE` literally, so a gateway-declaring
consumer's lock may legitimately be image-empty and a pure consumer mirrors nothing
(Make-it-Sing drops its mirror step; the zero-target mirror refusal stays for misconfigured
repos). The `barrier_image:`/`edge_image:` config fields die; a producer whose Dockerfiles
build `FROM ${ALPINE_IMAGE}` declares its own `base_images` entry (placeframe). Accepted
risk, time-boxed: upstream tag re-points move the bytes at the next up — small for a shell
loop and a pinned-minor caddy, and retired when boatyard hosts its own mirror namespace for
both images.

## The assembly model

### Projects

- Project names derive from `project_name` in `boatyard.yml` plus entry names:
  `<project>-<entry>` per bundle, `<project>` for the gateway. Project names are compose's
  native namespace: two bundles shipping the same service name coexist because compose never
  merges them. No up ever fails on bundle-name conflicts. Entry names are validated at config
  load to compose-project-safe spelling (lowercase alphanumerics and `-`).
- One external bridge network `<project>-shared` is created idempotently before the bundle
  projects come up (`bash_check docker network inspect` then `docker network create`), and
  removed by `down` after the projects are gone. Concurrent ups racing the create simply fail
  the second create loudly; accepted.
- Bundle projects come up first in declaration order (no cross-project dependencies today),
  the gateway project last. `down` reverses. The orchestration loop uses `bash()` per
  invocation — `bash_handoff` is `os.execvpe` and never returns, so it can only serve a
  genuinely final foreground invocation — with `CalledProcessError` translated at the verb's
  top-level boundary to a fail-stop exit naming the failing project. `--attached` means:
  bundles detached, the gateway project up foreground via `bash_handoff` on the final
  invocation. `--build` runs before the loop, unchanged. Pull flags (`--pull missing`,
  `--quiet-pull`) ride every project invocation.

### DNS

- Every bundle service joins `<project>-shared` via its override document. Compose exposes
  the service's name (already the collapsed bare name, per Decision 2) on every network it
  joins, so bare names resolve across bundles on the shared net — this is what makes the
  tunnel's authored defaults (`livekit:6178`, `boatyard-gateway:8443`) work with no consumer
  wiring at all. Provisioner-emitted services (`loki`, `grafana`, `pf-postgres`) keep their
  authored single names and resolve identically.
- The override document additionally attaches an entry-prefixed alias
  (`<entry>-<service>`), unique because entry names are unique config keys. The gateway's
  Caddyfile dials these prefixed aliases (`upstream = f"{service.name}-{route.upstream}"` is
  unchanged), so gateway routing is never ambiguous.
- With every service carrying an explicit `networks:` key (the shared net), compose does not
  create per-project default networks at all (verified live).
- Duplicate bare names across two selected bundles are NOT refused (e.g. core-cuda and
  auth-keycloak both ship `wait-for-resources`). The assembly comes up; only bare **dialing**
  of the duplicated name would be ambiguous (docker DNS round-robins), and `depends_on` is
  project-scoped and name-exact so it is unaffected. The only cross-bundle bare dialer in the
  ecosystem is the tunnel. This is a documented, accepted ambiguity.
- The single hard reservation: a bundle defining a service named `boatyard-gateway` is
  refused at lock and at up. It would shadow the real gateway for the tunnel's
  `boatyard-gateway:8443` default.

### The override document

Per bundle, boatyard renders a small compose file merged as a second `-f` (hand-rendered, not
`yaml.safe_dump`, because it may carry the `!reset` tag):

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
    container_name: !reset null      # only when the bundle text declares one
```

- Service keys come from the lock's recorded service names.
- Ports land here (they previously landed in the root document). Ports from multiple `-f`
  files append (verified), so adopted ports are skipped for a service whose own text already
  declares ports (deduped by published/target/protocol).
- A bundle-declared `container_name` would collide globally on the host; the override
  stanza emits `container_name: !reset null` (verified working), so arbitrary bundles are
  safe without editing their text.
- Ports precedence: `boatyard.yml` entry `ports:` replaces the adopted set wholesale for that
  entry; `boatyard.values.yaml` `ports:` then overrides the published number per service —
  and may only name a service with exactly one port (a single scalar cannot disambiguate a
  multi-port service like livekit's 6178+5957; those use the boatyard.yml form). A consumer
  port declaration naming a service the bundle does not define is refused.

### The gateway project

- The gateway service is rendered as today (Caddyfile
  config, published `gateway_port`) with these changes: the service name is
  `boatyard-gateway`; its network is the shared net only (the every-entry-net attach is
  deleted); the `environment:` stanza is gone entirely.
- The Caddyfile render is unchanged except the auth-mode block bakes literals:
  `header_up X-Forwarded-Proto <public_scheme>` and `header_up X-Forwarded-Port
  <effective public port>` — the effective public port (58080 in tunnel mode), not the
  listen port. Consequence, accepted by ruling: changing the public face in auth mode is a
  config-derived change → down/up applies it (the known compose staleness rule).
- `EDGE_BASE_URL` mint respells to `http://boatyard-gateway:8443`.
- A zero-selection assembly renders the gateway project alone (today's edge-only behavior
  carries over).

### Fetch

- Remote entries: the lock's `OciArtifactManifest.fetch_compose_texts` (crane; `install_crane`
  runs first — today only relock does) pulls the bundle's compose text by pinned digest.
  Multi-layer artifacts yield multiple texts; each layer is written to its own temp file and
  all are passed as additional `-f` files. No digest-keyed cache in this pass — every up pays
  a small registry round-trip; add caching later only if ups feel slow.
- Local entries and `path_overrides`: the local file is used directly. The producer-root
  lookup machinery for override bundles is unchanged; those env entries ride the shared env
  file. The override document and project naming apply to them exactly as to remote entries;
  their service names are parsed from the artifact text (they have no lock record).

### Env file

- One temp dotenv, rendered from the resolved plan exactly as today (mints + hand values +
  optional-slot empties), passed `--env-file` to every project invocation with the same
  `env -u` scrub. Slot names are global across the assembly; two bundles consuming the same
  slot name get the same value, as today. Foreign vars are inert (compose interpolates only
  each file's own slots). Temp files live until the last invocation completes; on failure the
  error reports every retained path (env contents may carry secrets, so paths only).

## Values schema

`boatyard.values.yaml` (gitignored, per machine):

```yaml
public_host: 5.78.28.175       # the address the world types (relay FIP, proxy domain, or host IP)
public_scheme: https           # optional enum http|https, default http
public_port: 58080             # optional; defaults to gateway_port's effective value
public_url_path: /makeitsing   # optional, default empty
gateway_port: 8443             # optional; overrides the config default (host port collision)
bundles: [core-cuda, livekit, rathole-client]
ports: {some-single-port-service: 6179}   # optional per-service published overrides
values: { ...slots... }
```

- All four public keys are pydantic-optional at the schema layer — `generate-score` loads the
  values file for `platform` alone and must not demand a public face. `public_host` presence
  is enforced at resolve: any selected surface consuming a `PUBLIC_*` slot with `public_host`
  unset refuses.
- `PUBLIC_URL` is minted as `scheme://host[:port][/path]`, the port elided when it equals the
  scheme default (80/443). `PUBLIC_HOST`, `PUBLIC_SCHEME`, and `PUBLIC_URL_PATH` are minted
  as their key values. `PUBLIC_PORT` is minted as the effective public port — the explicit
  key, else the effective `gateway_port`, else the scheme default — always a concrete number,
  never elided. All five slots are referenceable in the values section
  (`LIVEKIT_SIGNALING_IP: ${PUBLIC_HOST}`) and none is hand-settable (the minted-slot
  refusal covers all five plus `EDGE_BASE_URL`).
- `public_host` may bear no relation to any interface the deploy host owns (in tunnel mode it
  is the relay's floating IP; behind a proxy it is the proxy's domain). It is a fact about
  the public face, not about the machine.
- In tunnel mode the URL's port belongs to the relay's service registration
  (infra-rathole's `Pulumi.dev.yaml` `services: gateway 58080`), not to the deploy host;
  `gateway_port` only serves direct-LAN access. In airgapped/direct mode the two coincide
  and only one number is ever set (`gateway_port: 58080`, `public_port` omitted).
- The old `public_url` typed field and its parser are deleted.

## Producer-authored routes and ports

- Workload files declare them as annotations (treaty: substrate needs live in workload files
  as `outernet.internal/*`; `boatyard.yml` holds no per-workload runtime fact):
  - `outernet.internal/route: <path>=<host>:<port>[,strip]` — comma-separated for multiples
    (e.g. `/grafana/*=grafana:3000,/loki/*=loki:3100,/*=api:8000`); `,strip` sets
    `strip_prefix` (keycloak's `/auth/*=keycloak:8080,strip`).
  - `outernet.internal/publish: <published>:<target>[/<protocol>]` — comma-separated (e.g.
    `6178:6178,5957:5957/udp`).
- Annotations ride score's `metadata.annotations` passthrough onto the service stanzas in the
  artifact text; relock extracts them into the lock record (paths, hosts, ports, strip flags,
  in artifact order — the `/*` catch-all must render last). The workload lens validates the
  grammar. The old "publish annotation is dead" stance in placeframe's docs flips.
- Adoption at resolve: config-authored entry `routes:`/`ports:` replace the adopted defaults
  wholesale when non-empty; the auth-group exactly-one-route validation and the route
  path-uniqueness refusal run over the combined declared + adopted set (two producers
  claiming one path with the consumer overriding neither is refused). Effective routes
  require a declared gateway.
- Make-it-Sing's `boatyard.yml` therefore empties: its `routes:` and `ports:` blocks are
  deleted and every bundle carries its own declarations.

## Lock schema

- `ConfigLockBundle` gains **required** fields (no defaults — pre-change locks refuse at
  parse, per the loud-parse treaty): `services: list[str]` (collapsed bare names, extracted
  from the fetched text at relock), `routes` and `ports` (the adopted declarations), and the
  set of services declaring `container_name`.
- Used by: override-document service stanzas, the `boatyard-gateway` reservation, the
  consumer-port-names-unknown-service refusal, and adoption at resolve.

## down

- Preserves the treaty: reads config only — no values, no lock, no selection. It derives
  candidate project names from the config's full entry set (`<project>-<entry>` for every
  declared entry whether selected or not, plus `<project>`), runs `compose -p ... down
  --remove-orphans` for each (absent projects are harmless; the `<project>` invocation also
  removes any legacy single-project assembly), then removes the shared network via
  `bash_check` probe + `docker network rm`. Named volumes re-create empty under the new
  project names — documented, accepted.

## Producer and consumer changes

- **infra-rathole**: redefault the gateway slot `$${TUNNEL_GATEWAY_ADDR:-gateway:8443}` →
  `:-boatyard-gateway:8443`; regenerate; republish. Signaling/RTC defaults (`livekit:6178`,
  `livekit:5957`) are already correct under collapsed naming.
- **placeframe**: respell the ngrok bundle's baked `http://edge:8443` →
  `http://boatyard-gateway:8443`; author annotations (`/auth/*=keycloak:8080,strip` on
  keycloak; `/grafana/*=grafana:3000,/loki/*=loki:3100,/*=api:8000` on api; no publishes —
  debug/admin ports stay consumer-side); author slot dev-defaults (`DEPLOYMENT_ENVIRONMENT`,
  dev S3/postgres/database values as `:-` defaults); regenerate bare; republish. Its own
  consumer surface also updates: `gateway:` → `gateway_port:`, its values sample to the
  four-key schema, and its committed test asserting the namespaced shape is rewritten.
- **lbe-toolkit**: author `/livekit-token/*=livekit-token:8000` on livekit-token and
  `6178:6178,5957:5957/udp` on livekit; `gateway:` → `gateway_port:`; regenerate bare;
  republish. The published artifact path is the producer's entry name (`livekit-tier`).
- **Make-it-Sing**: `boatyard.yml` drops its `routes:`/`ports:` blocks; `gateway:` →
  `gateway_port:`; the lbe ref repoints to the `livekit-tier` artifact path and all refs bump
  to the producers' new sha tags; samples rewritten to the four-key schema with known-good
  `bundles` sets including livekit; the three `TUNNEL_*_ADDR` values and their comment blocks
  die; the live values file keeps only rathole-output facts plus optional dev secrets, with
  `LIVEKIT_*_IP: ${PUBLIC_HOST}`.

## Sequencing

Single pass, ordered: **boatyard 0.8.0 on PyPI first** (producers' CI drift gates run their
locked boatyard, so no producer PR can go green until 0.8.0 resolves; `major_minor: "0.8"`),
then producers (infra-rathole, placeframe, lbe-toolkit — each: bump `requires: ">=0.8"` and
the dev pin, regenerate bare, republish at the PR-head sha tag), then Make-it-Sing (bump pins,
repoint refs, relock — record the new digests and per-bundle records). There is no
compatibility window and no migration: pre-change locks refuse loudly, and the deploy host
runs `down` (new code) before the first new `up`. Old boatyard cannot consume bare artifacts
and new boatyard does not need to consume baked ones; every artifact is republished in-pass.

## Accepted limitations

- No fetch cache (registry round-trip per up).
- `up` issues N+1 compose invocations; slower than one.
- Partial-failure states across projects are reported fail-stop (first failing project named);
  no rollback of already-up projects.
- `docker compose ls` shows N+1 projects per assembly.
- Duplicate bare service names across bundles are unguarded ambiguity for bare dialers (see
  DNS section).
- The compose non-recreate-on-config-change bug remains external; policy is down/up on
  config-derived changes.
- The toolkit's `build --lock-only --check` gate, expected by newer toolkit SHAs, remains
  unimplemented (known-dark; separate work).
- `path_overrides` retain their pre-existing constraint that the producer's entry name match
  the consumer's (slot names embed the producer's entry spelling).

## Rejected alternatives (for the record)

- **Keep per-bundle nets + a `joins` schema** (rathole joins livekit): fixes the values file
  but makes cross-bundle wiring a declared per-relationship cost forever and leaves arbitrary
  bundles unsupported (no join can be authored for a bundle you don't control).
- **Flat single network + producer prefixing + intra-bundle reference rewriting**: requires
  string surgery on authored env/config contents (`http://loki:3100` → prefixed), with
  false-positive risk; rejected once bare-name resolution on the shared net was retained.
- **Consumer-side fetch+transform+materialize (rename at up time)**: a reimplementation of
  include; multi-project achieves the same uniqueness guarantee through compose's native
  project namespace with no text rewriting. (The `container_name: !reset null` case is the
  one sanctioned override-file rewrite, and it removes rather than renames.)
- **Native `oci://` includes + accept same-name bundle failures**: compose include refuses
  duplicate resource names in the merged project, and include-level override files cannot
  rename; the limitation is structural, and multi-project removes it for less machinery.
- **`outernet.internal/edge-peer` annotation** ("wire the tunnel like the gateway"):
  superseded — bare names on the shared net make the tunnel's authored defaults resolve with
  no wiring concept at all.
- **`FORWARDED_*` env delivery**: its only robustness justification was dodging the compose
  staleness bug, which is policy (down/up), not machinery; literals are simpler.
- **Monolithic `public_url`**: packs three independent facts (host, scheme, port) plus path;
  the four-key decomposition covers every expressible case (userinfo/query/fragment are
  meaningless for a canonical service address) and removes the composed-vs-given precedence
  question.
- **Score-natural doubled names** (`livekit-livekit`) as the bare spelling: kept on the table
  by one audit; rejected because it breaks every cross-bundle reference example and forces
  dual-name bookkeeping; collapsing to `{container}` preserves all authored spellings.

## Expected size

The original "~+30 net src" estimate excluded the resolve/up rewrite shape and the
near-total test churn (boatyard's root-document test class and placeframe's
namespaced-shape test both rewrite wholesale). Treat boatyard as: ~+120-200 new src, ~-90
deleted, tests ~+200-300 new / ~-150 deleted. Producers: authored annotations/defaults plus
regenerated artifacts and test updates. Make-it-Sing: sample and config edits only.
