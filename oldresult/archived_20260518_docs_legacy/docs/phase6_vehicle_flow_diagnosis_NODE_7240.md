# Phase 6 Vehicle Flow Diagnosis: NODE_7240 Smoke

## Current Vehicle Implementation Summary

- `smart_crosswalk_sumo/run_phase6_vehicle_flow.py` reads candidate CSV directly.
- It resolves `crosswalk_id`, `route_from_edge`, `route_to_edge`, `crossing_edge_id`, `ped_link_indices`, and batch net path from that CSV.
- Vehicle routes are built with `sumolib.net.readNet(net_file)` and `net.getShortestPath(from_edge, to_edge, vClass="passenger")`.
- Invalid vehicle routes are skipped from `demand_vehicle.rou.xml` and still written to `phase6_vehicle_flow_{scenario}_vehicle_route_validation.csv`.
- Pedestrians are written to `demand_pedestrian.rou.xml`.
- `demand_all.rou.xml` combines vehicle and pedestrian demand into one XML file.
- `phase6_vehicle_flow_{scenario}.sumocfg` points SUMO at `demand_all.rou.xml`.
- After `traci.start`, the runner tries to count departed and arrived vehicles, then writes summary CSV, debug traces, and metadata.

## NODE_7240 Baseline Smoke Result

- `candidate_rows = 1`
- `completed = false`
- `vehicle_requested_count = 3`
- `vehicle_route_valid_count = 3`
- `vehicle_departed_count = 0`
- `vehicle_arrived_count = 0`
- `vehicle_route_invalid_count = 0`
- `veh_waiting_time_mean = NaN`
- `veh_time_loss_mean = NaN`
- `extension_count = 0`
- `extension_sec = 5.0`

## Confirmed Facts

- Route validation passed for all 3 vehicles.
- `demand_vehicle.rou.xml` is structurally valid and contains 3 vehicle routes.
- `demand_all.rou.xml` contains vehicle routes before vehicle declarations and then pedestrian entries.
- `phase6_vehicle_flow_baseline.sumocfg` points only at `demand_all.rou.xml`.
- `run_metadata.json` contains the runtime failure reason.
- `simulation_error` is not empty.
- `phase6_vehicle_flow_baseline_vehicle_debug_trace.csv` and `phase6_vehicle_flow_baseline_pedestrian_debug_trace.csv` are effectively empty because the run aborted before normal loop completion.

## Most Likely Root Cause

- Runtime failure happens inside the TraCI loop at `traci.simulation.getDepartedVehicleIDList()`.
- The installed TraCI API does not expose `getDepartedVehicleIDList`.
- The available methods are `traci.simulation.getDepartedIDList()` and `traci.simulation.getArrivedIDList()`.
- Because that exception is thrown inside the `try` block, `completed` becomes `false` and the run stops before any vehicle departure/arrival accounting completes.

## Secondary Cause Candidates

- `vehicle_departed_count = 0` is a consequence of the runtime exception, not evidence that the route was invalid.
- `veh_time_loss_mean = NaN` follows from the same early abort.
- `ped_crossing_person_count = 0` also follows from the same abort.
- `demand_all.rou.xml` order is probably not the primary problem, because the failure happens after `traci.start`, not at XML write time.
- `route_edge_count = 2` is not a failure by itself. The route is valid and short.

## Unclear Yet

- Whether SUMO would have departed the vehicles if the TraCI API call had been correct.
- Whether vehicle insert timing or route-file structure would expose a second issue after the API fix.
- Whether the current pedestrian and vehicle measurement logic is too coupled for smoke-level runs.

## Next Fix Priority

1. Replace the nonexistent TraCI call with the installed API equivalent.
2. Re-check departure/arrival counting logic after the API fix.
3. Keep `demand_all.rou.xml` generation and route validation unchanged unless a second failure appears.
4. If vehicles still do not depart after the API fix, inspect SUMO logs and insert timing next.

## Fix Suggestions Only

- Replace `traci.simulation.getDepartedVehicleIDList()` with `traci.simulation.getDepartedIDList()`.
- Replace `traci.simulation.getArrivedVehicleIDList()` with `traci.simulation.getArrivedIDList()`.
- If runtime telemetry is still sparse, store the exception message and stack trace in `run_metadata.json` and a log file.
- If vehicle insertion still fails after the API fix, move `vehicle-start-time` from `0` to `10`.

## What Not To Do

- Do not interpret this smoke run as vehicle-flow policy evidence.
- Do not change `extension_sec` away from `5.0`.
- Do not switch batch03 to generic candidates CSV.
- Do not run SUMO or TraCI again until the API mismatch is fixed.
- Do not delete result files.

