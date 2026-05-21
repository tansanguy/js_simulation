# Phase 6 Vehicle Implementation Plan

## Goal

Bring vehicle routing into Phase 6 in a controlled sequence without collapsing smoke validation into final experiment scale.

## Sequence

1. Vehicle route smoke.
2. Vehicle mini.
3. Batch03 vehicle experiment.
4. Main expansion only after route validity and demand stability are proven.

## Vehicle Route Smoke

- Use `1` to `3` vehicles only.
- Purpose is route validity, not final policy measurement.
- Check that routes are valid against the matching net.
- Check that SUMO can load the route file without route errors.
- Check that vehicle placement and movement are legal on the corresponding network.

## Vehicle Mini

- After route smoke passes, raise vehicle demand slightly.
- Keep the run short.
- Validate that vehicles remain compatible with the smart crosswalk logic.
- Confirm no unexpected route failures before any broader batch run.

## Batch03 Vehicle Experiment

- Use batch03 as the vehicle-side experiment anchor.
- Keep batch03 aligned with `phase6_mini_experiment_candidates_batch03_only.csv`.
- Do not swap in the generic candidates CSV.
- Compare baseline and smart under the same route-valid setup.
- Keep the `5` second, `1`-time extension policy fixed.

## Main Expansion

- Expand only after batch03 vehicle results are stable.
- Increase vehicle demand only when route validity is clean.
- Repeat seeds before claiming a final pattern.
- Final experiment needs enough demand to create meaningful vehicle delay and queue effects.

## Batch Net Separation Principle

- Each batch gets its own net file.
- Do not reuse a network file across batches after topology or recovery logic changes.
- Keep batch01, batch02, and batch03 net artifacts separate.
- A batch-specific network file is part of the experiment record, not a throwaway temp file.

## Vehicle Route Validity Rules

- Validate route file against the exact net used for that batch.
- Route smoke passes only when SUMO accepts the route and vehicles move without topology mismatch.
- Treat route validity failure as a hard stop.
- Do not jump from route smoke failure to final experiment scaling.

## Fixed Smart Policy

- Keep the smart policy fixed at one `5` second extension.
- Keep max extension count at `1`.
- Do not broaden policy knobs while vehicle routing is still being validated.

## Final Experiment Rule

- `1` to `3` vehicles are smoke-only, not final-scale evidence.
- Final experiment needs enough vehicle demand and multiple seeds.
- Final claims must come from repeated runs, not a single smoke pass.
