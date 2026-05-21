# Phase 6 Vehicle Flow Diagnosis

## Scope

This note checks the current vehicle-flow runner only. No simulation was run.

## Keyword Search Result

Search target: `smart_crosswalk_sumo/*`

Findings:
- `bus_stop`, `accident`, `incident`, `disruption`, `normal_urban` exist in the project.
- Those terms appear in:
  - [smart_crosswalk_sumo/run_simulations.py](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/run_simulations.py)
  - [smart_crosswalk_sumo/main.py](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/main.py)
  - [smart_crosswalk_sumo/vehicle_sensitivity.py](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/vehicle_sensitivity.py)
  - config files under [smart_crosswalk_sumo/config/](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/config)
- The current vehicle runner file,
  [smart_crosswalk_sumo/run_phase6_vehicle_flow.py](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/run_phase6_vehicle_flow.py),
  does not contain those disruption CLI inputs or incident-generation code.

## Current Vehicle Runner

### Vehicle demand

- `--vehicle-count-per-candidate`: default `3`
- `--vehicle-depart-spacing-sec`: default `5.0`
- `--vehicle-start-time`: default `0.0`
- vehicle type: `car`, `vClass="passenger"`
- route source: shortest path on the net, not a hand-written `from -> to` route

### Route generation

- Candidate CSV is read directly from `--candidate-csv`.
- Runner requires these columns:
  - `crosswalk_id`
  - `nearest_junction_id`
  - `tls_id_used`
  - `ped_link_indices`
  - `route_from_edge`
  - `route_to_edge`
  - `crossing_edge_id`
- Vehicle path uses `sumolib.net.readNet(net_file)` and `net.getShortestPath(from_edge, to_edge, vClass="passenger")`.
- Invalid vehicle routes are skipped and recorded in `vehicle_route_validation.csv`.
- `demand_all.rou.xml` combines vehicle and pedestrian events, sorted by depart time.
- `sumocfg` points to `demand_all.rou.xml`.

## Bus Stop / Accident / Disruption Status

### Bus stop

- Bus-stop generation exists in the project.
- It is implemented in [smart_crosswalk_sumo/run_simulations.py](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/run_simulations.py).
- It is exposed through CLI flags in:
  - [smart_crosswalk_sumo/main.py](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/main.py)
  - [smart_crosswalk_sumo/run_simulations.py](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/run_simulations.py)
  - [smart_crosswalk_sumo/vehicle_sensitivity.py](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/vehicle_sensitivity.py)
- Current vehicle runner is not connected to that logic.

### Accident

- Accident generation exists in the project.
- It is implemented in [smart_crosswalk_sumo/run_simulations.py](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/run_simulations.py).
- It is exposed through the same disruption CLI path as bus stop.
- Current vehicle runner is not connected to that logic.

### Other disruption / incident modes

- `incident_scenario` / `disruption_scenario` exists in the main simulation path.
- Scenarios include `best_case`, `normal_urban`, `congested_urban`, `incident_case`.
- `illegal_parking` and `minor_incident` are also present in the main simulation pipeline.
- Current vehicle runner does not accept or forward these options.

## Current Smoke Result

Observed NODE_7240 smoke behavior:
- vehicle requested: `3`
- vehicle route valid: `3`
- vehicle departed: `3`
- vehicle arrived: `3`
- waiting time mean: `0.0`
- time loss mean: `1.997`

This is smoke, not real traffic.

Reason:
- only 3 vehicles were injected
- one short candidate was used
- no bus-stop / accident / disruption scenario is wired into the runner
- route is a shortest-path validation route, not demand calibrated to observed traffic
- no repeated seed experiment at scale yet

## Demand Rate

For the current NODE_7240 smoke:

`vehicles_per_hour = vehicle_count / sim_duration * 3600`

`3 / 600 * 3600 = 18 vehicles/hour`

So current smoke demand is `18 veh/h` for the single-candidate NODE_7240 run.

## Why This Is Not Real Traffic Flow

- Vehicle count is tiny.
- Vehicle route is only a validity smoke.
- Pedestrian logic is still the main purpose of the runner.
- No actual traffic volume profile is loaded.
- No bus stop or accident event is activated in this runner.
- No random disruption schedule is attached.

## What Is Needed For Real Vehicle Flow

1. Add traffic-demand inputs beyond `vehicle-count-per-candidate`.
2. Support route sets that reflect realistic origin-destination demand.
3. Add batch-scale vehicle counts per candidate.
4. Run multiple seeds.
5. Keep route-validity smoke separate from final demand experiments.
6. Decide whether disruption scenarios should be wired into this runner or kept in the main simulation path.
7. Add clear metrics for vehicle arrival, waiting, and delay at experiment scale.

## Current Connection Status

- Bus stop code exists in project: yes.
- Accident code exists in project: yes.
- Disruption / incident scenario code exists in project: yes.
- Connected to `run_phase6_vehicle_flow.py`: no.
- Current runner behavior: route smoke only.

## Next Priority

1. Keep the current runner as smoke.
2. Add a separate vehicle-demand experiment runner or an explicit high-volume mode.
3. Decide whether disruption scenarios belong in the vehicle runner or only in the integrated simulation path.
4. After that, scale vehicle demand and seed repetition before making any policy claims.
