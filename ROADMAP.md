# kwin-mcp 로드맵

## 목적

이 MCP 서버는 **Claude Code가 KDE Plasma 앱(특히 krema dock)의 GUI를 자율적으로 테스트**할 수 있게 하기 위해 존재한다.

Claude Code가 이 MCP를 통해 할 수 있어야 하는 것:
1. 격리된 KWin Wayland 세션에서 앱을 실행
2. 마우스 호버, 클릭, 스크롤, 드래그 등 모든 인터랙션 수행
3. 스크린샷을 찍어 시각적 변화를 확인
4. 접근성 트리를 읽어 위젯 구조를 파악
5. 위 과정을 반복하여 **시각적/기능적/UX 피드백 루프**를 자율 수행

이를 통해 Claude Code는 코드를 수정한 후 직접 앱을 실행하고, GUI를 조작하고, 결과를 확인하여 개발 사이클을 자율적으로 완성할 수 있다.

### 주요 사용 시나리오 (krema dock)
- 마우스 호버 시 아이콘 parabolic zoom 확대 효과 확인
- 마우스 스크롤로 워크스페이스 전환 동작 확인
- 우클릭 컨텍스트 메뉴 표시 및 항목 동작 확인
- 드래그로 아이콘 재배치 동작 확인
- auto-hide 동작 확인

---

## 아키텍처

```
Claude Code
  └── kwin-mcp (MCP 서버)
        ├── 환경 관리: dbus-run-session + kwin_wayland --virtual
        ├── 화면 관찰: KWin ScreenShot2 D-Bus + AT-SPI2
        └── 입력 주입: fake-input Wayland 프로토콜 (inputsynth)
```

3중 격리로 호스트 데스크탑에 영향 없음:
1. dbus-run-session → D-Bus 세션 격리
2. kwin_wayland --virtual → 디스플레이 격리
3. fake-input 프로토콜 → 입력 격리

---

## 마일스톤

### M0: 프로젝트 초기화 ✅
- [x] uv 기반 프로젝트 구조 생성
- [x] pyproject.toml (의존성, ruff, ty 설정)
- [x] ROADMAP.md, CLAUDE.md, README.md
- [x] git 초기화

### M1: 격리 환경 관리 (`session.py`)
- [ ] dbus-run-session + kwin_wayland --virtual 시작/종료
- [ ] AT-SPI 데몬 자동 시작
- [ ] 앱 실행 (환경변수 자동 설정: QT_LINUX_ACCESSIBILITY_ALWAYS_ON 등)
- [ ] 프로세스 트리 정리
- **완료 기준**: `session_start`로 kcalc 같은 Qt 앱이 격리 KWin에서 실행됨

### M2: 스크린샷 캡처 (`screenshot.py`)
- [ ] KWin ScreenShot2 D-Bus 연동 (격리 세션)
- [ ] CaptureActiveScreen → pipe fd → ARGB32 → PNG → base64
- [ ] MCP 도구로 이미지 반환
- **완료 기준**: 격리 세션의 앱 화면을 캡처하여 Claude Code에서 이미지로 확인 가능

### M3: AT-SPI2 접근성 트리 (`accessibility.py`)
- [ ] gi.repository.Atspi로 격리 세션의 위젯 트리 탐색
- [ ] 역할, 이름, 상태, 좌표, 크기 추출
- [ ] 텍스트 형식으로 MCP 도구에서 반환
- **완료 기준**: 격리 세션의 앱 위젯 목록(버튼, 입력 등)을 좌표와 함께 조회 가능

### M4: 입력 주입 (`input.py`)
- [ ] inputsynth 래퍼 (W3C WebDriver Actions JSON 생성)
- [ ] 마우스: move(호버), click(좌/우/중간, 단일/더블), scroll, drag
- [ ] 키보드: type text, key combo (Ctrl+C 등)
- **완료 기준**: 격리 세션의 앱에 마우스/키보드 입력 주입 가능, screenshot으로 변화 확인

### M5: MCP 서버 통합 (`server.py`)
- [ ] 10개 도구 등록 및 MCP 서버 구동
- [ ] Claude Code 설정에 서버 등록
- [ ] 전체 피드백 루프 테스트 (실행 → 조작 → 확인)
- **완료 기준**: Claude Code에서 MCP 도구로 앱 실행/조작/스크린샷 확인의 전체 루프 수행 가능

### M6: krema 통합 테스트
- [ ] krema dock 앱을 격리 환경에서 실행
- [ ] 호버 확대, 스크롤, 우클릭, 드래그 테스트
- [ ] 피드백 루프로 UX 검증
- **완료 기준**: Claude Code가 krema를 실행하고 핵심 UX를 자율 테스트 가능
