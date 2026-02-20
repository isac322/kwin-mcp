# Claude Code 작업 지침

## 프로젝트 개요

kwin-mcp는 KDE Plasma 6 Wayland 환경에서 GUI 자동화를 위한 MCP(Model Context Protocol) 서버이다.
**목적**: Claude Code가 krema(KDE dock 앱)의 GUI를 자율적으로 실행/조작/관찰하는 피드백 루프 구현.

## 도구체인

- **패키지 관리**: `uv` (Astral). pip/poetry 대신 반드시 uv 사용.
- **Lint + Format**: `ruff` (Astral). 설정은 `pyproject.toml`의 `[tool.ruff]`.
- **Type Check**: `ty` (Astral). 설정은 `pyproject.toml`의 `[tool.ty]`.
- **빌드**: `uv build` (uv_build 백엔드)

### 자주 쓰는 명령어

```bash
uv sync                   # 의존성 설치/동기화
uv add <pkg>              # 의존성 추가
uv add --dev <pkg>        # 개발 의존성 추가
uv run ruff check .       # lint
uv run ruff format .      # format
uv run ty check           # type check
uv run python -m kwin_mcp # 서버 실행 (추후)
```

## 코드 스타일

- Python 3.12+
- ruff 규칙: E, F, W, I, N, UP, B, A, SIM, TCH, RUF
- 줄 길이: 100
- 따옴표: double quote
- type hint 필수

## 아키텍처

ROADMAP.md 참조. 핵심:
- `session.py`: dbus-run-session + kwin_wayland --virtual (격리 환경)
- `screenshot.py`: KWin ScreenShot2 D-Bus (스크린샷)
- `accessibility.py`: AT-SPI2 (위젯 트리)
- `input.py`: inputsynth / fake-input (입력 주입)
- `server.py`: MCP 서버 (도구 등록)

## 작업 전 확인사항

1. `ROADMAP.md`를 읽고 현재 진행 상태 파악
2. 다음 마일스톤의 첫 번째 미완료 항목부터 작업
3. 코드 수정 후 `uv run ruff check .` + `uv run ruff format .` + `uv run ty check` 실행
4. 마일스톤 항목 완료 시 ROADMAP.md 체크리스트 업데이트

## 시스템 의존성 (Arch/Manjaro)

- `at-spi2-core`: AT-SPI2 접근성 프레임워크 (설치됨)
- `python-gobject`: GObject introspection Python 바인딩 (설치됨)
- `kwin`: KWin Wayland 컴포지터 (설치됨)
- `spectacle`: 스크린샷 도구 (설치됨, 폴백용)
- `selenium-webdriver-at-spi`: inputsynth 바이너리 (AUR, 설치 필요할 수 있음)
