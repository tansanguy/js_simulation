# SUMO 도로망 1단계 검증 리포트

## 요약
- 후보지 수: 1
- 합계 노드 수(추정): 129
- 합계 엣지 수: 599
- 합계 lane 수: 724
- 합계 도로 길이(m): 9176.83
- 신호 없음 후보지: 0
- 보행자 phase 없음 후보지: 0
- disconnected edge 총계: 18

## 산출물
- network_overview.csv
- disconnected_edges.csv
- crosswalk_candidate_matching.csv
- no_signal_candidates.csv
- no_pedestrian_phase_candidates.csv
- network_validation_summary.json

## 비고
- 각 cw 디렉토리마다 `network_validation_gui.sumocfg`와 `network_validation_snapshot.svg`를 생성했습니다.
- isolated component 상세는 `cw_<id>_isolated_components.json`에 저장됩니다.