# Phase 6 Vehicle Flow Implementation Notes

## Why 1-3 Cars Are Smoke Only

- `1` to `3` vehicles only prove route validity.
- That scale is too small to support final policy claims.
- Smoke runs answer "does the route work?" not "what is the policy effect?".

## Route Validity Rule

- Vehicle routes must be built from the net, not by hard-coding `from_edge -> to_edge`.
- `sumolib.net.readNet(net_file)` is the source of truth for route search.
- If `from_edge` or `to_edge` is missing, mark route invalid.
- If shortest path is missing, mark route invalid.
- Invalid route stays out of vehicle route XML and still appears in validation CSV.

## Batch Net Separation Rule

- Each batch keeps its own net file.
- Do not mix batch01, batch02, and batch03 route logic into one shared network artifact.
- Route validation must use the exact batch net for that run.

## Fixed Smart Policy

- Keep smart extension fixed at `5.0` seconds.
- Keep max extension count at `1` per signal cycle.
- Do not let repeated pedestrians accumulate multiple `5` second boosts in the same cycle.
- Baseline must keep `extension_count = 0`.

## Vehicle Metric Caution

- `veh_waiting_time_mean` and `veh_time_loss_mean` from smoke runs are directional only.
- Low vehicle counts can make these metrics noisy.
- Arrived count and route-valid count are the first checks; metric interpretation comes later.

## Next Expansion

- Step 1: `NODE_7240` vehicle route smoke with `3` cars.
- Step 2: batch03 vehicle mini with `10` cars per candidate.
- Step 3: batch03 vehicle experiment with higher demand and repeated seeds.
- Final claims need enough vehicle demand and multiple seeds.

