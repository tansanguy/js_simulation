"""최종 결과 패키지 생성기 — 3-CSV 관측 테이블 형식.

시뮬레이션 1회 실행 = 3개 메인 CSV + README + validity_checklist

  baseline.csv          — baseline 시나리오 관측값 테이블
  smart_crosswalk.csv   — smart 시나리오 관측값 테이블
  comparison.csv        — baseline vs smart 관측 delta (점수/판정 없음)
  README.md             — 패키지 안내
  validity_checklist.csv — 데이터 유효성 체크

핵심 원칙:
  시뮬레이션은 관측값만 제공한다.
  점수화, 판정, 정책 효과 주장은 이 파일에서 하지 않는다.

CLI:
  python3 -m smart_crosswalk_sumo.reporting.final_result_package \\
    --run_dir result/phase5c_diag_119055 \\
    --safety_report_dir result/phase5c_real_safety_report_119055 \\
    --output_dir result/final_package_phase6b_119055 \\
    [--simple_three_csv]  # 3-CSV가 기본값이므로 생략 가능
    [--charts]            # 차트 생성 (opt-in, 기본값 off)
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------

_OBSERVATION_COLS = [
    "scenario", "seed", "selected_crosswalk_count",
    "avg_vehicle_delay_sec", "avg_queue_length", "max_queue_length",
    "surrounding_road_delay_sec", "vehicle_delay_cost",
    "extension_count", "total_extension_sec",
    "network_arrived_vehicles", "network_avg_travel_time_sec", "network_avg_speed_mps",
    "network_teleported_vehicles", "network_spillback_rate", "network_spillback_step_count",
    "safety_risk_score", "accident_expected_value", "elderly_incomplete_crossings",
    "risk_event_collection_enabled", "risk_event_count", "senior_risk_event_count",
    "pedestrian_frame_count", "vehicle_frame_count",
    "risk_events_path", "risk_event_collection_error",
    "pair_diag_n_pairs_total", "pair_diag_n_within_15m", "pair_diag_n_within_30m",
    "pair_diag_min_distance_m", "pair_diag_mean_distance_m", "pair_diag_n_time_steps",
]

_SMART_SCENARIO_NAMES = ("smart_selected", "smart", "smart_crosswalk")

_TRAFFIC_COLS = {
    "avg_vehicle_delay_sec", "avg_queue_length", "max_queue_length",
    "surrounding_road_delay_sec", "vehicle_delay_cost",
    "network_avg_travel_time_sec", "network_avg_speed_mps",
    "network_arrived_vehicles", "network_teleported_vehicles",
    "network_spillback_rate", "network_spillback_step_count",
}

_COMPARISON_METRICS = [
    ("avg_vehicle_delay_sec",       "traffic_observation"),
    ("avg_queue_length",            "traffic_observation"),
    ("max_queue_length",            "traffic_observation"),
    ("surrounding_road_delay_sec",  "traffic_observation"),
    ("vehicle_delay_cost",          "traffic_observation"),
    ("network_avg_travel_time_sec", "traffic_observation"),
    ("network_avg_speed_mps",       "traffic_observation"),
    ("network_arrived_vehicles",    "traffic_observation"),
    ("network_teleported_vehicles", "traffic_observation"),
    ("network_spillback_rate",      "traffic_observation"),
    ("network_spillback_step_count","traffic_observation"),
    ("extension_count",             "smart_intervention_observation"),
    ("total_extension_sec",         "smart_intervention_observation"),
    ("safety_risk_score",           "safety_proxy_observation"),
    ("accident_expected_value",     "safety_proxy_observation"),
    ("elderly_incomplete_crossings","safety_proxy_observation"),
    ("risk_event_count",            "safety_proxy_observation"),
    ("senior_risk_event_count",     "safety_proxy_observation"),
    ("pedestrian_frame_count",      "collection_diagnostic"),
    ("vehicle_frame_count",         "collection_diagnostic"),
    ("pair_diag_n_pairs_total",     "collection_diagnostic"),
    ("pair_diag_n_within_15m",      "collection_diagnostic"),
    ("pair_diag_n_within_30m",      "collection_diagnostic"),
    ("pair_diag_min_distance_m",    "collection_diagnostic"),
    ("pair_diag_mean_distance_m",   "collection_diagnostic"),
    ("pair_diag_n_time_steps",      "collection_diagnostic"),
    ("smart_extension_eval_count",                        "smart_extension_diagnostic"),
    ("smart_extension_ped_green_count",                   "smart_extension_diagnostic"),
    ("smart_extension_remaining_trigger_window_count",    "smart_extension_diagnostic"),
    ("smart_extension_detected_peds_count",               "smart_extension_diagnostic"),
    ("smart_extension_sensor_pass_count",                 "smart_extension_diagnostic"),
    ("smart_extension_decision_true_count",               "smart_extension_diagnostic"),
    ("smart_extension_block_missing_tls_id_count",        "smart_extension_diagnostic"),
    ("smart_extension_block_missing_ped_link_indices_count", "smart_extension_diagnostic"),
    ("smart_extension_block_ped_signal_not_green_count",  "smart_extension_diagnostic"),
    ("smart_extension_block_remaining_time_sufficient_count", "smart_extension_diagnostic"),
    ("smart_extension_block_max_extension_reached_count", "smart_extension_diagnostic"),
    ("smart_extension_block_already_extended_in_cycle_count", "smart_extension_diagnostic"),
    ("smart_extension_block_no_pedestrians_detected_count", "smart_extension_diagnostic"),
    ("smart_extension_block_sensor_false_negative_count", "smart_extension_diagnostic"),
]

_SMART_EXTENSION_DIAG_COLS = [col for col, g in _COMPARISON_METRICS if g == "smart_extension_diagnostic"]

# ---------------------------------------------------------------------------
# 유틸리티
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _to_float(val: Any) -> float | None:
    if val is None:
        return None
    if isinstance(val, float) and math.isnan(val):
        return None
    try:
        return float(val)
    except Exception:
        return None


def _row_val(df: pd.DataFrame, scenario: str, col: str, default: Any = None) -> Any:
    if df.empty or col not in df.columns:
        return default
    rows = df[df["scenario"] == scenario] if "scenario" in df.columns else df
    if rows.empty:
        return default
    v = rows.iloc[0][col]
    return None if (isinstance(v, float) and math.isnan(v)) else v


def _int_val(df: pd.DataFrame, scenario: str, col: str) -> int:
    v = _row_val(df, scenario, col)
    try:
        return int(v) if v is not None else 0
    except Exception:
        return 0


def _find_smart_scenario(df: pd.DataFrame) -> str | None:
    if "scenario" not in df.columns:
        return None
    for name in _SMART_SCENARIO_NAMES:
        if (df["scenario"] == name).any():
            return name
    return None


# ---------------------------------------------------------------------------
# 파일 로더
# ---------------------------------------------------------------------------

def _load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _load_trace_events(log_path: Path) -> list[str]:
    events: list[str] = []
    if not log_path.exists():
        return events
    try:
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                d = json.loads(line)
                ev = d.get("event", "")
                if ev:
                    events.append(ev)
            except Exception:
                pass
    except Exception:
        pass
    return events


def _first_glob(parent: Path, pattern: str) -> Path | None:
    matches = sorted(parent.glob(pattern))
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# 입력 수집
# ---------------------------------------------------------------------------

def _collect_inputs(run_dir: Path, safety_report_dir: Path | None) -> dict[str, Any]:
    out_dir = run_dir / "outputs"

    seed_summary = _load_csv(out_dir / "network_simulation_summary_seed.csv")
    smart_scenario = _find_smart_scenario(seed_summary)

    trace_log = _first_glob(out_dir, "runtime_trace_seed*.log")
    trace_events = _load_trace_events(trace_log) if trace_log else []

    risk_bl = _first_glob(out_dir, "risk_events_baseline_seed*.csv")
    risk_sm = _first_glob(out_dir, "risk_events_smart_selected_seed*.csv")
    if risk_sm is None:
        risk_sm = _first_glob(out_dir, "risk_events_smart_seed*.csv")

    safety_md_text = ""
    if safety_report_dir and safety_report_dir.exists():
        md_path = safety_report_dir / "safety_benefit_report.md"
        if md_path.exists():
            safety_md_text = md_path.read_text(encoding="utf-8")

    return {
        "seed_summary": seed_summary,
        "smart_scenario": smart_scenario,
        "trace_events": trace_events,
        "risk_bl_path": risk_bl,
        "risk_sm_path": risk_sm,
        "safety_md_text": safety_md_text,
        "safety_report_dir": safety_report_dir,
    }


# ---------------------------------------------------------------------------
# 1. baseline.csv / smart_crosswalk.csv  — 관측값 테이블
# ---------------------------------------------------------------------------

def _build_scenario_df(seed_summary: pd.DataFrame, scenario: str) -> pd.DataFrame:
    if seed_summary.empty:
        return pd.DataFrame(columns=_OBSERVATION_COLS)
    if "scenario" in seed_summary.columns:
        rows = seed_summary[seed_summary["scenario"] == scenario]
    else:
        rows = seed_summary
    available = [c for c in _OBSERVATION_COLS if c in rows.columns]
    if not available:
        return pd.DataFrame(columns=_OBSERVATION_COLS)
    return rows[available].reset_index(drop=True)


# ---------------------------------------------------------------------------
# 2. comparison.csv  — 관측 delta (점수/판정 없음)
# ---------------------------------------------------------------------------

def _change_direction(b_f: float | None, s_f: float | None) -> str:
    if b_f is None or s_f is None:
        return "unavailable"
    diff = s_f - b_f
    if abs(diff) < 1e-9:
        return "unchanged"
    return "increased" if diff > 0 else "decreased"


def _delta_pct(b_f: float | None, s_f: float | None) -> float | None:
    if b_f is None or s_f is None:
        return None
    if abs(b_f) < 1e-12:
        return None
    return (s_f - b_f) / abs(b_f) * 100.0


def _caution_note(
    col: str,
    bl_risk: int, sm_risk: int,
    bl_ped: int, sm_ped: int,
    bl_veh: int, sm_veh: int,
    direction: str,
) -> str:
    if col in ("risk_event_count", "senior_risk_event_count"):
        if bl_risk == 0 and sm_risk == 0:
            return "이번 실행에서는 risk event가 탐지되지 않아 안전 편익 해석은 보류."
    elif col == "pedestrian_frame_count":
        if bl_ped == 0 or sm_ped == 0:
            return "보행자 프레임 수집이 0이므로 안전 proxy 해석 주의."
    elif col == "vehicle_frame_count":
        if bl_veh == 0 or sm_veh == 0:
            return "차량 프레임 수집이 0이므로 교통/위험 이벤트 해석 주의."
    elif col in ("safety_risk_score", "accident_expected_value"):
        if bl_risk == 0 and sm_risk == 0:
            return "risk event가 0이므로 이 값은 최종 안전 편익 근거로 사용하지 말 것."
    elif col in ("extension_count", "total_extension_sec"):
        return "스마트 신호 개입 강도 관측값. 단독으로 좋고 나쁨을 판단하지 않음."
    elif col in _TRAFFIC_COLS:
        if direction == "increased":
            return "baseline 대비 smart_crosswalk에서 값이 증가함."
        elif direction == "decreased":
            return "baseline 대비 smart_crosswalk에서 값이 감소함."
        elif direction == "unchanged":
            return "baseline 대비 smart_crosswalk에서 값의 변화가 없음."
        else:
            return "baseline 또는 smart_crosswalk 데이터 없음."
    return ""


def _build_comparison_df(seed_summary: pd.DataFrame, smart_scenario: str | None) -> pd.DataFrame:
    empty_cols = [
        "metric", "baseline_value", "smart_crosswalk_value",
        "delta", "delta_pct", "change_direction", "metric_group", "caution_note",
    ]
    if seed_summary.empty or smart_scenario is None:
        return pd.DataFrame(columns=empty_cols)

    bl_risk = _int_val(seed_summary, "baseline", "risk_event_count")
    sm_risk = _int_val(seed_summary, smart_scenario, "risk_event_count")
    bl_ped = _int_val(seed_summary, "baseline", "pedestrian_frame_count")
    sm_ped = _int_val(seed_summary, smart_scenario, "pedestrian_frame_count")
    bl_veh = _int_val(seed_summary, "baseline", "vehicle_frame_count")
    sm_veh = _int_val(seed_summary, smart_scenario, "vehicle_frame_count")

    rows = []
    for col, group in _COMPARISON_METRICS:
        b_raw = _row_val(seed_summary, "baseline", col)
        s_raw = _row_val(seed_summary, smart_scenario, col)
        b_f = _to_float(b_raw)
        s_f = _to_float(s_raw)
        delta = (s_f - b_f) if (b_f is not None and s_f is not None) else None
        dpct = _delta_pct(b_f, s_f)
        direction = _change_direction(b_f, s_f)
        note = _caution_note(
            col, bl_risk, sm_risk, bl_ped, sm_ped, bl_veh, sm_veh, direction
        )
        rows.append({
            "metric": col,
            "baseline_value": b_raw,
            "smart_crosswalk_value": s_raw,
            "delta": delta,
            "delta_pct": round(dpct, 4) if dpct is not None else None,
            "change_direction": direction,
            "metric_group": group,
            "caution_note": note,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 3. README.md
# ---------------------------------------------------------------------------

def _build_readme(output_dir: Path, risk_event_count_bl: int, risk_event_count_sm: int) -> str:
    risk_warn = ""
    if risk_event_count_bl == 0 and risk_event_count_sm == 0:
        risk_warn = (
            "\n> ⚠️ 이번 실행에서 risk_event_count = 0입니다.\n"
            "> 안전 편익 결론은 보류 상태입니다.\n"
        )

    return f"""\
# 스마트 횡단보도 시뮬레이션 결과 패키지 (3-CSV 관측 테이블)

생성일시: {_now_iso()}
패키지 위치: `{output_dir}`
{risk_warn}
---

## 이 패키지의 목적

이 폴더는 시뮬레이션 1회 실행 결과를 3개의 관측 CSV 파일로 정리한다.
점수화, 정책 효과 판단, 자동 결론은 포함하지 않는다.

---

## 파일 설명

| 파일 | 내용 |
|---|---|
| `baseline.csv` | baseline 시나리오 관측값 테이블 |
| `smart_crosswalk.csv` | smart_crosswalk 시나리오 관측값 테이블 |
| `comparison.csv` | baseline vs smart 관측 delta (점수/판정 없음) |
| `validity_checklist.csv` | 데이터 유효성 체크 (pass/warn/fail) |
| `README.md` | 이 파일 |

---

## 관측값 해석 주의사항

- `baseline.csv`와 `smart_crosswalk.csv`는 시뮬레이션에서 직접 측정된 관측값이다.
- `comparison.csv`는 baseline 대비 smart 시나리오의 delta 관측값이다.
  정책 효과 점수가 아니며, delta는 관측 차이이고 인과관계가 아니다.
- risk event는 SUMO 시뮬레이션에서 차량-보행자 근접 상황을 탐지한 proxy 지표이다.
  실제 사고 건수가 아니므로 절대 수치로 사고 감소를 주장할 수 없다.
- risk_event_count = 0이면 안전 편익 결론은 보류 상태이다.
  SUMO는 횡단보도에서 차량을 올바르게 감속/정지시키므로
  이동 중 근접 이벤트가 발생하지 않을 수 있다.
- 시뮬레이션 출력은 관측 지표이며 자동 평가 결과가 아니다.
  해석은 별도 분석 단계에서 수행해야 한다.

---

## 현재 주장 가능한 것

- 두 시나리오(baseline, smart_crosswalk)의 시뮬레이션이 완료되었다.
- 교통 비용 지표(차량 지연, 대기열, 통행시간 등)가 시나리오별로 측정되었다.
- 보행자·차량 프레임 수집 현황이 기록되었다.
- 파이프라인(수집 → 탐지 → 보고서)이 end-to-end로 작동하였다.

## 아직 주장하면 안 되는 것

- **"스마트 횡단보도가 사고를 줄였다"** — risk event가 없으면 안전 편익 근거 없음.
- **TAAS 기반 severity 개선** — risk event 없이 cutoff 적용 불가.
- **comparison.csv의 delta를 정책 효과로 해석** — delta는 관측 차이이며 인과관계가 아님.
"""


# ---------------------------------------------------------------------------
# 3-b. 결과_읽는법.md  — 한국어 안내 (비개발자용)
# ---------------------------------------------------------------------------

def _build_korean_guide(output_dir: Path, has_diag_counters: bool) -> str:
    diag_section = ""
    if has_diag_counters:
        diag_section = """
### smart_extension_* 진단 카운터 (comparison.csv 하단)

이번 실행부터 smart_extension_diagnostic 그룹의 카운터가 추가되었습니다.
이 숫자들은 **extension_count=0의 원인을 찾기 위한 런타임 진단 지표**입니다.

| 지표 | 의미 |
|---|---|
| `smart_extension_eval_count` | 보행 신호 green 상태에서 스마트 확장 로직이 실행된 횟수 |
| `smart_extension_remaining_trigger_window_count` | 잔여 시간이 트리거 윈도우 이내로 진입한 횟수 |
| `smart_extension_detected_peds_count` | 보행자가 실제로 감지된 횟수 |
| `smart_extension_decision_true_count` | 신호 연장이 실행된 횟수 (= extension_count와 일치해야 함) |
| `smart_extension_block_*_count` | 각 조건에서 차단된 횟수 |

`eval_count > 0` 이지만 `remaining_trigger_window_count = 0`이면:
→ ped 신호 phase가 트리거 윈도우보다 짧아서 개입 자체가 불가능함.

`remaining_trigger_window_count > 0` 이지만 `detected_peds_count = 0`이면:
→ 해당 시간대에 보행자가 해당 교차로에 없었음.

"""
    return f"""\
# 결과 읽는 법

생성일시: {_now_iso()}
패키지 위치: `{output_dir}`

---

## 이 결과 패키지는 무엇인가

스마트 횡단보도 신호 연장 시스템의 SUMO 시뮬레이션 결과물입니다.
`baseline`(기존 신호 유지)과 `smart_crosswalk`(보행자 감지 시 신호 연장) 두 시나리오를
동일한 교통 수요와 교차로 조건 하에서 실행하고 관측값을 비교합니다.

---

## csv/ 폴더의 세 파일

### csv/baseline.csv

baseline 시나리오의 seed별 시뮬레이션 관측값 테이블입니다.
스마트 신호 연장 없이 기존 신호 체계를 그대로 유지한 경우의 결과입니다.

### csv/smart_crosswalk.csv

smart_crosswalk(또는 smart_selected) 시나리오의 관측값 테이블입니다.
보행자가 감지되고 잔여 신호 시간이 트리거 윈도우 이내일 때 신호를 연장한 경우입니다.

### csv/comparison.csv

baseline 대비 smart_crosswalk의 관측 delta 테이블입니다.
`delta = smart_crosswalk_value - baseline_value`이며, **인과관계가 아니라 관측 차이**입니다.

---

## 어떤 지표를 먼저 보면 되는가

**안전 편익 확인:**
- `risk_event_count` — 차량-보행자 근접 위험 proxy 이벤트 수
- `senior_risk_event_count` — 고령자 관련 위험 이벤트 수

**교통 비용 확인:**
- `avg_vehicle_delay_sec` — 차량 평균 지연 시간(초)
- `avg_queue_length` — 평균 대기열 길이
- `max_queue_length` — 최대 대기열 길이
- `vehicle_delay_cost` — 차량 지연 비용(원 환산)

**스마트 신호 개입 확인:**
- `extension_count` — 실제 신호 연장이 발생한 횟수
- `total_extension_sec` — 총 연장 시간(초)

---

## risk_event_count는 실제 사고 건수가 아닙니다

`risk_event_count`는 SUMO 시뮬레이션에서 차량과 보행자의 근접 거리·속도를 기준으로
탐지한 **위험 proxy 지표**입니다. 실제 사고 건수가 아니므로
"risk_event가 줄었다 = 사고가 줄었다"는 주장은 불가합니다.

---

## extension_count=0이면 어떻게 해석해야 하는가

`extension_count=0`은 **"스마트 신호 연장 효과가 없다"는 결론이 아닙니다.**
이번 실행에서 스마트 신호 연장 개입이 한 번도 발생하지 않았다는 의미입니다.

원인 후보:
- 보행 신호 phase 지속 시간이 트리거 윈도우보다 짧아 개입 불가
- 시뮬레이션 시간 중 해당 교차로에 보행자 수요가 없었음
- registry 매핑 문제(ped_link_indices 불일치 등)

`smart_extension_*` 진단 카운터를 통해 어느 단계에서 막혔는지 확인하십시오.

---

## baseline과 smart의 delta=0이면 어떻게 해석해야 하는가

delta=0은 **"스마트 횡단보도 효과 없음 입증"이 아닙니다.**
이번 실행에서 스마트 신호 연장 개입이 발생하지 않아 두 시나리오가 동일하게 작동했다는 의미입니다.
extension_count=0이 선행 원인입니다.
{diag_section}
---

## 현재 119055 결과의 위치

crosswalk 119055 결과는 **효과 입증 완료가 아니라 실행 정상화 및 런타임 진단 단계의 결과**입니다.
registry v5 검증 기준으로 매핑은 완전히 유효하며,
이제 runtime에서 스마트 신호 연장이 왜 발동되지 않는지 진단하는 단계입니다.

---

*이 문서는 시뮬레이션 파이프라인이 자동 생성합니다.*
"""


# ---------------------------------------------------------------------------
# 4. validity_checklist.csv
# ---------------------------------------------------------------------------

def _build_checklist(
    inp: dict[str, Any],
    run_dir: Path,
    safety_report_dir: Path | None,
    baseline_df: pd.DataFrame,
    smart_df: pd.DataFrame,
) -> pd.DataFrame:
    seed_df = inp["seed_summary"]
    trace_events = inp["trace_events"]
    smart_scenario = inp["smart_scenario"]

    bl_ped  = _int_val(seed_df, "baseline", "pedestrian_frame_count")
    bl_veh  = _int_val(seed_df, "baseline", "vehicle_frame_count")
    bl_risk = _int_val(seed_df, "baseline", "risk_event_count")
    sm_risk = _int_val(seed_df, smart_scenario or "smart_selected", "risk_event_count")

    n_se = trace_events.count("scenario_end")
    baseline_done = n_se >= 1
    smart_done    = n_se >= 2
    exit_found    = "collect_integrated_metrics_exit" in trace_events

    risk_bl_exists = inp["risk_bl_path"] is not None and inp["risk_bl_path"].exists()
    risk_sm_exists = inp["risk_sm_path"] is not None and inp["risk_sm_path"].exists()
    safety_exists  = bool(inp["safety_md_text"])

    bl_row_exists = not baseline_df.empty
    sm_row_exists = not smart_df.empty
    any_risk = (bl_risk + sm_risk) > 0

    ready = (
        baseline_done and smart_done and exit_found
        and bl_row_exists and sm_row_exists
        and risk_bl_exists and risk_sm_exists
        and bl_ped > 0 and bl_veh > 0
        and any_risk
        and safety_exists
    )

    blocked: list[str] = []
    if not bl_row_exists:  blocked.append("baseline.csv 행 없음")
    if not sm_row_exists:  blocked.append("smart_crosswalk.csv 행 없음")
    if not baseline_done:  blocked.append("baseline scenario_end 없음")
    if not smart_done:     blocked.append("smart scenario_end 없음")
    if not risk_bl_exists: blocked.append("risk_events_baseline 파일 없음")
    if not risk_sm_exists: blocked.append("risk_events_smart 파일 없음")
    if bl_ped == 0:        blocked.append("pedestrian_frame=0")
    if bl_veh == 0:        blocked.append("vehicle_frame=0")
    if not any_risk:       blocked.append("risk_event=0")
    if not safety_exists:  blocked.append("safety report 없음")

    rows = [
        ("baseline row exists",
         "pass" if bl_row_exists else "fail",
         f"baseline.csv rows: {len(baseline_df)}",
         "baseline.csv에 데이터 행이 있어야 함"),
        ("smart_crosswalk row exists",
         "pass" if sm_row_exists else "fail",
         f"smart_crosswalk.csv rows: {len(smart_df)}",
         "smart_crosswalk.csv에 데이터 행이 있어야 함"),
        ("baseline scenario_end found",
         "pass" if baseline_done else "fail",
         f"trace: {n_se}개 scenario_end",
         "baseline 시뮬레이션 정상 종료"),
        ("smart_selected scenario_end found",
         "pass" if smart_done else "fail",
         f"trace: {n_se}개 scenario_end (2 이상 필요)",
         "smart 시뮬레이션 정상 종료"),
        ("collect_integrated_metrics_exit found",
         "pass" if exit_found else "warn",
         f"{'found' if exit_found else 'not found'} in trace",
         "전체 수집 프로세스 정상 종료"),
        ("risk_events_baseline file exists",
         "pass" if risk_bl_exists else "warn",
         str(inp["risk_bl_path"]) if risk_bl_exists else "not found",
         "baseline risk events CSV 파일"),
        ("risk_events_smart_selected file exists",
         "pass" if risk_sm_exists else "warn",
         str(inp["risk_sm_path"]) if risk_sm_exists else "not found",
         "smart risk events CSV 파일"),
        ("pedestrian_frame_count > 0",
         "pass" if bl_ped > 0 else "warn",
         f"baseline pedestrian_frame_count={bl_ped}",
         "0이면 보행자 위치 수집 불가 → 안전 proxy 해석 불가"),
        ("vehicle_frame_count > 0",
         "pass" if bl_veh > 0 else "warn",
         f"baseline vehicle_frame_count={bl_veh}",
         "0이면 차량 위치 수집 불가"),
        ("risk_event_count > 0",
         "pass" if any_risk else "warn",
         f"baseline={bl_risk}, smart={sm_risk}",
         "0이면 안전 편익 정량 결론 보류"),
        ("safety report exists",
         "pass" if safety_exists else "warn",
         str(safety_report_dir) if safety_exists else "not found",
         "safety_benefit_report.md 존재 여부"),
        ("ready_for_final_policy_claim",
         "pass" if ready else "warn",
         "all conditions met" if ready else f"blocked by: {', '.join(blocked)}",
         "모든 조건 충족 시에만 정책 효과 결론 가능. 이것은 유효성 게이트이며 점수가 아님."),
    ]
    return pd.DataFrame(rows, columns=["item", "status", "evidence", "note"])


# ---------------------------------------------------------------------------
# 차트 (opt-in — --charts 플래그 필요)
# ---------------------------------------------------------------------------

def _try_make_charts(seed_summary: pd.DataFrame, output_dir: Path) -> list[str]:
    if seed_summary.empty:
        return ["charts_skipped: no summary data"]
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return ["charts_skipped: matplotlib not available"]

    generated: list[str] = []
    smart_sc = _find_smart_scenario(seed_summary)
    scenarios = ["baseline"] + ([smart_sc] if smart_sc else [])

    try:
        metrics_traffic = [
            ("avg_vehicle_delay_sec", "avg_vehicle_delay_sec"),
            ("vehicle_delay_cost",    "vehicle_delay_cost"),
            ("avg_queue_length",      "avg_queue_length"),
            ("network_avg_travel_time_sec", "network_avg_travel_time_sec"),
        ]
        available = [(col, lbl) for col, lbl in metrics_traffic if col in seed_summary.columns]
        if available:
            vals = {
                col: [_row_val(seed_summary, sc, col) or 0.0 for sc in scenarios]
                for col, _ in available
            }
            fig, axes = plt.subplots(1, len(available), figsize=(4 * len(available), 4))
            if len(available) == 1:
                axes = [axes]
            for ax, (col, lbl) in zip(axes, available):
                ax.bar(scenarios, vals[col], color=["#5577AA", "#AA5555"][: len(scenarios)])
                ax.set_title(lbl, fontsize=8)
            fig.tight_layout()
            chart_path = output_dir / "traffic_cost_comparison.png"
            fig.savefig(chart_path, dpi=120, bbox_inches="tight")
            plt.close(fig)
            generated.append(str(chart_path))
    except Exception as exc:
        generated.append(f"traffic_chart_error: {exc}")

    return generated


# ---------------------------------------------------------------------------
# 메인 빌더
# ---------------------------------------------------------------------------

def build_final_package(
    run_dir: Path,
    safety_report_dir: Path | None,
    output_dir: Path,
    make_charts: bool = False,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_dir = output_dir / "csv"
    csv_dir.mkdir(parents=True, exist_ok=True)
    internal_dir = output_dir / "internal"
    internal_dir.mkdir(parents=True, exist_ok=True)

    inp = _collect_inputs(run_dir, safety_report_dir)
    seed_summary  = inp["seed_summary"]
    smart_scenario = inp["smart_scenario"]

    # 1. csv/baseline.csv
    bl_df  = _build_scenario_df(seed_summary, "baseline")
    bl_path = csv_dir / "baseline.csv"
    bl_df.to_csv(bl_path, index=False, encoding="utf-8-sig")

    # 2. csv/smart_crosswalk.csv
    sm_df  = _build_scenario_df(seed_summary, smart_scenario or "smart_selected")
    sm_path = csv_dir / "smart_crosswalk.csv"
    sm_df.to_csv(sm_path, index=False, encoding="utf-8-sig")

    # 3. csv/comparison.csv
    cmp_df  = _build_comparison_df(seed_summary, smart_scenario)
    cmp_path = csv_dir / "comparison.csv"
    cmp_df.to_csv(cmp_path, index=False, encoding="utf-8-sig")

    # 4. 결과_읽는법.md  (한국어 안내)
    has_diag = not seed_summary.empty and any(
        c in seed_summary.columns for c in _SMART_EXTENSION_DIAG_COLS
    )
    guide_path = output_dir / "결과_읽는법.md"
    guide_path.write_text(_build_korean_guide(output_dir, has_diag), encoding="utf-8")

    # 5. README.md  (짧은 인덱스)
    bl_risk = _int_val(seed_summary, "baseline", "risk_event_count")
    sm_risk = _int_val(seed_summary, smart_scenario or "smart_selected", "risk_event_count")
    readme_path = output_dir / "README.md"
    readme_path.write_text(_build_readme(output_dir, bl_risk, sm_risk), encoding="utf-8")

    # 6. internal/validity_checklist.csv
    checklist_df  = _build_checklist(inp, run_dir, safety_report_dir, bl_df, sm_df)
    checklist_path = internal_dir / "validity_checklist.csv"
    checklist_df.to_csv(checklist_path, index=False, encoding="utf-8-sig")

    result: dict[str, Path] = {
        "baseline":          bl_path,
        "smart_crosswalk":   sm_path,
        "comparison":        cmp_path,
        "guide":             guide_path,
        "README":            readme_path,
        "validity_checklist": checklist_path,
    }

    if make_charts:
        for i, cr in enumerate(_try_make_charts(seed_summary, output_dir)):
            result[f"chart_{i}"] = Path(cr)

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="최종 결과 패키지 생성기 — 3-CSV 관측 테이블 형식 (no-SUMO)"
    )
    parser.add_argument("--run_dir", required=True, help="시뮬레이션 결과 디렉터리")
    parser.add_argument("--safety_report_dir", default=None,
                        help="safety report 디렉터리 (optional)")
    parser.add_argument("--output_dir", required=True, help="패키지 출력 디렉터리")
    parser.add_argument("--simple_three_csv", action="store_true",
                        help="3-CSV 형식 사용 (기본값이므로 생략 가능)")
    parser.add_argument("--charts", action="store_true",
                        help="차트 생성 opt-in (기본값: off, 차트에서 한글 깨짐 주의)")
    args = parser.parse_args()

    paths = build_final_package(
        run_dir=Path(args.run_dir),
        safety_report_dir=Path(args.safety_report_dir) if args.safety_report_dir else None,
        output_dir=Path(args.output_dir),
        make_charts=args.charts,
    )
    for key, p in paths.items():
        print(f"{key}: {p}")


if __name__ == "__main__":
    main()
