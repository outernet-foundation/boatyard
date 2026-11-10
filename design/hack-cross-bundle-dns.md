# Cross-bundle network bridge hack

Status: deployable hack on `hack-cross-bundle-dns` (based on `boatyard-v0.7.2`); superseded
by the multi-project assembly ADR on `refactor-even-more` — delete this branch and its
runbook when that lands.

## Situation

Since per-bundle networks, every bundle sits on its own compose network. The rathole tunnel
container must dial two other bundles' services by bare docker DNS name — `edge:8443`
(works: the edge service joins every bundle net) and `livekit:6178` / `livekit:5957`
(broken: no bridge exists). The old workaround put deploy-host addresses into
`boatyard.values.yaml` (`TUNNEL_SIGNALING_ADDR` / `TUNNEL_RTC_ADDR`), which requires
introspecting the deploy host's IP and breaks the moment the host's address changes.

## Mechanism

After `up -d` returns, `bridge_project_networks` in `verbs/up.py` attaches every running
project container to every project network (`docker network connect`, discovered via the
`com.docker.compose.project` label; already-attached containers are skipped). Bare service
aliases then resolve assembly-wide, so the rathole client dials `livekit` directly
container-to-container — no host addresses, no published-port hairpin. The bridge re-runs
on every `up` (recreated containers start bare). `--attached` mode still hands off to
compose in the foreground and gets no bridge.

## Deploy recipe

On the deploy host, with Make-it-Sing and a boatyard clone side by side:

1. In boatyard: `git checkout hack-cross-bundle-dns`.
2. In Make-it-Sing's `boatyard.values.yaml`, set the tunnel dial addresses to the constants
   (they are the only machine-specific spellings this replaces):

   ```yaml
   TUNNEL_GATEWAY_ADDR: edge:8443
   TUNNEL_SIGNALING_ADDR: livekit:6178
   TUNNEL_RTC_ADDR: livekit:5957
   ```

3. Bring the stack up with the hack branch's boatyard (it resolves the consumer root from
   the working directory, so run it from Make-it-Sing):

   ```
   cd Make-it-Sing
   uv run --project ../boatyard boatyard up
   ```

`LIVEKIT_SIGNALING_IP` / `LIVEKIT_RTC_IP` stay as the public face (relay FIP, or the LAN IP
the devices bake in) — those advertise where clients connect, not where the stack runs.

`down` is unchanged; compose removes the bridge attachments with the containers.

## Verify

- `docker inspect <rathole-container> --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}'`
  lists every project network.
- `curl http://<public_host>:6178/` answers (LiveKit HTTP).
- `curl -X POST http://<public_host>:<gateway_port>/livekit-token/connection-details \
  -H 'Content-Type: application/json' -d '{"participant_identity":"check","room_name":"check"}'`
  returns connection details with `server_url`.

## Known sharp edges

- A container that exits between the `docker ps` snapshot and its connect makes `up` fail
  loudly after the stack is already up; rerun `up`.
- Duplicate bare names across bundles become resolvable-everywhere ambiguity (docker DNS
  round-robins); the tunnel is the only cross-bundle bare dialer today.
