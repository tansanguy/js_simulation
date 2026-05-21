# Phase 6 Traffic Volume Mapping Schema

Use this schema to map each crosswalk to a traffic-demand source before hourly-volume experiments.

| column | meaning |
| --- | --- |
| `crosswalk_id` | crosswalk identifier used in the runner |
| `matched_traffic_source` | source dataset or survey used for the match |
| `matched_station_id` | nearest station or counting point id |
| `road_name` | road name for the traffic source |
| `direction` | direction of the traffic flow |
| `hourly_volume_morning_peak` | morning peak hourly demand |
| `hourly_volume_midday` | midday hourly demand |
| `hourly_volume_evening_peak` | evening peak hourly demand |
| `speed_morning_peak` | morning peak speed reference |
| `speed_midday` | midday speed reference |
| `speed_evening_peak` | evening peak speed reference |
| `matching_grade` | quality grade for the match |
| `matching_note` | notes, caveats, or mismatch explanation |

## Usage rule

- Map each batch03 candidate to one traffic source before running hourly experiments.
- Keep the mapping fixed for baseline and smart.
- Do not change the mapping between paired seeds.
- Keep stress scenarios separate from this mapping layer.
