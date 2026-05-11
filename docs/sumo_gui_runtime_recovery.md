# SUMO GUI 런타임 복구 절차

## 문제 요약

현재 macOS의 `sumo-gui`는 실행 파일 자체보다 번들 내부 `libproj`와 `libfontconfig`가 문제다.

- SUMO 프레임워크는 자체 라이브러리를 포함하고 있음
- 하지만 해당 라이브러리의 기본 데이터 경로가 번들 내부가 아니라 Homebrew 기준 경로로 남아 있음
- 그래서 다음 파일을 못 찾음
  - `proj.db`
  - `fonts.conf`

확인된 기대 경로:

- `libproj` 기본 경로: `/opt/homebrew/.../share/proj`
- `libfontconfig` 기본 경로: `/opt/homebrew/etc/fonts/fonts.conf`

현재 머신 상태:

- `proj` 미설치
- `fontconfig` 미설치

즉, 가장 안정적인 복구는 Homebrew 런타임 의존성을 실제로 설치하는 것이다.

## 권장 복구 절차

### 1. Homebrew 패키지 설치

```bash
brew install proj fontconfig
```

### 2. 설치 확인

```bash
test -f /opt/homebrew/share/proj/proj.db && echo "proj ok" || echo "proj missing"
test -f /opt/homebrew/etc/fonts/fonts.conf && echo "fontconfig ok" || echo "fontconfig missing"
```

### 3. font cache 갱신

```bash
fc-cache -fv
```

### 4. SUMO GUI 재실행

```bash
sumo-gui -c /Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/sumo_nets/cw_23040/smart_seed42.sumocfg
```

## 진단 명령

SUMO 번들 라이브러리의 install name 확인:

```bash
otool -D /Library/Frameworks/EclipseSUMO.framework/Versions/1.26.0/EclipseSUMO/lib/libproj.25.9.5.1.dylib
otool -D /Library/Frameworks/EclipseSUMO.framework/Versions/1.26.0/EclipseSUMO/lib/libfontconfig.1.dylib
```

현재 문제의 핵심은 이 값들이 번들 내부 경로가 아니라 다음처럼 Homebrew 경로라는 점이다.

- `/opt/homebrew/opt/proj/lib/libproj.25.dylib`
- `/opt/homebrew/opt/fontconfig/lib/libfontconfig.1.dylib`

## 대안 복구

Homebrew 설치 대신 `install_name_tool`로 SUMO 번들을 직접 패치할 수도 있다.
다만 이 방식은 코드서명, 추후 업데이트, 의존성 추적 측면에서 더 불안정하다.
특별한 이유가 없으면 권장하지 않는다.

## 정리

이 문제는 네트워크 파일이나 `sumocfg` 문제가 아니다.
SUMO macOS 배포본이 Homebrew 기반 `proj`와 `fontconfig` 데이터 경로를 기대하는 상태라서 발생한다.
따라서 근본 복구는 해당 런타임 리소스를 `/opt/homebrew` 경로에 정상 설치하는 것이다.
