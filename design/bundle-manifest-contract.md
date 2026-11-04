# Bundle manifest contract

Parked. Design locked except one open call (oci overrides, below). Nothing implemented.

## Problem

`up` validates every route upstream and port owner against compose's own
resolution (`docker compose config --services` over the materialized assembly)
because consumer-authored `boatyard.yml` references producer-defined service
names, and the only moment both sides coexist is after compose merges the
includes at `up` time. The check is late, costs a compose subprocess per run,
and its refusals name the mangled `{entry}-` spelling rather than the authored
declaration.

The same locality mismatch exists for environment variables, with a worse
failure mode: bundle artifacts deliberately leave consumer-interpolated holes,
and an unset plain hole interpolates to an empty string after one warning line
— a quiet misconfiguration (empty credential/host/port) rather than an error.
`up` already hand-rolls the single-variable special case of this (PUBLIC_URL
required under a selected auth option).

Principle: a reference can only be validated where its definition is visible.
The fix is to record the definition — a manifest derived from the artifact —
where the reference is authored.

## Verified compose facts (v5.1.3, probed 2026-11-04)

- `${VAR:-default}` interpolates with default semantics in `environment:`
  values and inside `configs.content`. `$$` doubling is unaffected.
- `${VAR:?err}` errors when unset and when set-but-empty — compose `:?`
  already means "set and non-empty".
- Plain `${VAR}` unset → one warning line, blank-string value.
- Compose interpolates every service regardless of profile activation: an
  inactive, profile-gated service's `:?err` hole hard-fails even
  `config --services` and `config --profiles`.
- `config --services` resolves `include:` and lists merged service names.
- Repo side: `ENV_HOLE_PATTERN` accepts the `:-` spelling; `COMPOSE_HOLE`
  captures the `:suffix` and the score scan preserves it; consumer-interpolated
  targets ride un-doubled into `configs.content`. One inferred-not-run link:
  bake/publish resolution of `:-` spellings (standard semantics; verify with a
  test at implementation time).

## Design

Fully derived manifests. No authored `requires:`, no embedding in artifacts:
the consumer scans the bundle's compose content at relock and records the
result in the lock. Justification: publish substitutes everything resolvable
and leaves only consumer-interpolated holes, so the artifact's hole set is the
curated consumer surface by construction — derivation reads intent the
pipeline already encoded. `:-` is the explicit optionality spelling; treating
plain holes as required turns ambiguity into a producer decision.

Manifest shape, per entry:

```
services:
  <name>:
    profiles: [debug]        # from the service's profiles: key, may be absent
    requires: [FOO, BAR]     # odd-parity holes excluding lock-key names;
                             # plain and :? spellings; :- excluded as optional
```

Lock format: `bundles:` becomes `name -> {reference, manifest}` (nested
record; breaking change — old locks parse-refuse, regenerate via one relock).
The lock is the offline cache; the fetch happens only at relock.

Relock: fetch artifact content via crane (today relock reads digests only;
content fetch for compose-bundle media types is new capability), scan, record.
Drift handling: refuse to write when a name still referenced by any route
upstream or port owner under that entry disappears from the new manifest;
print (do not refuse) newly required env vars — machine-local environment is
unknowable at relock. Rationale: the repo's universal pattern is refusal at
the earliest provable contradiction and it has no advisory channel;
print-and-proceed would invent one. The old digest keeps working until the
consumer adapts.

Validation points:

- Load, in every verb (config resolve, offline): route upstreams and port
  owners against the lock's manifests, for `ref:` entries. The `{entry}-`
  prefix cancels: authored bare name must be a manifest service name. Local
  entries have no lock record; their names validate at `up` by scanning the
  generated output (whose absence already refuses).
- `up`, after selection, before materialization: required env vars from the
  manifests of selected entries, filtered by each service's `profiles` against
  active `COMPOSE_PROFILES`, checked non-empty against the effective
  environment (shell + `.env` + per-run env file). Local entries and path
  overrides: scan the local file each run (already parsed for overrides).
- The up-time `config --services` block is deleted from the common path, as is
  the `config --profiles` call (profiles are manifest data now; the unknown
  profile refusal moves to manifest data at selection). `_compose_config`
  dies; `_refuse_unknown` relocates to the shared leaf for load-time reuse.

Secrets unify into the single rule: odd-parity hole = required, satisfied =
non-empty in the effective environment. No consumer-side cross-check against
`stack/secrets.yaml`; that declaration keeps its producer-side
consumed-subset-declared and k8s Secret roles.

Producer-side rule: `:?err` under a `profiles:` key refuses at generate-score.
Compose interpolates gated services regardless of activation, so such a bundle
bricks any consumer who has not enabled the profile and set the variable;
refuse at the artifact's birthplace, in the producer's CI.

Stays as-is: the PUBLIC_URL refusal (a consumer fact consumed by `up`'s own
edge rendering, not an artifact hole) and score-path `validate_consumed`
(producer-side declared-vs-consumed, a different fact).

## Open call

oci:// bundle overrides have no manifest and no lock record. Options:
(a) run the compose `--services` verification only when oci overrides are in
play — escape hatch stays safe, normal runs pay nothing (recommended);
(b) accept the silent-502 gap on the dev-only path and delete the block
entirely. Rejected: fetch-and-scan the override artifact — a network
round-trip per `up` against a mutable tag.

## Rejected alternatives

- Authored `requires:` with an embedded `x-` manifest: derivation reads intent
  publish already encodes; `:-` spells optionality without new authoring
  surface.
- Bundle-declared wiring (references co-located with definitions inside the
  bundle): the strongest form — makes consumer-side name authoring
  unexpressible and is net-negative LOC long-term. Deferred as a larger
  conceptual change; revisit if the contract machinery proves heavy.
- Stable alias per bundle (consumers reference only entry names): only models
  one endpoint per bundle.
- Keeping the compose block as a permanent floor: redundant with load-time
  teeth on every standard path; only the oci gap needs it.

## Size

Roughly +350-530 src lines (scanner module ~100-140; lock schema +35-50;
crane content fetch +40-100, the riskiest estimate; relock +60-90; load
validation +40-60; up env check +50-70 with up.py net ~-25 overall;
generate-score refusal +15-25), +400-700 test lines, and an AGENTS.md rewrite
of the "Validation teeth, in up" law in the same commit.
