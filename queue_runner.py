import pandas as pd
import subprocess
import time
from pathlib import Path

# 📂 작업 목록 파일 경로
TASKS_FILE = "tasks.csv"

def run_simulation(name, smart_ids, duration):
    """실제 main.py를 호출하여 시뮬레이션을 실행하는 함수"""
    cmd = [
        "python3", "smart_crosswalk_sumo/main.py",
        "--simulation_mode", "integrated_selected",
        "--smart_crosswalk_ids", *smart_ids.split(),
        "--sim_duration", str(duration),
        "--run_name", name,
        "--seeds", "42", "43", "44"  # 정밀한 분석을 위해 3개 시드 권장
    ]
    
    print(f"\n" + "="*60)
    print(f"🚀 [시작] 작업명: {name}")
    print(f"📍 대상 ID: {smart_ids}")
    print(f"⏰ 시간: {duration}초")
    print("="*60)
    
    start_time = time.time()
    try:
        # 시뮬레이션 실행 (결과는 터미널과 로그에 실시간 출력)
        subprocess.run(cmd, check=True)
        elapsed = (time.time() - start_time) / 60
        print(f"\n✅ [성공] {name} 완료! (소요시간: {elapsed:.1f}분)")
        return True
    except subprocess.CalledProcessError as e:
        print(f"\n❌ [실패] {name} 실행 중 에러 발생 (Exit Code: {e.returncode})")
        return False

if __name__ == "__main__":
    if not Path(TASKS_FILE).exists():
        # 샘플 tasks.csv 생성 (파일이 없을 경우)
        sample_data = {
            "run_name": ["euljiro_sample", "bangsan_sample"],
            "smart_ids": ["10350 10352", "10360 10361"],
            "sim_duration": [600, 600]
        }
        pd.DataFrame(sample_data).to_csv(TASKS_FILE, index=False)
        print(f"📝 {TASKS_FILE} 샘플 파일을 생성했습니다. 이 파일을 수정해서 사용하세요.")

    # 작업 목록 읽기
    df = pd.read_csv(TASKS_FILE)
    total = len(df)
    print(f"🔥 총 {total}개의 시뮬레이션 작업이 대기 중입니다.")

    for i, row in df.iterrows():
        print(f"\n[{i+1}/{total}] 작업 준비 중...")
        success = run_simulation(row['run_name'], str(row['smart_ids']), row['sim_duration'])
        
        if not success:
            print("⚠️ 에러가 발생했으나 다음 작업을 계속 진행합니다.")
        
        # 서버 과열 방지를 위한 짧은 휴식
        time.sleep(5)

    print("\n🏁 모든 시뮬레이션 대기 작업을 마쳤습니다!")
