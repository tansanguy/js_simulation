"""no-SUMO 스마트 신호 확장 트리거 진단.

run_simulations.py 런타임이 실제로 사용하는 evaluate_smart_extension_decision()을
동일하게 import하여 합성 입력으로 검증한다.
SUMO, netconvert, 시뮬레이션 결과 CSV를 일절 건드리지 않는다.

사용법:
    python3 -m smart_crosswalk_sumo.safety.smart_extension_diagnostic synthetic-trigger
"""
from __future__ import annotations

import argparse
import sys

try:
    from ..smart_extension_logic import evaluate_smart_extension_decision
except ImportError:
    from smart_extension_logic import evaluate_smart_extension_decision

# run_simulations.py SIGNAL_PARAMS 기본값과 동일
_DEFAULT_SIGNAL_PARAMS = {
    "cycle_time": 120.0,
    "yellow_time": 4.0,
    "all_red_time": 3.0,
    "ped_entry_time": 7.0,
    "extension_increment": 5.0,
    "max_extensions": 1,
    "trigger_remaining": 10.0,
    "clearance_time": 2.0,
    "sensor_fn_rate": 0.05,
}

# tls_state 예시: 5글자, 인덱스 2가 보행자 신호
_TLS_STATE_PED_GREEN = "rrGrr"   # index 2 = G (보행자 녹색)
_TLS_STATE_PED_RED   = "rrrrr"   # index 2 = r (보행자 비녹색)

_CASES = [
    {
        "name": "A_should_trigger",
        "desc": "모든 조건 충족 → 확장 트리거",
        "kwargs": dict(
            tls_id="J5",
            ped_link_indices=[2],
            tls_state=_TLS_STATE_PED_GREEN,
            remaining_s=1.0,
            pedestrians_detected=True,
            extension_count_in_cycle=0,
            extended_in_cycle=False,
            signal_params=_DEFAULT_SIGNAL_PARAMS,
            sensor_pass=True,
        ),
        "expected_should_extend": True,
        "expected_reason": "all_conditions_met",
    },
    {
        "name": "B_ped_signal_not_green",
        "desc": "보행자 신호 비녹색 → 비트리거",
        "kwargs": dict(
            tls_id="J5",
            ped_link_indices=[2],
            tls_state=_TLS_STATE_PED_RED,
            remaining_s=1.0,
            pedestrians_detected=True,
            extension_count_in_cycle=0,
            extended_in_cycle=False,
            signal_params=_DEFAULT_SIGNAL_PARAMS,
            sensor_pass=True,
        ),
        "expected_should_extend": False,
        "expected_reason": "ped_signal_not_green",
    },
    {
        "name": "C_remaining_time_sufficient",
        "desc": "잔여 시간 충분 (15s > trigger 10s) → 비트리거",
        "kwargs": dict(
            tls_id="J5",
            ped_link_indices=[2],
            tls_state=_TLS_STATE_PED_GREEN,
            remaining_s=15.0,
            pedestrians_detected=True,
            extension_count_in_cycle=0,
            extended_in_cycle=False,
            signal_params=_DEFAULT_SIGNAL_PARAMS,
            sensor_pass=True,
        ),
        "expected_should_extend": False,
        "expected_reason": "remaining_time_sufficient",
    },
    {
        "name": "D_missing_ped_link_indices",
        "desc": "ped_link_indices 비어있음 → 비트리거",
        "kwargs": dict(
            tls_id="J5",
            ped_link_indices=[],
            tls_state=_TLS_STATE_PED_GREEN,
            remaining_s=1.0,
            pedestrians_detected=True,
            extension_count_in_cycle=0,
            extended_in_cycle=False,
            signal_params=_DEFAULT_SIGNAL_PARAMS,
            sensor_pass=True,
        ),
        "expected_should_extend": False,
        "expected_reason": "missing_ped_link_indices",
    },
    {
        "name": "E_max_extension_reached",
        "desc": "사이클 내 최대 확장 횟수 도달 → 비트리거",
        "kwargs": dict(
            tls_id="J5",
            ped_link_indices=[2],
            tls_state=_TLS_STATE_PED_GREEN,
            remaining_s=1.0,
            pedestrians_detected=True,
            extension_count_in_cycle=1,   # == max_extensions(1)
            extended_in_cycle=False,
            signal_params=_DEFAULT_SIGNAL_PARAMS,
            sensor_pass=True,
        ),
        "expected_should_extend": False,
        "expected_reason": "max_extension_reached",
    },
    {
        "name": "F_no_pedestrians_detected",
        "desc": "보행자 미검지 → 비트리거",
        "kwargs": dict(
            tls_id="J5",
            ped_link_indices=[2],
            tls_state=_TLS_STATE_PED_GREEN,
            remaining_s=1.0,
            pedestrians_detected=False,
            extension_count_in_cycle=0,
            extended_in_cycle=False,
            signal_params=_DEFAULT_SIGNAL_PARAMS,
            sensor_pass=True,
        ),
        "expected_should_extend": False,
        "expected_reason": "no_pedestrians_detected",
    },
    {
        "name": "G_already_extended_in_cycle",
        "desc": "이번 사이클에 이미 확장 완료 → 비트리거",
        "kwargs": dict(
            tls_id="J5",
            ped_link_indices=[2],
            tls_state=_TLS_STATE_PED_GREEN,
            remaining_s=1.0,
            pedestrians_detected=True,
            extension_count_in_cycle=0,
            extended_in_cycle=True,
            signal_params=_DEFAULT_SIGNAL_PARAMS,
            sensor_pass=True,
        ),
        "expected_should_extend": False,
        "expected_reason": "already_extended_in_cycle",
    },
    {
        "name": "H_missing_tls_id",
        "desc": "tls_id 없음 → 비트리거",
        "kwargs": dict(
            tls_id=None,
            ped_link_indices=[2],
            tls_state=_TLS_STATE_PED_GREEN,
            remaining_s=1.0,
            pedestrians_detected=True,
            extension_count_in_cycle=0,
            extended_in_cycle=False,
            signal_params=_DEFAULT_SIGNAL_PARAMS,
            sensor_pass=True,
        ),
        "expected_should_extend": False,
        "expected_reason": "missing_tls_id",
    },
]


def run_synthetic_cases() -> int:
    """합성 케이스 8개를 실행하고 pass/fail을 출력한다.

    Returns:
        실패한 케이스 수 (0 이면 전부 통과).
    """
    failures = 0
    print(f"\n{'─'*80}")
    print("evaluate_smart_extension_decision() 합성 트리거 진단")
    print(f"import 경로: {evaluate_smart_extension_decision.__module__}")
    print(f"{'─'*80}\n")

    for case in _CASES:
        result = evaluate_smart_extension_decision(**case["kwargs"])
        ok_extend = result["should_extend"] == case["expected_should_extend"]
        ok_reason = result["reason"] == case["expected_reason"]
        passed = ok_extend and ok_reason
        tag = "PASS" if passed else "FAIL"

        ext_info = (
            f"extension_sec={result['extension_sec']}"
            if result["should_extend"]
            else f"reason={result['reason']}"
        )
        print(
            f"[{tag}] {case['name']:<35} "
            f"should_extend={str(result['should_extend']):<5}  {ext_info}"
        )
        if not passed:
            failures += 1
            if not ok_extend:
                print(
                    f"       ✗ should_extend: expected={case['expected_should_extend']}  "
                    f"got={result['should_extend']}"
                )
            if not ok_reason:
                print(
                    f"       ✗ reason:        expected={case['expected_reason']}  "
                    f"got={result['reason']}"
                )

    total = len(_CASES)
    passed_count = total - failures
    print(f"\n{'─'*80}")
    print(f"{passed_count}/{total} PASSED", end="")
    if failures == 0:
        print(
            " — evaluate_smart_extension_decision() 결정 로직 정상 확인.\n"
            "  (이 진단은 런타임 TraCI 입력값·보행자 검지 여부는 검증하지 않습니다.)"
        )
    else:
        print(f" — {failures}개 케이스 실패.")
    print(f"{'─'*80}\n")
    return failures


def _cmd_synthetic_trigger(args: argparse.Namespace) -> None:
    failures = run_synthetic_cases()
    sys.exit(failures)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="no-SUMO 스마트 신호 확장 트리거 진단"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser(
        "synthetic-trigger",
        help="합성 입력으로 8가지 케이스를 검증한다 (SUMO 불필요)",
    )

    parsed = parser.parse_args(argv)
    if parsed.command == "synthetic-trigger":
        _cmd_synthetic_trigger(parsed)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
