# Phase 6 Junggu Traffic Demand Design

## Why the current 3-car run is only smoke

- The current vehicle runner can validate routes and basic SUMO insertion.
- `3 vehicles / 600 sec = 18 veh/h` is far below a real traffic experiment.
- That run is useful for route validity and control-flow smoke only.
- It is not enough to claim real congestion response, delay, or policy effect.

## Demand conversion rule

Use hourly demand as the experiment input, then convert it to the simulation horizon:

`vehicle_count = hourly_volume * sim_duration / 3600`

Example for `sim_duration = 600 sec`:

- `300 veh/h -> 50 vehicles`
- `600 veh/h -> 100 vehicles`
- `900 veh/h -> 150 vehicles`
- `1200 veh/h -> 200 vehicles`

## Paired seed design

- Baseline and smart must use the same seed.
- Baseline and smart must use the same vehicle demand.
- Baseline and smart must use the same pedestrian demand.
- Compare results as paired differences by seed.
- Report:
  - mean
  - standard deviation
  - 95% confidence interval

## Suggested 30-seed structure

- Run 30 seeds for each traffic level.
- Keep the candidate set fixed.
- Keep the route-validity smoke separate from the main experiment.
- Do not mix bus-stop, accident, or disruption stress tests into the base traffic-demand run.

## Demand modes

- `smoke`: small route-validity check
- `fixed`: fixed vehicle count per candidate
- `hourly`: convert hourly volume into simulated vehicle count

## Arrival process

- `deterministic`: even depart spacing inside the simulation horizon
- `poisson`: exponential inter-arrival schedule from the seed

## Stress scenarios

- Bus stop, accident, and disruption behavior already exists in the project.
- Those belong to a separate stress-scenario layer.
- Do not mix them into the base traffic-demand benchmark.

## Expansion priority

1. Keep the smoke path working.
2. Restore the original batch03 smart-extension timing by preserving the raw candidate order.
3. Add hourly traffic demand for baseline/smart comparison.
4. Scale up seed repetition.
5. Only then interpret vehicle delay and waiting-time trends as experiment results.
