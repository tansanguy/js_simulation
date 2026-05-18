# Setup

이 문서는 로컬 또는 Mac mini에서 현재 파이프라인을 실행 가능한 상태로 만드는 방법이다.

## Python

- 현재 워크스페이스는 Python 3.12.3에서 돌고 있다.
- 프로젝트는 `requirements.txt` 기준으로 맞춘다.

## 가상환경

```bash
cd "$PROJECT_ROOT"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## SUMO 환경

```bash
export SUMO_HOME="/path/to/your/SUMO_HOME"
export PATH="$SUMO_HOME/bin:$PATH"
export PYTHONPATH="$PWD:${PYTHONPATH:-}"
```

`SUMO_HOME`는 환경별 설치 경로로 맞춘다. commands wrapper와 Python helper가 후보 경로를 찾는다.

## 입력 확인

- 네트워크 입력은 `result/active/nets/*.net.xml`이다.
- 보행자 assumption CSV는 `result/active/pedestrian_assumption/*.csv`다.
- 현재 30seed/smoke 입력은 `result/active/real_30seed_runs_sampled10/manifests/` 아래에 있다.

## 검증

```bash
bash commands/verify.sh
```

`verify.sh`는 `SUMO_HOME`과 필요한 입력 파일이 없으면 실패한다.
