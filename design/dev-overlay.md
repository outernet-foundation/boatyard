# Cross-repo dev overlay (trigger-gated)

Pre-implementation design. Build only when the trigger holds: the
save → build → up loop's rebuild latency proves too slow in daily
consumer-driven development (the commit friction is gone — disk-state hashing
made the unit of iteration save → build). A session taking this work first
confirms the trigger; otherwise it does not run.

With a path-form override active, `up --dev` generates overlay entries from
the producer's declaration: the producer's dev-stage services, the producer's
repo root as the bind source (the same derivation the producer-lock
attachment uses), and the producer's `dev-toggles` as `${NAME:-default}` holes
— overlaid onto the consumer's assembly. `:dev` images come from the
producer's `build --dev`.

Verify: overlay generation over a producer/consumer fixture pair; toggle
holes present; bind sources point at the producer root.
