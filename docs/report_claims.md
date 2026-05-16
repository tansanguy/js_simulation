# Report Claims

Use this file as wording guardrail for reports and summaries.

## Safe Claims

| Safe wording | Why it is safe |
|---|---|
| `Top50 candidates are sorted by risk_score descending, with crosswalk_id ascending as the tie-breaker.` | Current selection rule is current and checked |
| `ROAD_RANK is not used for candidate selection.` | Matches current code and contracts |
| `34 smart candidates are managed in the current 30seed/smoke lane.` | Matches current manifest state |
| `Remaining Top50 candidates are not missing because the road network is absent; they are blocked by crossing, walkingarea, TLS, or ped-link consistency issues.` | Matches current diagnosis and avoids overclaiming |
| `Pedestrian scenario is daytime_high_1p2, with observed S-DoT values first and living-population imputation for missing dongs.` | Matches current demand design |
| `final_pedestrian_600s is computed as round(base_pedestrian_600s * 1.2).` | Matches current demand formula |
| `Vehicle scenario is main_realistic_stress, at 20,877 vph and 3,480 vehicles per 600 seconds, passenger only.` | Matches current vehicle policy |
| `The 12 road-group values are policy metadata, not exact route-generation buckets.` | Matches current vehicle policy interpretation |
| `Baseline and smart share the same vehicle route, trip, and net inputs.` | Matches current validation contract |
| `The SUMO network is a Jung-gu boundary plus about 1 km buffer experimental network, not a 1:1 copy of Jung-gu roads.` | Matches current network evidence |

## Do Not Say

| Banned wording | Why not |
|---|---|
| `완성됨` | Overstates current state |
| `최종 실험 완료` | Not verified at current scope |
| `Top50 전체 실행 완료` | Not all Top50 are implemented or validated as a single completed set |
| `보행자 end-to-end 검증 완료` | Pedestrian end-to-end status is still partial |
| `중구 도로망과 완전 동일` | Network is representative, not an exact copy |
| `12개 road group이 실제 route bucket` | Road groups are metadata only |
| `모든 edge에 균일 분포` | Vehicle coverage is broad, not uniform everywhere |
| `baseline/smart가 서로 다른 vehicle route를 쓴다` | Baseline/smart must share same route, trip, and net |

## Keep Partial

| Claim | Status to keep | Why |
|---|---|---|
| Final pedestrian demand end-to-end implementation | partial | Smoke evidence exists, but not full final demand proof |
| Pedestrian runtime observation | partial | Runtime counts are observation, not implementation proof |
| Network representativeness | partial | Good enough for experiment use, not 1:1 identity |
| Smoke-level pedestrian validation | partial | Useful sanity check, not policy effect proof |
| Source provenance from smoke-run JSON alone | partial or not_checked | Metadata is helpful, but not fully source traced in the smoke artifacts |

## Example Sentences

| Usable | Avoid |
|---|---|
| `Current vehicle policy is implemented and validated on the checked smoke artifact.` | `Vehicle policy is fully validated for the final experiment.` |
| `Pedestrian demand implementation is partial; runtime smoke counts exist, but the full end-to-end demand run is not yet proved.` | `Pedestrian demand is complete.` |
| `Network representativeness is partial but acceptable for experimental use.` | `The SUMO network is identical to the real Jung-gu road network.` |
| `Baseline and smart share the same route/trip/net contract.` | `Smart and baseline can use different vehicle routes.` |
| `Road-group values are policy metadata.` | `Road groups are the actual routing buckets.` |

## Review Rule

- If a sentence would make a reader believe the pipeline is more complete than the checked artifacts support, do not use it.
- If a sentence crosses pedestrian, vehicle, network, and candidate-selection scopes, split it before writing it.
