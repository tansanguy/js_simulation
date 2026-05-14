# Phase 6 Vehicle Experiment Draft Design

## Draft conditions

- `sim_duration = 600`
- `warmup = 0`
- `seeds = 1..30`
- demand levels:
  - `300 veh/h`
  - `600 veh/h`
  - `900 veh/h`
- observation scopes:
  - `100m`
  - `300m`
  - `500m`
  - `1000m`
  - `global`

## Why 600 sec

- short enough to keep runtime cost bounded
- long enough to observe a 5 sec extension event
- short enough to repeat 30 seeds without exploding cost
- if extension frequency is too low, extend to `900 sec` as a fallback

## Why 30 seeds

- paired baseline/smart comparison needs repeated samples
- 30 runs improve mean stability
- 30 runs do not automatically guarantee statistical significance
- summary should center on paired differences and confidence intervals

## Why multiple impact scopes

- `100m`: direct local effect
- `300m`: nearby approach edge effect
- `500m`: main draft scope
- `1000m`: wider ripple check
- `global`: sanity check only

## Demand conversion

Use hourly volume as the input.

`vehicle_count = hourly_volume * sim_duration / 3600`

For `600 sec`:

- `300 veh/h -> 50 vehicles`
- `600 veh/h -> 100 vehicles`
- `900 veh/h -> 150 vehicles`

These are draft sensitivity values, not final Junggu traffic estimates.

## Paired comparison rule

- baseline and smart use the same seed
- baseline and smart use the same vehicle demand
- baseline and smart use the same pedestrian demand
- compare seed-paired differences
- report:
  - mean
  - standard deviation
  - standard error
  - 95% confidence interval

## Traffic volume next step

- phase 1: fixed draft volumes `300/600/900 veh/h`
- phase 2: replace with TOPIS / T-Data matched traffic volumes
- phase 3: test `80% ~ 130%` robustness around the matched values

## Stress scenarios

- bus stop
- accident
- disruption

These are separate stress scenarios.
Do not mix them into the base traffic-demand benchmark.
