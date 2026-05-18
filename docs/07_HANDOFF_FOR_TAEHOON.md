# 태훈님 인수인계

이 문서는 팀원이 바로 볼 수 있게 쓴 짧은 인수인계다. 여기서는 팀 구글드라이브에서 `XML`과 `net.xml`을 받아서 프로젝트 로컬 경로에 넣는 절차만 적는다.

다른 준비나 실행 설명이 필요하면 아래 문서를 보면 된다.

- 환경 준비: [`docs/01_SETUP.md`](01_SETUP.md)
- 실행 순서: [`docs/02_RUN_GUIDE.md`](02_RUN_GUIDE.md)
- 결과 해석: [`docs/03_RESULT_GUIDE.md`](03_RESULT_GUIDE.md)
- 정책 기준: [`docs/04_PIPELINE_POLICY.md`](04_PIPELINE_POLICY.md)
- 오류 확인: [`docs/05_TROUBLESHOOTING.md`](05_TROUBLESHOOTING.md)
- 커밋 기준: [`docs/06_COMMIT_GUIDE.md`](06_COMMIT_GUIDE.md)

## 1. XML 받기

팀 구글드라이브에 올라온 `XML`은 그 경로를 그대로 쓰지 말고, 프로젝트 로컬 경로로 옮겨서 써야 한다. 드라이브 경로를 직접 물고 있으면 팀원 환경마다 꼬이기 쉽다.

아래에서 `$PROJECT_ROOT`는 각자 clone 한 프로젝트 루트다. 예를 들면 `~/js_simulation`이다.

받아야 할 파일은 `result/active/nets/*.net.xml`이다. 이 파일들은 프로젝트 아래의 같은 경로로 넣는다.

```bash
mkdir -p "$PROJECT_ROOT/result/active/nets"
cp "<TEAM_GOOGLE_DRIVE_XML_DIR>/"*.xml "$PROJECT_ROOT/result/active/nets/"
```

만약 팀 구글드라이브에서 압축 파일로 받았다면, 먼저 풀고 나서 위 경로로 옮긴다.

```bash
tar -xzf active_nets_xml.tgz
mkdir -p "$PROJECT_ROOT/result/active/nets"
cp result/active/nets/*.xml "$PROJECT_ROOT/result/active/nets/"
```

## 2. 들어가야 하는 파일

최소한 아래 파일은 있어야 한다.

- `result/active/nets/current_main_12.net.xml`
- `result/active/nets/generated_signal_7.net.xml`
- `result/active/nets/p1_p4_recovery_6.net.xml`
- `result/active/nets/signal_fix_9.net.xml`

## 3. 확인

```bash
find result/active/nets -maxdepth 1 -type f -name "*.net.xml" | sort
```

위 파일들이 보이면 드라이브에서 받은 `XML`은 제대로 들어간 것이다.

## 4. 기억할 점

- `result/active/nets/*.net.xml`은 Git에 올리지 않는다.
- `XML`은 드라이브 경로를 직접 쓰지 않고 로컬 프로젝트 경로로 옮겨 둔다.
- 실행이나 환경 설명이 더 필요하면 `docs/01_SETUP.md`와 `docs/02_RUN_GUIDE.md`를 본다.
