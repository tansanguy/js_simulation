# Network Provenance And Representativeness Check

Scope: file inspection only. No SUMO rerun. No code edits. No manifest regeneration.

## 1. Executive Summary

The current Jung-gu network is usable as a buffer-expanded experimental network, not as a cadastral 1:1 copy of Jung-gu roads.

The strongest signals are:
- `result/vehicle_policy_smoke_1seed/run_metadata.json` records `network_mode=expanded`, `buffer_m=1000.0`, and `admin_polygon_path=smart_crosswalk_sumo/data/junggu_admin_boundary.geojson`.
- `smart_crosswalk_sumo/data/junggu_admin_boundary.geojson` bbox is `126.9615402,37.5438572,127.0267821,37.5719426`.
- `result/vehicle_policy_smoke_1seed/sumo_nets/cw_NODE_5837/metadata.json` records network bbox `126.9502084428188,37.534874088250085,127.0381138571812,37.58092571174991`, which is the Jung-gu bbox plus about a 1 km buffer on each side.
- Direct XML scan of `result/vehicle_policy_smoke_1seed/sumo_nets/cw_NODE_5837/network.net.xml` over vehicle-usable edges found `25,048` driveable edges, `20,841` junctions, `23` connected components, largest component `24,886` edges, and `0` isolated components.
- Route coverage output reports `24,990` driveable edges, `18,360` unique route edges, `0` disconnected routes, `coverage_ratio=0.734694`, `grid_coverage_ratio=1.0`, and `candidate_buffer_vehicle_share=0.000109`.

Bottom line: representativeness is good enough for Jung-gu-based experiments, but claims must stay at "Jung-gu context network with buffer expansion" and not "exact Jung-gu road duplicate".

## 2. What “Same As Jung-gu Road Network” Can And Cannot Mean

Can mean:
- Network covers Jung-gu core streets and nearby context roads.
- Major roads and connector corridors exist in the SUMO net.
- Candidate points and vehicle routes sit inside the intended network window.

Cannot mean:
- Every physical lane, curb, and signal is preserved exactly.
- The net file is clipped strictly to the admin polygon.
- Every candidate has a recorded per-edge distance proof in the requested artifacts.
- Source OSM / boundary provenance is fully reconstructible from the smoke-run JSON alone.

## 3. Network Provenance

Evidence:
- `result/vehicle_policy_smoke_1seed/run_metadata.json` has `network_mode=expanded`, `buffer_m=1000.0`, and `admin_polygon_path=smart_crosswalk_sumo/data/junggu_admin_boundary.geojson`.
- `result/vehicle_policy_smoke_1seed/sumo_nets/cw_NODE_5837/network_build_provenance.json` has `network_bbox`, netconvert fingerprints, and version info, but no explicit source OSM path or boundary file path.
- `result/vehicle_policy_smoke_1seed/sumo_nets/cw_NODE_5837/network.net.xml` header has SUMO `location`, `convBoundary`, `origBoundary`, and `projParameter`, but not the admin boundary filename.
- Canonical reference net `smart_crosswalk_sumo/sumo_nets/cw_23040/network.net.xml` does show direct `osm-files = .../map.osm` provenance in its header, so the repo does contain a fully traced canonical net, but the smoke-run target net does not expose the same full chain in the requested artifacts.
- `result/active/nets/*.net.xml` are distinct files, not copies: `current_main_12.net.xml`, `signal_fix_9.net.xml`, `generated_signal_7.net.xml`, and `p1_p4_recovery_6.net.xml` all have different SHA-256 hashes and are mapped to separate `net_group` / `run_group` entries in `result/active/csv/net_group_summary.csv` and `result/active/real_30seed_runs/manifests/candidate_metadata.csv`.

Status: partial. The generation window is clear; the full source chain is not fully embedded in the smoke-run provenance JSON.

## 4. Boundary / BBox Check

Evidence:
- Boundary bbox: `126.9615402,37.5438572,127.0267821,37.5719426`.
- Network bbox from `result/vehicle_policy_smoke_1seed/sumo_nets/cw_NODE_5837/metadata.json`: `126.9502084428188,37.534874088250085,127.0381138571812,37.58092571174991`.
- That bbox expansion is consistent with a 1000 m buffer around Jung-gu.
- Direct XML scan of vehicle-usable edges found `24,520 / 25,048` edges with both endpoints inside the 1000 m buffer, and `528 / 25,048` edges that touch or extend beyond the buffer edge.
- Direct XML scan found `6,882 / 25,048` vehicle-usable edges fully inside the admin polygon and `18,021 / 25,048` with no endpoint inside the admin polygon.
- Top50 candidate bbox from `result/active/pedestrian_assumption/top50_mapped_candidates_daytime_high_1p2.csv` is `126.9670777439272,37.55370168943821,127.01163269719552,37.56806411952721`, which sits comfortably inside the network bbox.
- One top50 point, `NODE_10381`, falls `0.34 m` outside the boundary polygon by point-to-boundary distance, so that is boundary precision noise, not a network miss.

Status: partial. The intended buffer window is correct; the XML contains a small spillover tail beyond the strict 1000 m envelope.

## 5. Driveable Edge And Connectivity Check

Evidence:
- Direct XML scan over vehicle-usable edges: `25,048` driveable edges, `20,841` junctions, `23` connected components, largest component `24,886` edges, largest component ratio `0.993532`, `0` isolated components.
- Route coverage artifact: `vehicle_edge_coverage_summary.csv` reports `24,990` driveable edges, `18,360` unique route edges, `13,239` shared edges, `0` disconnected routes, and `route_bbox_area_ratio=0.8513845796987747`.
- `vehicle_global_coverage_validation.csv` reports `coverage_ratio=0.734694`, `grid_coverage_ratio=1.0`, `used_vehicle_edges=3226`, `unique_depart_edges=3236`, `unique_arrival_edges=3226`, and `candidate_buffer_vehicle_share=0.000109`.

Interpretation:
- Vehicle routes are not trapped in one tiny pocket of Jung-gu.
- Connectivity is strong.
- The road graph is fragmented into a few components, but the main component dominates.

Status: pass.

## 6. Major Road Plausibility Check

Evidence from `result/vehicle_policy_smoke_1seed/sumo_nets/cw_NODE_5837/network.net.xml` name hits:
- `퇴계로`: 3104
- `을지로`: 1193
- `세종대로`: 516
- `동호로`: 1856
- `장충단로`: 582
- `칠패로`: 50
- `서소문로`: 312

Interpretation:
- The target SUMO network contains the expected Jung-gu arterial names.
- Exact 12 road-group to edge joins are still not proven from the requested artifacts alone.

Status: pass.

## 7. Candidate-To-Network Consistency

Evidence:
- `result/active/pedestrian_assumption/top50_mapped_candidates_daytime_high_1p2.csv` has 50 rows.
- Status split: `FINAL_READY_FOR_30SEED=12`, `NOT_READY=20`, `LOCATION_OK_BUT_SMART_RESULT_FAIL=9`, `SIGNAL_OK_LOCATION_REVIEW=9`.
- Top50 bbox is inside the network bbox.
- `result/active/real_30seed_runs/manifests/candidate_metadata.csv` has 34 rows, all `is_smart_target=True`, with groups `current_main_12=12`, `signal_fix_9=9`, `generated_signal_7=7`, `p1_p4_recovery_6=6`.
- The manifest provides mapping fields like `nearest_junction_id`, `tls_id_used`, `crossing_id`, `crossing_edge_id`, `ped_link_index`, and `ped_link_indices`, but no explicit per-candidate distance column.
- The top50 sheet provides boolean mapping evidence such as `crossing_present`, `walkingarea_present`, `tlLogic_present`, `location_verified`, `signal_logic_verified`, `smart_smoke_success`, and `baseline_smoke_success`.
- The only candidate outside the Jung-gu polygon is `NODE_10381`, and it is outside by only `0.34 m` while still marked `FINAL_READY_FOR_30SEED`, `location_verified=True`, and `smart_smoke_success=True`.
- `NOT_READY` rows are mostly explained by pipeline readiness text such as `not present in ready5 or mixed redesign promotion inputs` or `location review needed before smoke`, not by network coverage collapse.

Status: partial. Candidate placement is broadly consistent with the network, but the requested artifacts do not contain a full nearest-edge distance audit for every candidate.

## 8. Baseline / Smart Same Network Check

Evidence:
- `smart_crosswalk_sumo/sumo_nets/cw_23040/baseline_seed42.sumocfg` points to `/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/sumo_nets/cw_23040/network.net.xml`.
- `smart_crosswalk_sumo/sumo_nets/cw_23040/smart_seed42.sumocfg` points to the same net file.
- `result/vehicle_policy_smoke_1seed/outputs/vehicle_global_coverage_validation.csv` records `baseline_smart_same_route_file=true`, `baseline_smart_same_trip_file=true`, and `baseline_smart_same_net_file=true`.

Status: pass.

## 9. What Can Be Claimed In The Report

- This is a Jung-gu-based, buffer-expanded SUMO context network.
- It contains the expected arterial corridors and a large connected vehicle graph.
- The route set is broadly distributed across the network, not locked to a tiny candidate neighborhood.
- Baseline and smart smoke runs are on the same net file for the canonical seed42 pair.

## 10. What Must Not Be Claimed

- Do not say the SUMO net is an exact clone of Jung-gu roads.
- Do not say provenance is fully traceable from the smoke-run JSON alone.
- Do not say every candidate has a recorded nearest-edge distance under 50 m in the requested artifacts.
- Do not say exact road-group-to-edge assignment is proven unless a dedicated road-group mapping file is added.
- Do not say the network is strictly clipped to the admin polygon; the file includes buffer spillover.

## 11. Final Pass / Partial / Fail Table

| check_item | expected_meaning | actual_evidence | status | evidence_file | report_sentence |
|---|---|---|---|---|---|
| network provenance | Source/boundary/buffer chain should be visible | `run_metadata.json` has `network_mode=expanded`, `buffer_m=1000.0`, `admin_polygon_path=smart_crosswalk_sumo/data/junggu_admin_boundary.geojson`; `network_build_provenance.json` has bbox/fingerprint/version but no source OSM path | partial | `result/vehicle_policy_smoke_1seed/run_metadata.json`, `result/vehicle_policy_smoke_1seed/sumo_nets/cw_NODE_5837/network_build_provenance.json`, `result/vehicle_policy_smoke_1seed/sumo_nets/cw_NODE_5837/network.net.xml` | Provenance is clear at metadata level, but not fully source-traced in the smoke-run artifacts. |
| active nets inventory | Active net files should be inspected and mapped | Four distinct net files exist and have distinct SHA-256 hashes; `net_group_summary.csv` maps them to separate groups | pass | `result/active/nets/*.net.xml`, `result/active/csv/net_group_summary.csv`, `result/active/real_30seed_runs/manifests/candidate_metadata.csv` | Active nets are separate Jung-gu network variants, not duplicates. |
| boundary / bbox check | Network bbox should cover Jung-gu plus intended buffer | Boundary bbox vs network bbox matches about 1 km expansion each side; 24,520/25,048 vehicle-usable edges are fully inside the 1000 m buffer; 528 touch or exceed it | partial | `smart_crosswalk_sumo/data/junggu_admin_boundary.geojson`, `result/vehicle_policy_smoke_1seed/sumo_nets/cw_NODE_5837/metadata.json`, `result/vehicle_policy_smoke_1seed/sumo_nets/cw_NODE_5837/network.net.xml` | Network is buffer-expanded and mostly inside the intended window, with a small spillover tail. |
| driveable edge and connectivity | Main vehicle graph should be large and connected | 25,048 vehicle-usable edges, 20,841 junctions, 23 components, largest component 24,886 edges, 0 isolated components; route coverage has 0 disconnected routes | pass | `result/vehicle_policy_smoke_1seed/sumo_nets/cw_NODE_5837/network.net.xml`, `result/vehicle_policy_smoke_1seed/outputs/vehicle_edge_coverage_summary.csv`, `result/vehicle_policy_smoke_1seed/outputs/vehicle_global_coverage_validation.csv` | The network is strongly connected enough for experiment use. |
| major road plausibility | Expected Jung-gu arterials should exist | `퇴계로`, `을지로`, `세종대로`, `동호로`, `장충단로`, `칠패로`, `서소문로` all appear in the target net file | pass | `result/vehicle_policy_smoke_1seed/sumo_nets/cw_NODE_5837/network.net.xml` | The network contains the major Jung-gu corridors expected for a realistic experiment. |
| candidate consistency | Top50 candidates should sit on the network and have usable mappings | Top50 bbox is inside network bbox; 49/50 are inside the polygon and the 1 outlier is only 0.34 m outside; manifest has mapping fields but no direct distance column | partial | `result/active/pedestrian_assumption/top50_mapped_candidates_daytime_high_1p2.csv`, `result/active/real_30seed_runs/manifests/candidate_metadata.csv` | Candidate placement is broadly consistent with the network, but no full nearest-edge distance audit is present. |
| baseline/smart same network | Baseline and smart seed42 configs should share the same net file | Both sumocfg files point to the same net file; validation CSV records `baseline_smart_same_net_file=true` | pass | `smart_crosswalk_sumo/sumo_nets/cw_23040/baseline_seed42.sumocfg`, `smart_crosswalk_sumo/sumo_nets/cw_23040/smart_seed42.sumocfg`, `result/vehicle_policy_smoke_1seed/outputs/vehicle_global_coverage_validation.csv` | Baseline and smart smoke runs are on the same network. |

