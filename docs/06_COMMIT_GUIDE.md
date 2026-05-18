# Commit Guide

커밋 전에는 좁게 본다. broad stage 안 쓴다.

## 먼저 확인

```bash
git status --short
```

## stage 후보

- `commands/`
- `smart_crosswalk_sumo/reporting/simple_final_pipeline.py`
- `smart_crosswalk_sumo/run_sampled10_group.py`
- `smart_crosswalk_sumo/run_phase6_recovery_smoke.py`
- 수요 정책 파일
- 현재 문서 파일

## stage 제외

- `outputs/`
- `result/`
- `result/active/nets/*.net.xml`
- `.venv/`
- `.DS_Store`
- `.claude/settings.local.json`

## stage 전

1. exact file list를 먼저 출력한다.
2. 필요한 파일만 고른다.
3. `git add .`와 `git add -A`는 쓰지 않는다.

## stage 후

```bash
git status --short
```

커밋은 선택된 파일만으로 만든다.
