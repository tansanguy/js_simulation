import os
import signal
import subprocess
import time
from pathlib import Path

BASE_CMD = [
    "python3", "smart_crosswalk_sumo/main.py",
    "--simulation_mode", "integrated_selected",
    "--smart_crosswalk_ids", "119055",
    "--sim_duration", "300",
    "--warmup", "0",
    "--network_mode", "expanded",
    "--buffer_m", "1000",
]

seeds = [1, 2, 3, 4, 5, 42, 43, 44, 45, 46]
timeout_sec = 900  # 각 300초 시뮬이 15분 넘으면 비정상으로 보고 끊음

log_dir = Path("result/_micro_logs")
log_dir.mkdir(parents=True, exist_ok=True)

summary = []

for seed in seeds:
    run_name = f"micro_119055_s{seed}_t300"
    log_path = log_dir / f"{run_name}.log"

    cmd = BASE_CMD + [
        "--seeds", str(seed),
        "--run_name", run_name,
    ]

    print(f"\n=== RUN {run_name} ===")
    print(" ".join(cmd))
    print(f"LOG: {log_path}")

    started = time.time()

    with open(log_path, "w", encoding="utf-8") as f:
        proc = subprocess.Popen(
            cmd,
            stdout=f,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid,
        )

        try:
            rc = proc.wait(timeout=timeout_sec)
            status = "OK" if rc == 0 else f"EXIT_{rc}"
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            status = "TIMEOUT_KILLED"

    elapsed = round(time.time() - started, 1)
    print(f"RESULT: {status}, elapsed={elapsed}s")

    summary.append((run_name, seed, status, elapsed, str(log_path)))

print("\n=== MICRO BATCH SUMMARY ===")
for row in summary:
    print(row)
