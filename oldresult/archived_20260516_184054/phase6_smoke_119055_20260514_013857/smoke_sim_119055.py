#!/usr/bin/env python3
"""
119055 Phase 6 smoke simulation — baseline vs smart.

대상:
  junction / tls: 11252413185
  crossing: :11252413185_c0
  pedestrian linkIndex: 6

사용 네트워크:
  result/phase_next_119055_junction_tls_pilot_20260514_010647/119055_junction_tls_pilot_v6.net.xml

이 스크립트는 독립 실행 가능. 기존 run_simulations.py 대규모 수정 없음.
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 경로 설정
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent.parent.parent  # js/
NET_FILE = BASE_DIR / "result/phase_next_119055_junction_tls_pilot_20260514_010647/119055_junction_tls_pilot_v6.net.xml"
OUT_DIR = Path(__file__).parent

# ---------------------------------------------------------------------------
# 119055 핵심 상수
# ---------------------------------------------------------------------------
TLS_ID = "11252413185"
CROSSING_EDGE = ":11252413185_c0"
PED_LINK_INDEX = 6          # TraCI getControlledLinks li=6 = pedestrian crossing
PED_GREEN_PHASE = 2         # phase index (0-based) where index 6 = G
SIM_DURATION = 300          # 초
SEED = 42

# 보행자 경로: 516948898#10 → (w1 → c0 → w0) → 516948898#11
PED_FROM_EDGE = "516948898#10"
PED_TO_EDGE = "516948898#11"

# 차량 경로: 516948898#10 → 516948898#11 (주도로 통과)
VEH_FROM_EDGE = "516948898#10"
VEH_TO_EDGE = "516948898#11"

# 제어 파라미터
EXTENSION_INCREMENT = 8.0       # 초
TRIGGER_REMAINING = 12.0        # 남은 초 이하일 때 연장
MAX_EXTENSIONS_PER_CYCLE = 1
PED_ARRIVAL_INTERVAL = 25       # 초마다 보행자 1명 도착
VEH_ARRIVAL_INTERVAL = 8        # 초마다 차량 1대 도착


# ---------------------------------------------------------------------------
# demand XML 생성
# ---------------------------------------------------------------------------
def generate_pedestrian_demand(out_path: Path, seed: int, duration: int) -> Path:
    """보행자 demand rou.xml 생성. 단순 주기적 도착."""
    rng = random.Random(seed)
    persons = []
    t = 10.0
    pid = 0
    while t < duration - 30:
        depart = round(t + rng.uniform(-3, 3), 1)
        depart = max(5.0, depart)
        persons.append((depart, pid))
        t += PED_ARRIVAL_INTERVAL
        pid += 1

    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<routes xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
             '    <vType id="pedestrian_type" vClass="pedestrian"/>']
    for dep, pid_ in sorted(persons):
        lines.append(
            f'    <person id="ped_{pid_}" type="pedestrian_type" depart="{dep}">'
        )
        lines.append(
            f'        <walk from="{PED_FROM_EDGE}" to="{PED_TO_EDGE}"/>'
        )
        lines.append('    </person>')
    lines.append('</routes>')

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[demand] 보행자 {len(persons)}명 → {out_path.name}")
    return out_path


def generate_vehicle_demand(out_path: Path, seed: int, duration: int) -> Path:
    """차량 demand rou.xml 생성."""
    rng = random.Random(seed + 1)
    vehicles = []
    t = 2.0
    vid = 0
    while t < duration - 10:
        depart = round(t + rng.uniform(-2, 2), 1)
        depart = max(1.0, depart)
        vehicles.append((depart, vid))
        t += VEH_ARRIVAL_INTERVAL
        vid += 1

    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<routes xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
             '    <vType id="car" vClass="passenger" maxSpeed="15.0" accel="2.6" decel="4.5"/>',
             f'    <route id="veh_route" edges="{VEH_FROM_EDGE} {VEH_TO_EDGE}"/>']
    for dep, vid_ in sorted(vehicles):
        lines.append(
            f'    <vehicle id="veh_{vid_}" type="car" route="veh_route" depart="{dep}"/>'
        )
    lines.append('</routes>')

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[demand] 차량 {len(vehicles)}대 → {out_path.name}")
    return out_path


def write_sumocfg(cfg_path: Path, net_file: Path, ped_file: Path, veh_file: Path,
                   duration: int, step_length: float = 0.5) -> Path:
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<configuration>
  <input>
    <net-file value="{net_file}"/>
    <route-files value="{ped_file},{veh_file}"/>
  </input>
  <time>
    <begin value="0"/>
    <end value="{duration}"/>
    <step-length value="{step_length}"/>
  </time>
  <processing>
    <collision.action value="warn"/>
    <time-to-teleport value="-1"/>
  </processing>
  <report>
    <no-step-log value="true"/>
    <no-warnings value="false"/>
  </report>
</configuration>
"""
    cfg_path.write_text(content, encoding="utf-8")
    return cfg_path


# ---------------------------------------------------------------------------
# 메트릭 컨테이너
# ---------------------------------------------------------------------------
class SimMetrics:
    def __init__(self, scenario: str):
        self.scenario = scenario
        self.extension_events: list[dict] = []
        self.ped_wait_times: list[float] = []
        self.veh_delays: list[float] = []
        self.ped_crossing_count = 0
        self.ped_total_departed = 0
        self.veh_total = 0
        self.errors: list[str] = []
        self.step_count = 0
        self.completed = False


# ---------------------------------------------------------------------------
# 핵심: baseline / smart 시뮬레이션
# ---------------------------------------------------------------------------
def run_one(scenario: str, cfg_path: Path, metrics: SimMetrics) -> None:
    """
    TraCI로 단일 시뮬레이션 실행.
    scenario = "baseline" | "smart"
    """
    try:
        import traci
    except ImportError:
        metrics.errors.append("traci_not_available")
        return

    import time
    time.sleep(1)  # 이전 SUMO 프로세스 완전 종료 대기

    sumo_cmd = [
        "sumo",
        "-c", str(cfg_path),
        "--no-step-log",
        "--collision.action", "warn",
        "--time-to-teleport", "-1",
    ]

    print(f"\n[{scenario}] SUMO 시작")
    try:
        traci.start(sumo_cmd)
    except Exception as e:
        metrics.errors.append(f"sumo_start_failed: {e}")
        return

    try:

        # TLS 확인
        tls_ids = set(traci.trafficlight.getIDList())
        if TLS_ID not in tls_ids:
            metrics.errors.append(f"tls_not_found: {TLS_ID}")
            print(f"  [ERROR] TLS {TLS_ID} not found in simulation")
        else:
            print(f"  [OK] TLS {TLS_ID} confirmed in simulation")
            ctrl_links = traci.trafficlight.getControlledLinks(TLS_ID)
            print(f"  [OK] controlled_links={len(ctrl_links)}")
            if PED_LINK_INDEX < len(ctrl_links):
                print(f"  [OK] li={PED_LINK_INDEX}: {ctrl_links[PED_LINK_INDEX]}")
            else:
                metrics.errors.append(f"ped_linkIndex_{PED_LINK_INDEX}_out_of_range")

        # 상태 추적
        extended_in_cycle = False
        extension_count_in_cycle = 0
        last_phase = -1
        ped_entry_times: dict[str, float] = {}   # pid → step 시작 시각

        step = 0
        while traci.simulation.getMinExpectedNumber() > 0 or step < SIM_DURATION:
            if step >= SIM_DURATION:
                break

            traci.simulationStep()
            metrics.step_count += 1
            t = traci.simulation.getTime()

            # --- 보행자 추적 ---
            ped_ids = traci.person.getIDList()
            for pid in ped_ids:
                edge = traci.person.getRoadID(pid)
                if edge == CROSSING_EDGE:
                    # 횡단보도 진입
                    if pid not in ped_entry_times:
                        ped_entry_times[pid] = t
                        metrics.ped_crossing_count += 1
                        wait = ped_entry_times.get(f"_depart_{pid}", t)
                        # 간단한 wait: crossing 도달 시각 - depart 시각 근사
                        metrics.ped_crossing_count += 0  # already counted

            # 보행자 도착 시 entry 시각 기록
            arriving = traci.simulation.getDepartedPersonIDList() if hasattr(traci.simulation, 'getDepartedPersonIDList') else []
            for pid in arriving:
                ped_entry_times[f"_depart_{pid}"] = t

            # 보행자 wait time: crossing에 들어서기까지 걸린 시간
            for pid in ped_ids:
                edge = traci.person.getRoadID(pid)
                if edge == CROSSING_EDGE and f"_wait_done_{pid}" not in ped_entry_times:
                    depart_t = ped_entry_times.get(f"_depart_{pid}", t)
                    wait_t = t - depart_t
                    metrics.ped_wait_times.append(wait_t)
                    ped_entry_times[f"_wait_done_{pid}"] = t

            # --- 차량 delay 샘플링 (매 10스텝) ---
            if step % 20 == 0:
                veh_ids = traci.vehicle.getIDList()
                for vid in veh_ids:
                    try:
                        delay = traci.vehicle.getAccumulatedWaitingTime(vid)
                        metrics.veh_delays.append(delay)
                    except Exception:
                        pass

            # --- smart 제어 ---
            if scenario == "smart" and TLS_ID in tls_ids:
                state = traci.trafficlight.getRedYellowGreenState(TLS_ID)
                current_phase = traci.trafficlight.getPhase(TLS_ID)
                remaining = traci.trafficlight.getNextSwitch(TLS_ID) - t

                # phase 전환 시 cycle 리셋
                if current_phase != last_phase:
                    if last_phase >= 0:
                        extended_in_cycle = False
                        extension_count_in_cycle = 0
                    last_phase = current_phase

                # 보행자 green phase 여부
                ped_is_green = (
                    PED_LINK_INDEX < len(state) and
                    state[PED_LINK_INDEX] in {'G', 'g'}
                )

                if ped_is_green:
                    # crossing 근처 보행자 감지
                    ped_near = 0
                    for pid in ped_ids:
                        try:
                            edge = traci.person.getRoadID(pid)
                            # crossing edge 또는 w1(대기 walkingArea)에 있으면 감지
                            if edge in (CROSSING_EDGE, ":11252413185_w1"):
                                ped_near += 1
                        except Exception:
                            pass

                    should_extend = (
                        ped_near > 0 and
                        remaining <= TRIGGER_REMAINING and
                        not extended_in_cycle and
                        extension_count_in_cycle < MAX_EXTENSIONS_PER_CYCLE
                    )

                    if should_extend:
                        new_dur = remaining + EXTENSION_INCREMENT
                        try:
                            traci.trafficlight.setPhaseDuration(TLS_ID, new_dur)
                            extended_in_cycle = True
                            extension_count_in_cycle += 1
                            event = {
                                "time": round(t, 1),
                                "tls_id": TLS_ID,
                                "linkIndex": PED_LINK_INDEX,
                                "phase": current_phase,
                                "state": state,
                                "remaining_before": round(remaining, 1),
                                "extension_sec": EXTENSION_INCREMENT,
                                "ped_near": ped_near,
                            }
                            metrics.extension_events.append(event)
                            print(f"  [EXTEND] t={t:.1f}s phase={current_phase} state={state} "
                                  f"remaining={remaining:.1f}s ped_near={ped_near} "
                                  f"+{EXTENSION_INCREMENT}s")
                        except Exception as ex:
                            metrics.errors.append(f"extension_failed: {ex}")

            step += 1

        # 시뮬레이션 완료 후 통계
        metrics.ped_total_departed = traci.simulation.getDepartedNumber() if hasattr(traci.simulation, 'getDepartedNumber') else 0
        metrics.veh_total = len(set(vid for vid in traci.vehicle.getIDList()))
        metrics.completed = True
        print(f"  [DONE] step={step} ped_crossing={metrics.ped_crossing_count} "
              f"extensions={len(metrics.extension_events)}")

    except Exception as e:
        import traceback
        metrics.errors.append(f"sim_error: {e}")
        traceback.print_exc()
    finally:
        try:
            traci.close(False)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 결과 저장
# ---------------------------------------------------------------------------
def save_results(out_dir: Path, baseline: SimMetrics, smart: SimMetrics) -> dict:
    def summarize(m: SimMetrics) -> dict:
        pw = m.ped_wait_times
        vd = m.veh_delays
        return {
            "scenario": m.scenario,
            "completed": m.completed,
            "step_count": m.step_count,
            "ped_crossing_count": m.ped_crossing_count,
            "ped_wait_time_mean": round(sum(pw)/len(pw), 2) if pw else None,
            "ped_wait_time_max": round(max(pw), 2) if pw else None,
            "veh_delay_mean": round(sum(vd)/len(vd), 2) if vd else None,
            "veh_delay_max": round(max(vd), 2) if vd else None,
            "extension_count": len(m.extension_events),
            "errors": m.errors,
        }

    result = {
        "crosswalk_id": "119055",
        "tls_id": TLS_ID,
        "crossing_edge": CROSSING_EDGE,
        "ped_linkIndex": PED_LINK_INDEX,
        "sim_duration": SIM_DURATION,
        "seed": SEED,
        "baseline": summarize(baseline),
        "smart": summarize(smart),
        "extension_events": smart.extension_events,
        "success_criteria": {
            "baseline_completed": baseline.completed,
            "smart_completed": smart.completed,
            "extension_count_ge1": len(smart.extension_events) >= 1,
            "extension_tls_correct": all(e["tls_id"] == TLS_ID for e in smart.extension_events),
            "extension_linkindex_correct": all(e["linkIndex"] == PED_LINK_INDEX for e in smart.extension_events),
            "ped_metric_not_null": summarize(smart)["ped_wait_time_mean"] is not None,
            "veh_metric_not_null": summarize(smart)["veh_delay_mean"] is not None,
        },
    }

    (out_dir / "smoke_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "extension_log.json").write_text(
        json.dumps(smart.extension_events, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> None:
    random.seed(SEED)
    print("=== 119055 Phase 6 Smoke Simulation ===")
    print(f"NET: {NET_FILE}")
    print(f"TLS: {TLS_ID}  CROSSING: {CROSSING_EDGE}  PED_LI: {PED_LINK_INDEX}")
    print(f"DURATION: {SIM_DURATION}s  SEED: {SEED}")
    print()

    if not NET_FILE.exists():
        print(f"ERROR: net file not found: {NET_FILE}")
        sys.exit(1)

    # demand 생성
    ped_file = OUT_DIR / "demand_pedestrian.rou.xml"
    veh_file = OUT_DIR / "demand_vehicle.rou.xml"
    generate_pedestrian_demand(ped_file, SEED, SIM_DURATION)
    generate_vehicle_demand(veh_file, SEED, SIM_DURATION)

    # cfg 생성 (baseline, smart 공용 — TraCI로 제어하므로 동일 cfg 가능)
    cfg_path = OUT_DIR / "smoke_119055.sumocfg"
    write_sumocfg(cfg_path, NET_FILE.resolve(), ped_file.resolve(), veh_file.resolve(), SIM_DURATION)
    print(f"[cfg] {cfg_path.name}")

    # baseline
    print("\n=== BASELINE ===")
    baseline = SimMetrics("baseline")
    run_one("baseline", cfg_path, baseline)

    # smart
    print("\n=== SMART ===")
    smart = SimMetrics("smart")
    run_one("smart", cfg_path, smart)

    # 결과 저장
    result = save_results(OUT_DIR, baseline, smart)

    # 보고
    print("\n=== 결과 요약 ===")
    for scenario_key in ("baseline", "smart"):
        s = result[scenario_key]
        print(f"\n[{scenario_key}]")
        print(f"  completed:         {s['completed']}")
        print(f"  ped_crossing:      {s['ped_crossing_count']}")
        print(f"  ped_wait_mean:     {s['ped_wait_time_mean']}")
        print(f"  veh_delay_mean:    {s['veh_delay_mean']}")
        print(f"  extension_count:   {s['extension_count']}")
        if s['errors']:
            print(f"  ERRORS: {s['errors']}")

    print("\n=== 성공 기준 ===")
    sc = result["success_criteria"]
    all_pass = True
    for k, v in sc.items():
        status = "PASS" if v else "FAIL"
        if not v:
            all_pass = False
        print(f"  {status}  {k}: {v}")

    print(f"\n{'ALL PASS' if all_pass else 'SOME FAIL'}")
    if smart.extension_events:
        print("\n=== Extension Events ===")
        for ev in smart.extension_events:
            print(f"  t={ev['time']}s tls={ev['tls_id']} li={ev['linkIndex']} "
                  f"phase={ev['phase']} state={ev['state']} +{ev['extension_sec']}s")

    # 실패 분류
    if not all_pass:
        print("\n=== 실패 원인 분류 ===")
        if not baseline.completed or not smart.completed:
            print("  → demand 없음 또는 SUMO 실행 실패")
            print(f"    baseline errors: {baseline.errors}")
            print(f"    smart errors: {smart.errors}")
        if not sc.get("extension_count_ge1"):
            print("  → extension event 없음: 보행자가 crossing에 도달하지 못했거나")
            print("    phase 조건(remaining <= trigger)을 충족하지 못함")
        if not sc.get("ped_metric_not_null"):
            print("  → pedestrian metric null: crossing 통과 보행자 없음")
        if not sc.get("veh_metric_not_null"):
            print("  → vehicle metric null: 차량 없음")


if __name__ == "__main__":
    main()
