# 준비하기

이 문서는 로컬 또는 Mac mini에서 현재 파이프라인을 실행할 수 있게 만드는 준비 절차다.
이 절차는 Python 3.11.x와 SUMO 1.26.0을 기준으로 적었다.

## Python

- Python은 `3.11.x`를 사용한다.
- 가상환경은 `python3.11 -m venv .venv`로 만든다.
- 패키지는 `requirements.txt` 기준으로 맞춘다.

## 가상환경

```bash
cd "$PROJECT_ROOT"
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## SUMO 환경

이 문서는 `SUMO 1.26.0` 기준이다. 다른 버전은 이 문서의 기준으로 보지 않는다.

```bash
export SUMO_HOME="/path/to/your/SUMO_HOME"
export PATH="$SUMO_HOME/bin:$PATH"
export PYTHONPATH="$PWD:${PYTHONPATH:-}"
```

`SUMO_HOME`은 SUMO 설치 경로다. 터미널을 새로 열 때마다 다시 설정해야 한다.

## 입력 확인

- 네트워크 입력은 `result/active/nets/*.net.xml`이다.
- 보행자 assumption CSV는 `result/active/pedestrian_assumption/*.csv`다.
- 현재 30seed/smoke 입력은 `result/active/real_30seed_runs_sampled10/manifests/` 아래에 있다.

## 검증

```bash
bash commands/verify.sh
```

`verify.sh`는 `SUMO_HOME`과 필요한 입력 파일이 없으면 바로 실패한다.
