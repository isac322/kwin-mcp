# Arch Linux Docker Smoke Harness for kwin-mcp

## TL;DR

> **Quick Summary**: Build a minimal Arch-family Docker image whose only job is to run `kwin_wayland --virtual` headlessly + a Python smoke test that imports `kwin_mcp` directly. Image does NOT contain kwin-mcp — host builds a wheel (`uv build`), mounts it as volume, container installs into venv at startup. Single command `scripts/test-distro.sh archlinux` orchestrates wheel build → image build → container run → evidence capture → exit code. **Multi-arch by default via a single base**: `manjarolinux/base:YYYYMMDD` is multi-arch (linux/amd64 + linux/arm64) and pacman-based (Arch-compatible), so one Dockerfile transparently covers both architectures. The Dockerfile name `docker/archlinux.Dockerfile` reflects the user-facing distro-family slot (`scripts/test-distro.sh archlinux`); the FROM line internally points at Manjaro purely because Arch's official image is amd64-only. Future-proof: adding ubuntu/debian/fedora/opensuse = drop one Dockerfile + one `SUPPORTED` array entry.
>
> **Deliverables**:
> - `docker/archlinux.Dockerfile` — minimal Arch-family image for **both amd64 + arm64** (single multi-arch Dockerfile; no kwin-mcp inside)
> - `docker/entrypoint.sh` — install mounted wheel into venv, exec smoke runner, propagate exit (arch-agnostic)
> - `docker/smoke_test.py` — Python smoke test importing `kwin_mcp.core.AutomationEngine` directly (arch-agnostic)
> - `docker/smoke_app.qml` — vendored QML test app (TextField "Smoke entry" + Button "Ping button" + Label "Status text" with deterministic accessible names)
> - `docker/runtime-contract.md` — single source of truth (mount paths, uid/gid, venv path, screen size, locale, env, base-image policy with date-tag pinning)
> - `scripts/test-distro.sh` — host wrapper: `uv build` → `docker build` → `docker run` → evidence collection (single Dockerfile per distro slot; no host-arch branching)
> - `.gitignore` update (`.sisyphus/evidence/`)
> - `docs/docker-testing.md` — usage docs + how to add a new distro
> - `ROADMAP.md` checkbox update
>
> **Estimated Effort**: Medium
> **Parallel Execution**: YES — 3 waves
> **Critical Path**: T1 (date-tag lock for `manjarolinux/base`) → T3 (runtime contract) → T6 (single multi-arch Dockerfile) → T10 (POC end-to-end run on host arch) → F1-F4

---

## Context

### Original Request
> 이 프로젝트는 여러 linux distro에서 동작이 되는것을 확인하는게 중요해. ... archlinux부터 테스트해보자. 먼저 archlinux base에서부터 kwin-mcp를 테스트하기위한 가장 최소한의 의존성을 설치해놓은 docker image를 만들고, ... kwin-mcp는 cli도 지원하기 때문에 docker 내부에서 테스트는 cli를 쓰면 될 것 같아. ... 로컬에서는 python wheel을 빌드 까지만 하고, 컨테이너 안에서 해당 wheel을 volume mount로 받아서 설치 후 실행하는 방식이어야 해. ... 만약 정확히 이 목적으로 누군가 이미지를 만들어놨다면 그걸 그냥 사용하면 돼. ... 꼭 kcalc가 아니어도 돼. 접근성이 있고, kwin에서 실행할 수 있는 gui 앱이면 어느것이든 상관없어.

### Interview Summary
**Key Discussions**:
- Verification depth → smoke + input validation (mouse_click, keyboard_type) — NOT full 30-tool regression
- CI scope → local-only this plan; GitHub Actions deferred
- Image publishing → local build only this plan; GHCR deferred
- Image content → image is just a runtime; kwin-mcp wheel is volume-mounted at run time
- Smoke runner → Python script imports `kwin_mcp.core.AutomationEngine` directly (not via subprocess CLI)
- Layout → `docker/` + `scripts/` separation; future distros drop in alongside
- Test app → ANY a11y-exposing GUI app on KWin; `kcalc` is acceptable but heavy KDE deps; lighter alternatives must be investigated

**Research Findings (citations)**:
- `src/kwin_mcp/cli.py:138-141` — pipe mode auto via stdin TTY check (we still avoid CLI; use direct import)
- `src/kwin_mcp/session.py:148-154` — `dbus-run-session bash -c <wrapper>` is how virtual session boots
- `src/kwin_mcp/session.py:331-379` — wrapper runs `at-spi-bus-launcher`, `dbus-update-activation-environment`, `kwin_wayland --virtual --no-lockscreen --width $W --height $H --socket $S`, polls for socket
- `src/kwin_mcp/session.py:383-408` — env vars set: `KDE_FULL_SESSION`, `XDG_SESSION_TYPE=wayland`, `QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1`, `ATSPI_DBUS_IMPLEMENTATION=dbus-daemon`, `KWIN_SCREENSHOT_NO_PERMISSION_CHECKS=1`, `KWIN_WAYLAND_NO_PERMISSION_CHECKS=1`
- `src/kwin_mcp/core.py:170-179` — `session_start` defaults: 1920x1080, no clipboard, no isolate_home
- `src/kwin_mcp/core.py:696-713` — `launch_app(command, env=None)`, `list_windows()`, `focus_window(app_name)`
- `CONTRIBUTING.md:17-24` — Arch packages: `kwin spectacle at-spi2-core python-gobject dbus-python-common` (mandatory), `wl-clipboard wtype wayland-utils` (optional)
- `pyproject.toml:55-60` — runtime deps: `mcp`, `PyGObject`, `dbus-python`, `Pillow`
- `.github/workflows/ci.yml` — only lint/ty/build; no KWin runtime testing exists yet
- `.gitignore` — does NOT yet ignore `.sisyphus/`

**Container/headless KWin findings (librarian)**:
- `manjarolinux/base:YYYYMMDD` is the chosen runtime base for BOTH amd64 and arm64 — it is multi-arch (linux/amd64 + linux/arm64), pacman-based, ships `archlinux-keyring + manjaro-keyring`, and is Arch-compatible (same `pacman -Syu` install flow). This single base eliminates host-arch branching in the wrapper. Pin by **date-stamped tag** (`YYYYMMDD`, e.g. `20260322`); first RUN must be `pacman-key --init && pacman-key --populate archlinux manjaro && pacman -Syu --noconfirm`. (`archlinux:base` was rejected because the official Arch image is amd64-only — verified at https://hub.docker.com/_/archlinux — which would have forced a second Dockerfile for arm64.)
- KWin virtual backend has QPainterCompositing fallback when no render device → `/dev/dri` NOT required
- DRM backend in containers fails with permission errors (containers/toolbox#1553) — stick with `--virtual`
- libei is UNIX-socket based; `/dev/uinput` is server-side concern, never needed by client
- AT-SPI2 auto-activates via D-Bus `org.a11y.Bus`
- XDG_RUNTIME_DIR must be 0700, owned by user (freedesktop basedir spec)
- Mesa llvmpipe enables headless software rendering
- `dbus-run-session` is the right wrapper; systemd not needed

### Metis Review (gap analysis incorporated)

**Key Metis findings (now reflected in this plan)**:
1. **`uv pip install --system` was a risky assumption** → plan now uses **a writable venv** at `/opt/kwinmcp-venv` owned by the non-root user
2. **POC validation gate is mandatory** → T10 is explicitly a "first-run, debug, iterate to green" task before declaring success
3. **kcalc as canonical test app is brittle** → T2 makes test-app selection a deliberate decision (lightest a11y-exposing GUI, librarian-investigated)
4. **Runtime contract must be locked first** → T3 is explicit: documents wheel mount path, evidence mount path, uid/gid, venv path, screen size, locale, env vars BEFORE Wave 2 implements them
5. **Input validation by log strings = false positive risk** → smoke test asserts on **observable state changes** in the accessibility tree (e.g. text field value changes, button focus state) — NOT on log strings
6. **Evidence shape must be defined** → standardized: `{stdout.log, stderr.log, screenshots/initial.png, screenshots/post-click.png, screenshots/post-typing.png, a11y/before.txt, a11y/after.txt, summary.json}` per run (a11y dumps are TXT because `accessibility_tree()` returns `str` per `src/kwin_mcp/core.py:331-335`, NOT a JSON-serializable dict)
7. **Locale traps avoided** → image pins `LANG=C.UTF-8`; smoke test does NOT match on locale-sensitive UI text
8. **`kcalc` (or any test app) UI string assertions are forbidden** → assertions target accessible name/role/state, not user-visible text
9. **Architecture target**: BOTH amd64 AND arm64 supported via a SINGLE multi-arch base. Strategy: `manjarolinux/base:YYYYMMDD` (multi-arch upstream — https://hub.docker.com/r/manjarolinux/base — pacman-based, Arch-compatible) covers both architectures from one Dockerfile, eliminating host-arch branching in the wrapper. (Rejected alternative: using `archlinux:base-YYYYMMDD.0.<id>` for amd64 and Manjaro for arm64 — would have required two near-identical Dockerfiles. The official `archlinux` image is amd64-only — verified at https://hub.docker.com/_/archlinux — which is why we picked Manjaro: it offers Arch parity AND multi-arch in one base.)
10. **Failure handling defined** → on any failure inside container, evidence is flushed BEFORE container exits (entrypoint uses `trap` to copy)

---

## Work Objectives

### Core Objective
Produce a single command (`scripts/test-distro.sh archlinux`) that, on a developer's Linux machine with Docker installed, builds an Arch Linux container, builds a kwin-mcp wheel, runs the wheel inside that container against a virtual KWin session, exercises smoke + input flows on a lightweight GUI test app via direct `AutomationEngine` calls, and exits 0 (with full evidence on disk) iff everything worked.

### Concrete Deliverables
1. `docker/archlinux.Dockerfile` (**multi-arch: amd64 + arm64**) — single Dockerfile based on `manjarolinux/base:YYYYMMDD` (date-tag pinned, never `:latest` or `@sha256:`); Manjaro is multi-arch and pacman-based so one Dockerfile covers both architectures. The filename keeps the user-facing distro-family slot name (`scripts/test-distro.sh archlinux`) for consistency. Includes system packages, non-root `kwinmcp` user (uid 1000), `/opt/kwinmcp-venv` (uv-managed), `XDG_RUNTIME_DIR=/run/user/1000` (mode 0700), `LANG=C.UTF-8`. A header comment in the file explains why the FROM line points at Manjaro despite the `archlinux` filename.
2. `docker/entrypoint.sh` — sets traps to flush evidence on exit; installs mounted wheel into venv; execs `python /opt/docker/smoke_test.py`; propagates exit code
3. `docker/smoke_test.py` — Python: imports `from kwin_mcp.core import AutomationEngine`; runs `session_start` → `launch_app(<TEST_APP>)` → `wait_for_element` (a11y) → `screenshot` (initial) → `mouse_click` on a known widget → `screenshot` (post-click) → `keyboard_type` into a focused widget → `screenshot` (post-typing) → asserts a11y state change (NOT text) → dumps trees + screenshots + summary.json to `/evidence/` → `session_stop`
4. `docker/runtime-contract.md` — single source of truth for mount paths, uid/gid, venv path, screen size (1920x1080), locale, env, evidence layout, exit code semantics. Future distro Dockerfiles MUST conform.
5. `scripts/test-distro.sh` — bash wrapper: validates `archlinux` arg, runs `uv build --wheel`, runs `docker build -f docker/<distro>.Dockerfile -t kwin-mcp-test:<distro> docker/` (build context is `docker/` so `COPY entrypoint.sh` resolves; this matches T6/T9 exactly; one Dockerfile per distro slot, no host-arch branching because the chosen base is multi-arch), runs `docker run --rm <mounts> kwin-mcp-test:<distro>`, propagates container exit code
6. `.gitignore` — adds `.sisyphus/evidence/` (and confirms `.sisyphus/drafts/` and `.sisyphus/plans/` remain tracked-or-not per existing convention)
7. `docs/docker-testing.md` — how to run, what evidence looks like, how to add a new distro (1-page checklist)
8. `ROADMAP.md` — checkbox added under appropriate milestone

### Definition of Done
- [x] `scripts/test-distro.sh archlinux` exits 0 on a clean checkout (no env modifications needed beyond Docker daemon running)
- [x] `.sisyphus/evidence/archlinux/<timestamp>/` exists with: `summary.json`, `stdout.log`, `stderr.log`, `screenshots/{initial,post-click,post-typing}.png` (all > 1 KB, all 3 SHA-256 hashes distinct), `a11y/{before,after}.txt` (formatted accessibility-tree text dumps; `before.txt` and `after.txt` MUST differ)
- [x] `summary.json` reports `verdict: "pass"` AND includes a populated `install` object (with `wheel_basename`, `wheel_sha256`, `kwin_mcp_version`, `package_versions` map, `image_tag` — populated by T8 merging T7's `install.json`) AND includes `tasks_passed` integer ≥ 5 AND includes `screenshot_sha` object with all 3 keys (`initial`, `post_click`, `post_typing`) all different
- [x] No `--privileged`, no `--cap-add=SYS_ADMIN`, no `--device=/dev/uinput`, no `--device=/dev/input`, no `--device=/dev/dri` in any docker run command (these exact 5 flag-strings must be absent — verified by grep in F1, F3, F4, Success Criteria)
- [x] `scripts/test-distro.sh archlinux` works on BOTH amd64 hosts AND arm64 hosts using a SINGLE multi-arch Dockerfile (`docker/archlinux.Dockerfile`, FROM `manjarolinux/base:YYYYMMDD`). The wrapper does NOT branch on `uname -m`; the multi-arch base handles both architectures transparently. Date-tag pinned (no `:latest`, no `@sha256:` digest).
- [x] No file under `src/kwin_mcp/` is modified (read-only consumer)
- [x] No GitHub Actions workflow file added or modified
- [x] No GHCR or registry pushes happen
- [x] Adding a hypothetical `docker/ubuntu.Dockerfile` would require changing `scripts/test-distro.sh` ONLY in its argument validation (same contract reused)
- [x] `docs/docker-testing.md` exists and a fresh contributor could follow it without asking questions

### Must Have
- Single multi-arch image based on `manjarolinux/base:YYYYMMDD` (date-tag pinned, never `:latest`, never `@sha256:`). Manjaro chosen because it is multi-arch (linux/amd64 + linux/arm64) and pacman-based (Arch parity). Must use a specific dated tag — no floating tags.
- Image installs ONLY: `kwin spectacle at-spi2-core python-gobject dbus-python-common mesa wl-clipboard wtype wayland-utils python uv` plus the chosen test-app package, plus minimal locale/`base` runtime — and cleans pacman cache afterward
- Container runs as non-root user `kwinmcp` (uid 1000)
- Smoke test imports `kwin_mcp.core.AutomationEngine` DIRECTLY — does NOT shell out to `kwin-mcp-cli`
- Smoke test asserts on accessibility-tree state CHANGES (focus, value-changed events, role match) NOT UI string content
- Evidence is flushed to host volume even on container failure (entrypoint trap)
- Wheel is built fresh by the wrapper on each run (`uv build --wheel`) — no stale wheel reuse
- Layout structurally accommodates ubuntu/debian/fedora/opensuse without rewriting wrapper logic

### Must NOT Have (Guardrails)
- ❌ Modify any file under `src/kwin_mcp/` (read-only consumer)
- ❌ Introduce `pytest` or any unit test framework — smoke is a single Python script
- ❌ Add GitHub Actions workflow file in this plan (deferred)
- ❌ Push images to any registry (GHCR/Docker Hub) in this plan (deferred)
- ❌ Use `--privileged`, `--cap-add=SYS_ADMIN`, `--device=/dev/uinput`, `--device=/dev/input`, `--device=/dev/dri` (default invocation MUST work without any of these)
- ❌ Bake the kwin-mcp wheel into the image (must be runtime mounted)
- ❌ Build other distros' Dockerfiles in this plan (Ubuntu/Debian/Fedora/openSUSE)
- ❌ Use X11/Xvfb/x11docker as a fallback (Wayland-only)
- ❌ Match smoke assertions on user-visible UI text (locale-fragile)
- ❌ Match smoke assertions on log string content alone (false-positive risk)
- ❌ Build a generic multi-distro abstraction layer (one shared base Dockerfile etc.) — Arch only, future distros are independent files
- ❌ Treat the smoke test as a logging framework — keep it boring, single-file, single-purpose
- ❌ Reuse host's `~/.config`, `~/.cache`, or any host XDG path — full container isolation
- ❌ Modify `.github/workflows/docs-seo.yml` triggers (the new `docker/`+`scripts/test-distro.sh` paths are intentionally outside its scope)
- ❌ Modify CLAUDE.md's docs-seo trigger table (these new files don't have SEO/manifest-sync implications)

---

## Verification Strategy (MANDATORY)

> **ZERO HUMAN INTERVENTION** — every verification is agent-executed via Bash/file inspection.

### Test Decision
- **Infrastructure exists**: NO (current CI is lint/ty/build only on ubuntu-latest)
- **Automated tests in this plan**: NO new unit tests; this IS the integration smoke harness
- **Framework**: NO pytest, NO bun test — smoke is a single Python script invoked once
- **Agent-Executed QA**: MANDATORY for every TODO

### QA Policy
Every task has agent-executed QA scenarios. Evidence saved to `.sisyphus/evidence/task-{N}-<slug>.{ext}`.
- **Docker layer**: Bash (`docker build`, `docker inspect`, `docker run --rm`); assertions via `jq`/`grep` on JSON/text output, file existence + size checks
- **Bash scripts**: Bash (`bash -n` for syntax, `shellcheck`, run with `set -x`, capture exit codes)
- **Python smoke runner**: Bash (run inside container during T10; verify by stdout schema and evidence file shape)
- **Markdown docs**: Bash (`grep` for required sections, `markdown-link-check` if convenient, otherwise visual self-review)

### Evidence Layout (per task)
- `.sisyphus/evidence/task-{N}-<scenario-slug>.{png,json,log,txt}`
- Final-wave evidence: `.sisyphus/evidence/final-qa/`

---

## Execution Strategy

### Parallel Execution Waves

```
Wave 1 (Start Immediately — foundation, max parallel):
├── T1. Lock date-tag for manjarolinux/base (single multi-arch base) [unspecified-high]
├── T2. Test-app decision: lightest a11y-exposing GUI (librarian) [unspecified-high]
├── T3. Runtime contract document (docker/runtime-contract.md) [writing]
├── T4. .gitignore update for .sisyphus/evidence/ [quick]
└── T5. Create docker/ directory scaffold + README.md placeholder [quick]

Wave 2 (After Wave 1 — implementation):
├── T6. docker/archlinux.Dockerfile (single multi-arch Dockerfile, FROM manjarolinux/base) [deep]
├── T7. docker/entrypoint.sh [unspecified-high]
├── T8. docker/smoke_test.py [deep]
└── T9. scripts/test-distro.sh (single Dockerfile per distro slot, no host-arch branching) [unspecified-high]

Wave 3 (After Wave 2 — POC validation + docs):
├── T10. End-to-end POC: run scripts/test-distro.sh archlinux, debug, iterate to green [deep]
├── T11. docs/docker-testing.md [writing]
└── T12. ROADMAP.md checkbox [quick]

Wave FINAL (After ALL tasks — 4 parallel reviews, then user okay):
├── F1. Plan compliance audit (oracle)
├── F2. Code quality review (unspecified-high)
├── F3. Real manual QA — actual scripts/test-distro.sh archlinux run + evidence inspection (unspecified-high)
└── F4. Scope fidelity check (deep)
-> Present results -> Get explicit user okay

Critical Path: T1 → T3 → T6 → T10 → F1-F4 → user okay
Parallel Speedup: ~60% faster than sequential
Max Concurrent: 5 (Wave 1) | 4 (Wave 2)
```

### Dependency Matrix

- **T1**: blocked-by none; blocks T6, T10
- **T2**: blocked-by none; blocks T6, T8
- **T3**: blocked-by none; blocks T6, T7, T8, T9, T11
- **T4**: blocked-by none; blocks T9 (wrapper writes there), T10
- **T5**: blocked-by none; blocks T6, T7, T8
- **T6**: blocked-by T1, T2, T3, T5; blocks T10
- **T7**: blocked-by T3, T5; blocks T10
- **T8**: blocked-by T2, T3, T5; blocks T10
- **T9**: blocked-by T3, T4; blocks T10
- **T10**: blocked-by T6, T7, T8, T9; blocks F1-F4 (POC builds and runs the single Dockerfile end-to-end on host arch — same Dockerfile resolves to amd64 or arm64 layer automatically)
- **T11**: blocked-by T3, T10 (docs reflect actual contract); blocks F1
- **T12**: blocked-by T10; blocks none
- **F1-F4**: blocked-by all of T1-T12; blocks user-okay step

### Agent Dispatch Summary

- **Wave 1 (5)**: T1 → `unspecified-high`, T2 → `unspecified-high`, T3 → `writing`, T4 → `quick`, T5 → `quick`
- **Wave 2 (4)**: T6 → `deep`, T7 → `unspecified-high`, T8 → `deep`, T9 → `unspecified-high`
- **Wave 3 (3)**: T10 → `deep`, T11 → `writing`, T12 → `quick`
- **FINAL (4)**: F1 → `oracle`, F2 → `unspecified-high`, F3 → `unspecified-high`, F4 → `deep`

---

## TODOs

- [x] 1. Lock date-stamped tag for `manjarolinux/base` (single multi-arch base)

  **What to do**:
  - The base image is pre-decided: `manjarolinux/base:YYYYMMDD` (Docker Hub: https://hub.docker.com/r/manjarolinux/base). Multi-arch (linux/amd64 + linux/arm64) — covers both host architectures from one Dockerfile. Pacman-based, ships `archlinux-keyring + manjaro-keyring` (Dockerfile: https://github.com/manjaro/manjaro-docker/blob/main/base.Dockerfile).
  - At execution time, look up the **most recent stable date-tag** on Docker Hub. Tag format is `YYYYMMDD` (e.g. `20260322`). If a release looks unusually fresh (<24h), prefer the previous available tag.
  - Validate the tag is pullable on both architectures:
    - `docker pull manjarolinux/base:<chosen-tag>` (host-native)
    - `docker pull --platform linux/arm64 manjarolinux/base:<chosen-tag>` (cross-arch — verifies the multi-arch manifest entry exists)
  - Briefly survey (≤30 min) whether any community image (KDE invent CI, kasmweb, x11docker, linuxserver/docker-webtop) provides a more turnkey headless-KWin base. If a clearly superior option exists AND meets all guardrails (multi-arch, no `--privileged`, no GPU passthrough), use it. Otherwise stick with the librarian-recommended Manjaro base.
  - (`archlinux:base` was considered and explicitly rejected: the official Arch image is amd64-only — verified at https://hub.docker.com/_/archlinux — which would have forced a second Dockerfile for arm64. Manjaro gives Arch parity AND multi-arch in one base.)
  - Hand off the chosen tag + rationale to T3 (which writes it into `docker/runtime-contract.md` "Base image decision" section). T1 leaves the structured note at `.sisyphus/evidence/task-1-base-image-decision.md` so T3 incorporates verbatim.

  **Must NOT do**:
  - Pick an image requiring `--privileged` or GPU passthrough — instant disqualification
  - Pick a non-pacman-based image (Ubuntu/Debian/Fedora-based defeats the Arch-family point)
  - Pick an amd64-only image (would re-introduce the dual-Dockerfile problem; multi-arch is non-negotiable)
  - Use floating tags (`:latest`, `:base`, `:main` without date suffix) — must be a specific dated tag
  - Use `@sha256:` digest pinning — we deliberately use **date-tags only** for human readability and predictable rebuild cycles (decision lives in Definition of Done)
  - Adopt an unmaintained image (>1 year stale) without compelling, written reason
  - Spend more than ~30 min on the community-image survey — librarian already did the heavy lifting; T1 is primarily a validation + record task

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: Investigation + decision-making across web sources; needs judgment on trade-offs, not just code edits
  - **Skills**: none
    - `visual-engineering`/`artistry` etc. have no domain overlap with image-survey work

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1 (with T2, T3, T4, T5)
  - **Blocks**: T6 (provides FROM line + date-tag value for the single multi-arch base)
  - **Blocked By**: None — start immediately

  **References**:

  **Pattern References**:
  - `CONTRIBUTING.md:17-24` — required Arch packages: `kwin spectacle at-spi2-core python-gobject dbus-python-common` + optional `wl-clipboard wtype wayland-utils`. Any candidate must already supply these OR allow installing them on top with `pacman` available.
  - `README.md:343-356` — same Arch install snippet, same screening checklist

  **External References**:
  - Docker Hub `manjarolinux/base` (multi-arch) — https://hub.docker.com/r/manjarolinux/base — chosen base; date-tag source. Format: `YYYYMMDD` (e.g. `20260322`). Pacman-based, multi-arch (linux/amd64 + linux/arm64).
  - Manjaro Dockerfile — https://github.com/manjaro/manjaro-docker/blob/main/base.Dockerfile — confirms `pacman-key --init`, ships `archlinux-keyring + manjaro-keyring`, packages installable with `pacman -Syu`.
  - Docker Hub `archlinux` — https://hub.docker.com/_/archlinux — REJECTED ALTERNATIVE: amd64-only, would force a dual-Dockerfile design.
  - KDE invent CI images — https://invent.kde.org/sysadmin — survey only; low expected payoff
  - ArchWiki Docker — https://wiki.archlinux.org/title/Docker — documents `pacman-key --init` + image gotchas (applies to Manjaro too because both are pacman-based)
  - containers/toolbox#1553 — https://github.com/containers/toolbox/issues/1553 — confirms DRM backend fails in unprivileged containers; auto-reject any image relying on DRM

  **WHY Each Reference Matters**:
  - Docker Hub `manjarolinux/base` is the authoritative date-tag listing; must be checked at execution time so we don't pin a tag that's been replaced
  - Manjaro's base.Dockerfile proves it's drop-in pacman-compatible — same install commands as Arch work
  - The Arch Hub page is cited specifically to lock in the rejection rationale (amd64-only) so future contributors don't reopen the question
  - KDE invent is the only place a ready-made headless-KWin image might exist; quick check, not a deep dive
  - ArchWiki Docker page tells the executor what `pacman-key --init` looks like — same drill on Manjaro

  **Acceptance Criteria**:

  **QA Scenarios (MANDATORY)**:
  ```
  Scenario: Single date-tag recorded with rationale (happy path)
    Tool: Bash
    Preconditions: T1 investigation complete; evidence note written
    Steps:
      1. cat .sisyphus/evidence/task-1-base-image-decision.md
      2. Assert file contains a line matching regex `^FROM manjarolinux/base:[0-9]{8}\s*$` (e.g. `FROM manjarolinux/base:20260322`)
      3. Assert file does NOT contain `@sha256:` — digest pinning is deliberately forbidden
      4. Assert file does NOT contain `:latest` and does NOT contain `manjarolinux/base$` or `manjarolinux/base ` (no floating tag without date suffix)
      5. Assert file contains a non-empty `## Decision rationale` section explaining why this specific date (recency, stability, multi-arch coverage)
      6. Assert file contains a non-empty `## Rejected alternatives` section that explicitly mentions `archlinux:base` was rejected for being amd64-only
    Expected Result: Single multi-arch base pinned by date-tag (NO @sha256, NO floating); rationale + rejection documented
    Failure Indicators: any floating tag; missing rationale; rejection of archlinux:base not documented
    Evidence: .sisyphus/evidence/task-1-base-image-decision.md

  Scenario: Forbidden-flag images stayed rejected (negative)
    Tool: Bash
    Preconditions: Decision recorded
    Steps:
      1. Run: awk '/^## Chosen option/,/^## /' .sisyphus/evidence/task-1-base-image-decision.md > /tmp/t1-chosen.txt
      2. Run: grep -E '\-\-privileged|\-\-cap-add=SYS_ADMIN|\-\-device=/dev/' /tmp/t1-chosen.txt
      3. Assert: grep returns NOTHING (exit 1) — these flags must not appear under "Chosen option"
    Expected Result: Chosen image does not require any forbidden runtime flag
    Evidence: .sisyphus/evidence/task-1-rejected-flags-audit.txt (empty file = pass)
  ```

  **Evidence to Capture**:
  - [ ] `.sisyphus/evidence/task-1-base-image-decision.md` (markdown report with candidates, rationale, chosen single multi-arch date-tag, plus rejected-alternatives section)
  - [ ] `.sisyphus/evidence/task-1-rejected-flags-audit.txt` (empty file proves clean grep)

  **Commit**: YES (part of C1, groups with T3-T5)
  - Message: `chore(docker): scaffold test harness directory + runtime contract`
  - Files: contributes data to `docker/runtime-contract.md` (T3 writes the file)
  - Pre-commit: `test -s .sisyphus/evidence/task-1-base-image-decision.md`

- [x] 3. Define runtime contract (`docker/runtime-contract.md`)

  **What to do**:
  - Create `docker/runtime-contract.md` — the immutable cross-distro contract that ALL future Dockerfiles + entrypoints + smoke tests + wrapper scripts must conform to
  - Sections (in order):
    1. **Mount paths** (all 4 are required for every distro Dockerfile + wrapper invocation): `/wheels` (read-only host wheel dir, source of the kwin-mcp wheel), `/evidence` (read-write evidence sink), `/opt/docker/smoke_test.py` (read-only mount of host's `docker/smoke_test.py`), `/opt/docker/smoke_app.qml` (read-only mount of host's `docker/smoke_app.qml` — the test app the smoke runner launches via `qml6`)
    2. **User**: uid 1000, gid 1000, name `kwinmcp`, home `/home/kwinmcp`, shell `/bin/bash`
    3. **Venv**: `/opt/kwinmcp-venv` (created by Dockerfile, owned by `kwinmcp`, populated by entrypoint via `uv pip install /wheels/*.whl`)
    4. **XDG_RUNTIME_DIR**: `/run/user/1000`, mode `0700`, owned by `kwinmcp` (created by Dockerfile, NOT by tmpfs at runtime)
    5. **Screen size**: 1920×1080 (matches `core.py:170-179` default; do NOT override unless test app requires it)
    6. **Locale**: `LANG=C.UTF-8`, `LC_ALL=C.UTF-8` (must be generated in Dockerfile if Arch base does not include it)
    7. **Env vars by source**:
       - Dockerfile-set: `LANG`, `LC_ALL`, `XDG_RUNTIME_DIR`, `PATH` (with `/opt/kwinmcp-venv/bin` prepended)
       - Entrypoint-set: `PYTHONUNBUFFERED=1` (so logs flush)
       - kwin-mcp/session.py-set (do NOT duplicate): `KDE_FULL_SESSION`, `XDG_SESSION_TYPE`, `QT_LINUX_ACCESSIBILITY_ALWAYS_ON`, `ATSPI_DBUS_IMPLEMENTATION`, `KWIN_*_NO_PERMISSION_CHECKS` (see `session.py:383-408`)
    8. **Test app**: name, Arch package, launch command — value filled from T2 result (T3 may include a placeholder `{{TEST_APP}}` and a "filled by T2" note if T2 hasn't completed yet; if T3 runs after T2, fill directly)
    9. **Base image decision**: date-tag + rationale — value filled from T1 result. Lists ONE date-tag for the single multi-arch base: `manjarolinux/base:YYYYMMDD` (covers both amd64 + arm64; pacman-based, Arch-compatible). NEVER `@sha256:` (digest pinning forbidden by policy). MUST also include a "Rejected alternatives" subsection naming `archlinux:base` and the reason (amd64-only).
    10. **Evidence layout** (canonical paths under `/evidence/<timestamp>/`): `summary.json`, `stdout.log`, `stderr.log`, `screenshots/initial.png`, `screenshots/post-click.png`, `screenshots/post-typing.png`, `a11y/before.txt`, `a11y/after.txt` (text dumps because `accessibility_tree()` returns `str`, not JSON — see `src/kwin_mcp/core.py:331-335`)
    11. **Exit code semantics**: `0` pass, `1` smoke assertion failed, `2` environment setup failed, `3` wheel install failed, `≥10` uncaught exception
    12. **Forbidden flags**: `--privileged`, `--cap-add=SYS_ADMIN`, `--device=/dev/uinput`, `--device=/dev/input`, `--device=/dev/dri` — list verbatim so future contributors don't reintroduce them

  **Must NOT do**:
  - Document Arch-specific package names here (those belong in T6's Dockerfile, not the cross-distro contract)
  - Make any contract clause require a forbidden runtime flag
  - Embed implementation details of one specific Dockerfile (entry-point script body, etc.)
  - Reference distro-specific paths like `/var/cache/pacman` (Arch-specific) — keep it distro-agnostic

  **Recommended Agent Profile**:
  - **Category**: `writing`
    - Reason: Pure documentation authoring; precision and structure matter more than code skill
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1 (with T1, T2, T4, T5)
  - **Blocks**: T6, T7, T8, T9, T11 (everyone reads the contract)
  - **Blocked By**: None to START; T1 and T2 deliver values to FILL specific sections — if T1/T2 not done when T3 starts writing, leave placeholders and update later (T3 must be re-edited once T1/T2 complete; do this within Wave 1 before Wave 2 begins)

  **References**:

  **Pattern References**:
  - `src/kwin_mcp/core.py:170-179` — `session_start` defaults (1920x1080) — contract MUST match these to avoid surprising users
  - `src/kwin_mcp/session.py:383-408` — env vars set internally by `_build_env()` — contract documents which to NOT duplicate
  - `src/kwin_mcp/session.py:331-379` — wrapper script that boots virtual session — informs which binaries the contract demands on PATH

  **External References**:
  - freedesktop XDG Basedir spec — https://specifications.freedesktop.org/basedir-spec/latest/ — `XDG_RUNTIME_DIR` must be 0700, user-owned
  - Conventional Commits — https://www.conventionalcommits.org/ — for the exit-code semantics naming style

  **WHY Each Reference Matters**:
  - core.py:170-179 / session.py:383-408 are THE authoritative source of contract values; the contract document is just a public-facing transcript of these
  - XDG spec is the canonical justification for the 0700 mode requirement — cite it so reviewers don't second-guess

  **Acceptance Criteria**:

  **QA Scenarios (MANDATORY)**:
  ```
  Scenario: All 12 sections present (happy path)
    Tool: Bash
    Preconditions: T3 complete
    Steps:
      1. for s in "Mount paths" "User" "Venv" "XDG_RUNTIME_DIR" "Screen size" "Locale" "Env vars" "Test app" "Base image decision" "Evidence layout" "Exit code semantics" "Forbidden flags"; do grep -q "^## $s" docker/runtime-contract.md || echo "MISSING: $s"; done > /tmp/t3-sections.txt
      2. Assert /tmp/t3-sections.txt is empty (no missing sections)
    Expected Result: All 12 required sections exist as `## ` headings
    Failure Indicators: Any "MISSING: ..." line
    Evidence: .sisyphus/evidence/task-3-sections-check.txt

  Scenario: Forbidden flags listed verbatim (negative)
    Tool: Bash
    Preconditions: Contract written
    Steps:
      1. grep -E '\-\-privileged' docker/runtime-contract.md
      2. grep -E '\-\-cap-add=SYS_ADMIN' docker/runtime-contract.md
      3. grep -E '\-\-device=/dev/uinput' docker/runtime-contract.md
      4. grep -E '\-\-device=/dev/input' docker/runtime-contract.md
      5. grep -E '\-\-device=/dev/dri' docker/runtime-contract.md
    Expected Result: All five greps match (under "Forbidden flags" section); these are the exact 5 forbidden flag-strings the entire plan enforces
    Evidence: .sisyphus/evidence/task-3-forbidden-flags-listed.txt
  ```

  **Evidence to Capture**:
  - [ ] `.sisyphus/evidence/task-3-sections-check.txt` (empty = all sections present)
  - [ ] `.sisyphus/evidence/task-3-forbidden-flags-listed.txt` (5 lines = all 5 flags listed)

  **Commit**: YES (part of C1)
  - Message: `chore(docker): scaffold test harness directory + runtime contract`
  - Files: `docker/runtime-contract.md`
  - Pre-commit: `test -s docker/runtime-contract.md && grep -q '^## Forbidden flags' docker/runtime-contract.md`

- [x] 4. Update `.gitignore` for `.sisyphus/evidence/`

  **What to do**:
  - Add `.sisyphus/evidence/` to root `.gitignore` so generated screenshots, logs, and JSON dumps don't accidentally get committed
  - Keep `.sisyphus/plans/` and `.sisyphus/drafts/` TRACKABLE (they're documentation; don't ignore them)
  - Place the new line near the existing `.opencode/` rule for consistency

  **Must NOT do**:
  - Ignore all of `.sisyphus/` (would hide plans + drafts)
  - Add a separate `.gitignore` inside `.sisyphus/` (root one is enough)
  - Touch unrelated `.gitignore` patterns
  - Reformat the file (preserve existing line ordering)

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: 1-line edit, zero ambiguity once the rule is decided
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1
  - **Blocks**: T9 (wrapper writes evidence; want it ignored from day 1)
  - **Blocked By**: None

  **References**:

  **Pattern References**:
  - `.gitignore:1-14` — current ignore rules: `__pycache__/`, `dist/`, `.venv`, `.opencode/` etc. Add `.sisyphus/evidence/` adjacent to `.opencode/` for grouping consistency

  **External References**:
  - Git docs — https://git-scm.com/docs/gitignore — `dir/` form ignores everything under that directory recursively

  **WHY Each Reference Matters**:
  - `.gitignore:1-14` shows exactly the style/grouping convention to follow (one rule per line, blank-line separation between groups)

  **Acceptance Criteria**:

  **QA Scenarios (MANDATORY)**:
  ```
  Scenario: Evidence dir is ignored (happy path)
    Tool: Bash
    Preconditions: T4 complete; create a dummy file under .sisyphus/evidence/
    Steps:
      1. mkdir -p .sisyphus/evidence/test && echo dummy > .sisyphus/evidence/test/x.txt
      2. git status --porcelain | grep -E '^\?\? \.sisyphus/evidence/' && exit 1
      3. exit 0
    Expected Result: git does NOT report `.sisyphus/evidence/test/x.txt` as untracked
    Failure Indicators: git status shows the dummy file as `??`
    Evidence: .sisyphus/evidence/task-4-gitignore-respected.txt (output of git status piped)

  Scenario: Plans + drafts NOT accidentally ignored (negative)
    Tool: Bash
    Preconditions: T4 complete
    Steps:
      1. git check-ignore -v .sisyphus/plans/archlinux-docker-harness.md && exit 1
      2. exit 0
    Expected Result: `.sisyphus/plans/archlinux-docker-harness.md` is NOT ignored (git check-ignore exits 1 = not ignored)
    Evidence: .sisyphus/evidence/task-4-plans-not-ignored.txt
  ```

  **Evidence to Capture**:
  - [ ] `.sisyphus/evidence/task-4-gitignore-respected.txt`
  - [ ] `.sisyphus/evidence/task-4-plans-not-ignored.txt`

  **Commit**: YES (part of C1)
  - Message: `chore(docker): scaffold test harness directory + runtime contract`
  - Files: `.gitignore`
  - Pre-commit: `git check-ignore .sisyphus/evidence/ >/dev/null && ! git check-ignore .sisyphus/plans/ >/dev/null`

- [x] 5. Scaffold `docker/` directory + placeholder README

  **What to do**:
  - Create directory `docker/` if it doesn't exist
  - Write `docker/README.md` (5–10 lines) explaining the directory's role: "Docker test harnesses for verifying kwin-mcp runs on multiple Linux distros. See `runtime-contract.md` for the cross-distro contract. To add a new distro, drop in `<distro>.Dockerfile` plus matching entries in `scripts/test-distro.sh`."
  - Do NOT create any Dockerfile in this task (those belong to T6 and future plans)
  - Verify `scripts/` already exists (it does — confirmed by earlier exploration)

  **Must NOT do**:
  - Create any Dockerfile (T6's job)
  - Create `docker/smoke_test.py` (T8's job)
  - Create `docker/entrypoint.sh` (T7's job)
  - Modify `scripts/` (T9 will add `test-distro.sh`)
  - Bake any kwin-mcp source/wheel into `docker/`

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Trivial scaffolding; mkdir + 5-line README
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1
  - **Blocks**: T6, T7, T8 (they put files inside `docker/`)
  - **Blocked By**: None

  **References**:

  **Pattern References**:
  - `scripts/check_docs_seo.py:1-18` — example of a one-purpose script with a docstring header — same convention applies to `docker/README.md`'s framing

  **External References**:
  - None — purely structural scaffolding

  **WHY Each Reference Matters**:
  - check_docs_seo.py header style is the project's existing convention; the README mirrors that voice

  **Acceptance Criteria**:

  **QA Scenarios (MANDATORY)**:
  ```
  Scenario: Directory + README exist (happy path)
    Tool: Bash
    Preconditions: T5 complete
    Steps:
      1. test -d docker
      2. test -s docker/README.md
      3. wc -l docker/README.md   # expect 5-20 lines
      4. grep -q -i 'runtime-contract' docker/README.md
    Expected Result: directory exists, README is non-empty (5-20 lines), references runtime-contract
    Evidence: .sisyphus/evidence/task-5-scaffold-check.txt

  Scenario: No premature Dockerfile/script created (negative)
    Tool: Bash
    Preconditions: T5 complete; T6/T7/T8 NOT yet started
    Steps:
      1. ! test -e docker/archlinux.Dockerfile
      2. ! test -e docker/smoke_test.py
      3. ! test -e docker/entrypoint.sh
    Expected Result: only README.md and (later) runtime-contract.md may exist in docker/ at this point
    Evidence: .sisyphus/evidence/task-5-no-premature-files.txt (output of `ls docker/`)
  ```

  **Evidence to Capture**:
  - [ ] `.sisyphus/evidence/task-5-scaffold-check.txt`
  - [ ] `.sisyphus/evidence/task-5-no-premature-files.txt`

  **Commit**: YES (part of C1)
  - Message: `chore(docker): scaffold test harness directory + runtime contract`
  - Files: `docker/README.md`
  - Pre-commit: `test -d docker && test -s docker/README.md`

- [x] 7. Write `docker/entrypoint.sh`

  **What to do**:
  - Bash script that runs as PID 1 inside the container (declared via `ENTRYPOINT` in T6's Dockerfile)
  - Shebang `#!/usr/bin/env bash` + `set -euo pipefail` + `IFS=$'\n\t'`
  - Establish run dir: `EVIDENCE_DIR=/evidence/$(date -u +%Y%m%dT%H%M%SZ)`; `mkdir -p "$EVIDENCE_DIR/screenshots" "$EVIDENCE_DIR/a11y"`
  - Tee logs: redirect stdout/stderr through `tee` to `$EVIDENCE_DIR/stdout.log` and `$EVIDENCE_DIR/stderr.log` while still printing to console
  - Trap on EXIT to write a final `summary.json` skeleton (verdict=`error` if exit code > 0; smoke_test.py overwrites it with verdict=`pass` on success)
  - Find wheel: `wheel=$(ls -t /wheels/kwin_mcp-*.whl | head -1)`; if none → write summary `verdict=error`, `reason=no_wheel_found`, exit `3`
  - Install wheel into pre-existing venv (Dockerfile creates it): `uv pip install --python /opt/kwinmcp-venv/bin/python "$wheel"` → on failure write `reason=wheel_install_failed`, exit `3`
  - Record install metadata as **canonical JSON** to `$EVIDENCE_DIR/install.json` with EXACTLY these keys (T8 reads and merges this into `summary.json` under the `install` key):
    - `wheel_basename` — `basename "$wheel"` (e.g. `kwin_mcp-0.7.0-py3-none-any.whl`)
    - `wheel_sha256` — `sha256sum "$wheel" | awk '{print $1}'`
    - `kwin_mcp_version` — `/opt/kwinmcp-venv/bin/python -c "import kwin_mcp; print(kwin_mcp.__version__)"`
    - `package_versions` — JSON object mapping package name → installed version, populated from `pacman -Q kwin spectacle at-spi2-core qt6-declarative python` (parse the `name version` lines into a dict)
    - `image_tag` — value of `$KWIN_MCP_IMAGE_TAG` env var if set, else literal `"unknown"` (best-effort; not all wrappers will set it)
    Build the JSON with `python3 -c 'import json; print(json.dumps(...))' > "$EVIDENCE_DIR/install.json"` or `jq -n` — do NOT hand-write JSON (escaping bugs)
  - Export `PYTHONUNBUFFERED=1`, `EVIDENCE_DIR`
  - exec: `/opt/kwinmcp-venv/bin/python /opt/docker/smoke_test.py` (smoke_test.py is host-mounted at this path)
  - Propagate smoke_test.py exit code as container exit code

  **Must NOT do**:
  - Install kwin-mcp into system Python (must use venv at `/opt/kwinmcp-venv`)
  - Skip the EXIT trap (would lose evidence on crash)
  - Run anything as root (Dockerfile already sets `USER kwinmcp` — entrypoint runs as uid 1000)
  - Hardcode test app launch logic here (that's smoke_test.py's job)
  - Touch host's `~/.config` or any path outside `/evidence`, `/wheels`, `/opt/docker`, `/run/user/1000`, `/home/kwinmcp`
  - Source any host shell profile

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: Bash entrypoint with traps, exit-code semantics, evidence collection — needs care, not just typing
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 2 (with T6, T8, T9)
  - **Blocks**: T10 (POC end-to-end run)
  - **Blocked By**: T3 (contract), T5 (docker/ dir)

  **References**:

  **Pattern References**:
  - `scripts/check_docs_seo.py:299-329` — main entry pattern with explicit exit codes (0 success, 1 failure) — apply same discipline here
  - `scripts/sync_plugin_version.py:1-10` — header docstring style — entrypoint.sh first comment block follows same voice

  **API/Type References**:
  - `docker/runtime-contract.md` (T3) — sections "Mount paths", "Venv", "Evidence layout", "Exit code semantics" — entrypoint MUST conform exactly
  - `src/kwin_mcp/session.py:383-408` — env vars set internally by `_build_env()` — entrypoint must NOT duplicate these (kwin_mcp sets them once `session_start` is called inside smoke_test.py)

  **External References**:
  - Bash strict mode reference — http://redsymbol.net/articles/unofficial-bash-strict-mode/ — for `set -euo pipefail` rationale
  - uv venv docs — https://docs.astral.sh/uv/pip/environments/ — `uv pip install --python <venv>/bin/python` syntax

  **WHY Each Reference Matters**:
  - runtime-contract.md is THE source of truth for paths/exit codes — entrypoint is its first concrete implementor; mismatches here propagate to every other distro Dockerfile
  - session.py:383-408 prevents accidentally double-setting env vars that kwin_mcp manages itself

  **Acceptance Criteria**:

  **QA Scenarios (MANDATORY)**:
  ```
  Scenario: Syntax + shellcheck (happy path)
    Tool: Bash
    Preconditions: T7 complete
    Steps:
      1. bash -n docker/entrypoint.sh
      2. command -v shellcheck && shellcheck -S warning docker/entrypoint.sh || echo "shellcheck unavailable"
      3. grep -q '^set -euo pipefail' docker/entrypoint.sh
      4. grep -q 'trap.*EXIT' docker/entrypoint.sh
      5. grep -q 'EVIDENCE_DIR' docker/entrypoint.sh
      6. grep -q '/opt/kwinmcp-venv' docker/entrypoint.sh
    Expected Result: bash -n exits 0; strict mode + trap + venv path + EVIDENCE_DIR all present
    Failure Indicators: any grep returns non-zero
    Evidence: .sisyphus/evidence/task-7-syntax-and-structure.txt

  Scenario: Exit-code semantics implemented (failure-path)
    Tool: Bash
    Preconditions: T7 complete
    Steps:
      1. grep -E 'exit (1|2|3)' docker/entrypoint.sh   # at least one explicit non-zero exit besides 0
      2. grep -E 'wheel.*install.*fail|install_failed' docker/entrypoint.sh   # wheel install failure path documented
      3. grep -E 'no_wheel_found|wheel.*not found|ls -t /wheels' docker/entrypoint.sh   # missing-wheel path handled
    Expected Result: distinct error paths for missing wheel and install failure
    Evidence: .sisyphus/evidence/task-7-error-paths.txt
  ```

  **Evidence to Capture**:
  - [ ] `.sisyphus/evidence/task-7-syntax-and-structure.txt`
  - [ ] `.sisyphus/evidence/task-7-error-paths.txt`

  **Commit**: YES (part of C2, groups with T6, T8, T9)
  - Message: `feat(docker): arch linux smoke test harness`
  - Files: `docker/entrypoint.sh` (with executable bit set: `chmod +x`)
  - Pre-commit: `bash -n docker/entrypoint.sh && test -x docker/entrypoint.sh`

- [x] 9. Write `scripts/test-distro.sh` (host wrapper)

  **What to do**:
  - Bash script: validates argument, builds wheel, builds image, runs container, propagates exit code
  - Shebang `#!/usr/bin/env bash` + `set -euo pipefail` + `IFS=$'\n\t'`
  - Argument validation: `$1` must be one of the supported distros. For now: only `archlinux`. Future entries: `ubuntu`, `debian`, `fedora`, `opensuse`. Maintain a single bash array `SUPPORTED=(archlinux)` so adding distros = appending one element + dropping a Dockerfile.
  - On unsupported arg: print `error: distro '$1' not supported (no docker/$1.Dockerfile)` to stderr, exit `2`
  - Resolve repo root: `REPO=$(git rev-parse --show-toplevel)` (or fall back to script-relative if outside git)
  - Build wheel: `uv build --wheel --out-dir "$REPO/dist"` (does nothing extra if up-to-date)
  - Locate wheel: `wheel=$(ls -t "$REPO/dist"/kwin_mcp-*.whl | head -1)`
  - **Single Dockerfile per distro slot** (no host-arch branching; the chosen `manjarolinux/base` is multi-arch and resolves to the correct architecture layer automatically — verified at https://hub.docker.com/r/manjarolinux/base):
    ```
    dockerfile="$1.Dockerfile"   # e.g. archlinux.Dockerfile (covers both amd64 + arm64)
    test -f "$REPO/docker/$dockerfile" || { echo "error: docker/$dockerfile not found" >&2; exit 2; }
    ```
  - Build image: `docker build -f "$REPO/docker/$dockerfile" -t "kwin-mcp-test:$1" "$REPO/docker"` (Docker pulls the host-arch layer of the multi-arch base automatically; no `--platform` flag needed for native builds)
  - Prepare evidence dir: `mkdir -p "$REPO/.sisyphus/evidence/$1" && chmod 0777 "$REPO/.sisyphus/evidence/$1"` (so container uid 1000 can write regardless of host UID)
  - Run container with mounts (NO `--privileged`, NO `--cap-add`, NO `--device=...`):
    ```
    docker run --rm \
      -v "$REPO/dist:/wheels:ro" \
      -v "$REPO/docker/smoke_test.py:/opt/docker/smoke_test.py:ro" \
      -v "$REPO/docker/smoke_app.qml:/opt/docker/smoke_app.qml:ro" \
      -v "$REPO/.sisyphus/evidence/$1:/evidence" \
      kwin-mcp-test:$1
    ```
  - Exit with `docker run`'s exit code

  **Must NOT do**:
  - Use `--privileged`, `--cap-add=SYS_ADMIN`, `--device=/dev/uinput`, `--device=/dev/input`, `--device=/dev/dri` (these exact 5 flag-strings = automatic plan failure; no `--cap-add=*` of any other capability either)
  - Push the image to any registry
  - Skip the wheel build (must always rebuild — guarantees fresh code)
  - Mount the host's `~/.config`, `~/.cache`, `/var/run/dbus`, or any host XDG path
  - Hard-code the host UID; use the `chmod 0777` approach so any host UID works
  - Re-introduce `uname -m` host-arch branching, a `case "$ARCH"` block, or any `*-arm.Dockerfile` / `*-amd64.Dockerfile` selection — the chosen base (`manjarolinux/base`) is multi-arch so a single `$1.Dockerfile` covers both architectures; adding a branch would silently regress to the rejected dual-Dockerfile design
  - Add an "advanced" mode that enables forbidden flags

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: Bash wrapper with multiple stages and strict no-forbidden-flag invariant; needs care
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 2 (with T6, T7, T8)
  - **Blocks**: T10 (POC run)
  - **Blocked By**: T3 (contract), T4 (.gitignore for evidence dir)

  **References**:

  **Pattern References**:
  - `scripts/sync_plugin_version.py:1-10` — header doc style
  - `scripts/check_docs_seo.py:299-329` — exit-code discipline (`sys.exit(1)`/`sys.exit(0)` pattern translated to bash)

  **API/Type References**:
  - `docker/runtime-contract.md` (T3) — sections "Mount paths", "Forbidden flags", "User", "Exit code semantics" — wrapper enforces all of these
  - `pyproject.toml:73-75` — `[build-system] backend=uv_build` confirms `uv build --wheel` is the right invocation

  **External References**:
  - Docker run reference — https://docs.docker.com/engine/reference/run/ — for `-v` syntax and exit-code propagation semantics
  - uv build docs — https://docs.astral.sh/uv/concepts/projects/build/ — `uv build --wheel --out-dir` syntax

  **WHY Each Reference Matters**:
  - runtime-contract.md mount paths and forbidden-flags lists are the wrapper's compliance surface
  - pyproject.toml backend confirms wheel build path; if backend changes (e.g. to setuptools), wrapper command may need adjustment

  **Acceptance Criteria**:

  **QA Scenarios (MANDATORY)**:
  ```
  Scenario: Syntax + no forbidden flags + single Dockerfile per slot (happy path)
    Tool: Bash
    Preconditions: T9 complete
    Steps:
      1. bash -n scripts/test-distro.sh
      2. command -v shellcheck && shellcheck -S warning scripts/test-distro.sh || echo "shellcheck unavailable"
      3. ! grep -E '\-\-privileged|\-\-cap-add=SYS_ADMIN|\-\-device=/dev/uinput|\-\-device=/dev/input|\-\-device=/dev/dri' scripts/test-distro.sh   # exact 5 flag-strings; if ANY matches → fail
      4. grep -q 'uv build --wheel' scripts/test-distro.sh
      5. grep -q 'docker build' scripts/test-distro.sh
      6. grep -q 'docker run' scripts/test-distro.sh
      7. grep -q 'chmod 0777' scripts/test-distro.sh
      8. ! grep -q 'uname -m' scripts/test-distro.sh   # NO host-arch branching (multi-arch base handles both archs transparently)
      9. ! grep -E 'manjaro-arm\.Dockerfile|archlinux-arm\.Dockerfile|\.Dockerfile-(amd64|arm64)' scripts/test-distro.sh   # NO arch-suffixed Dockerfile names
     10. grep -qE '"\$1\.Dockerfile"|\$\{1\}\.Dockerfile|"\$1"\.Dockerfile' scripts/test-distro.sh   # single $1.Dockerfile pattern present (one Dockerfile per distro slot)
    Expected Result: bash -n passes, NO forbidden flags (all 5 exact strings absent), all required stages present, NO host-arch branching, single $1.Dockerfile resolution
    Failure Indicators: any forbidden flag, missing stage, presence of `uname -m` or arch-suffixed Dockerfile names (regression to dual-Dockerfile design)
    Evidence: .sisyphus/evidence/task-9-no-forbidden-flags.txt

  Scenario: Unsupported distro graceful failure (negative)
    Tool: Bash
    Preconditions: T9 complete; do NOT actually run docker
    Steps:
      1. scripts/test-distro.sh ubuntu 2>&1 | tee /tmp/t9-ubuntu.txt
      2. echo "exit=${PIPESTATUS[0]}" >> /tmp/t9-ubuntu.txt   # ${PIPESTATUS[0]} captures the LEFT side's exit code (tee always exits 0); plain $? would mask failures
      3. grep -qi 'not supported' /tmp/t9-ubuntu.txt
      4. grep -q 'exit=2' /tmp/t9-ubuntu.txt   # exit code 2 (env setup failure semantic) or any non-zero
    Expected Result: prints clear error mentioning "not supported", exits non-zero (preferably 2)
    Failure Indicators: silent exit 0; misleading message
    Evidence: /tmp/t9-ubuntu.txt → .sisyphus/evidence/task-9-unsupported-distro.txt
  ```

  **Evidence to Capture**:
  - [ ] `.sisyphus/evidence/task-9-no-forbidden-flags.txt`
  - [ ] `.sisyphus/evidence/task-9-unsupported-distro.txt`

  **Commit**: YES (part of C2)
  - Message: `feat(docker): arch linux smoke test harness`
  - Files: `scripts/test-distro.sh` (with `chmod +x`)
  - Pre-commit: `bash -n scripts/test-distro.sh && test -x scripts/test-distro.sh && ! grep -E '\-\-privileged|\-\-cap-add=SYS_ADMIN|\-\-device=/dev/uinput|\-\-device=/dev/input|\-\-device=/dev/dri' scripts/test-distro.sh`

- [x] 11. Write `docs/docker-testing.md`

  **What to do**:
  - Markdown doc explaining harness usage to a fresh contributor
  - Required sections in this order:
    1. **Overview**: 2-3 sentences. What the harness is. What it is NOT (CI workflow, image publishing).
    2. **Quick Start**: single command `scripts/test-distro.sh archlinux`. Pre-reqs: Docker daemon running, `uv` installed, repo checked out.
    3. **What it does**: bullet flow — host builds wheel → image build → container run → smoke test imports `kwin_mcp` → exits with smoke verdict
    4. **Evidence layout**: link to `docker/runtime-contract.md`'s "Evidence layout" section. Include sample tree.
    5. **Adding a new distro**: 5-step checklist — (1) write `docker/<distro>.Dockerfile` conforming to runtime contract, (2) add `<distro>` to `SUPPORTED` array in `scripts/test-distro.sh`, (3) run `scripts/test-distro.sh <distro>` and iterate to green, (4) update this doc's "Supported distros" list, (5) add ROADMAP entry.
    6. **Supported distros (current)**: just `archlinux` (others coming). State explicitly which distros are NOT yet supported.
    7. **Architecture**: amd64 AND arm64 supported via a SINGLE multi-arch Dockerfile. The base `manjarolinux/base:YYYYMMDD` is multi-arch (linux/amd64 + linux/arm64) and pacman-based, so one Dockerfile transparently covers both architectures and the wrapper does not branch on host arch. Note: the file is named `archlinux.Dockerfile` because it's the user-facing distro-family slot — internally the FROM line points at Manjaro because the official Arch image is amd64-only on Docker Hub. Other architectures (armv7, ppc64le, riscv64) are out of scope.
    8. **Known limitations**: software rendering only, no GPU passthrough, no elevated Docker privileges (no host-device passthrough, no kernel capability grants), no GHA integration yet, no GHCR publishing yet. (Specific flag strings are intentionally NOT spelled out here — they live in `docker/runtime-contract.md`'s "Forbidden flags" section, the single source of truth. Repeating them here would trip the F1/F4 forbidden-flag audits.)
    9. **Troubleshooting**: top 3 likely failure modes — (a) Docker daemon not running, (b) `uv` not installed, (c) the pinned `manjarolinux/base:YYYYMMDD` date-tag is no longer pullable from Docker Hub (rare but possible if the registry GCs very old tags, or if a rebuild is in flight). For each: error symptom + fix (for (c) the fix is "pick a more recent date-tag in `docker/archlinux.Dockerfile`").

  **Must NOT do**:
  - Reproduce the runtime contract here (link only — single source of truth in T3)
  - Document the smoke test's internal Python (it's an implementation detail; doc consumers don't care)
  - Recommend `--privileged` or any forbidden flag as a "workaround"
  - Reference GHA/GHCR/publishing as available now (they're deferred)
  - Use marketing language; this is a developer doc

  **Recommended Agent Profile**:
  - **Category**: `writing`
    - Reason: Pure prose; clarity and precision over code skill
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 3 (with T10 partially, T12)
  - **Blocks**: F1 (compliance audit reads docs)
  - **Blocked By**: T3 (contract to link), T10 (so we can document any actual quirks discovered during POC)

  **References**:

  **Pattern References**:
  - `CONTRIBUTING.md:17-24` — voice + structure of existing dev docs in this repo
  - `README.md:340-420` — OS-specific installation sections; same depth/voice for our distro list

  **API/Type References**:
  - `docker/runtime-contract.md` (T3) — link target for "Evidence layout" section

  **External References**:
  - None required (we're not citing external docs in this user-facing guide)

  **WHY Each Reference Matters**:
  - CONTRIBUTING.md and README.md establish the project's documentation voice; our doc must match it (no marketing fluff, file paths inline-coded, commands in fenced blocks)
  - runtime-contract.md is the canonical source — duplicating it here invites drift

  **Acceptance Criteria**:

  **QA Scenarios (MANDATORY)**:
  ```
  Scenario: All required sections present (happy path)
    Tool: Bash
    Preconditions: T11 complete
    Steps:
      1. for s in "Overview" "Quick Start" "What it does" "Evidence layout" "Adding a new distro" "Supported distros" "Architecture" "Known limitations" "Troubleshooting"; do grep -q "^## $s" docs/docker-testing.md || echo "MISSING: $s"; done > /tmp/t11-sections.txt
      2. test ! -s /tmp/t11-sections.txt   # empty file = all sections present
      3. grep -q '`scripts/test-distro.sh archlinux`' docs/docker-testing.md
      4. grep -q 'docker/runtime-contract.md' docs/docker-testing.md   # links to contract
    Expected Result: all 9 sections present, quick-start command and contract link both included
    Evidence: .sisyphus/evidence/task-11-sections-present.txt

  Scenario: No forbidden recommendations (negative)
    Tool: Bash
    Preconditions: T11 complete
    Steps:
      1. grep -E '\-\-privileged|\-\-cap-add|\-\-device=/dev/' docs/docker-testing.md && exit 1 || exit 0
      2. grep -i 'GHCR\|registry push\|github actions' docs/docker-testing.md | grep -vi 'deferred\|out of scope\|future' && exit 1 || exit 0
    Expected Result: no forbidden flags recommended; any GHA/GHCR mention only in deferred/future context
    Evidence: .sisyphus/evidence/task-11-no-forbidden-recos.txt
  ```

  **Evidence to Capture**:
  - [ ] `.sisyphus/evidence/task-11-sections-present.txt`
  - [ ] `.sisyphus/evidence/task-11-no-forbidden-recos.txt`

  **Commit**: YES (part of C4)
  - Message: `docs(docker): document test harness usage`
  - Files: `docs/docker-testing.md`
  - Pre-commit: `grep -q '## Quick Start' docs/docker-testing.md`

- [x] 12. Update `ROADMAP.md` with Arch Docker harness completion checkbox

  **What to do**:
  - Read current `ROADMAP.md` to find the appropriate milestone/section (likely a "Testing" or "Tooling" or "CI" subsection — confirm by reading the file)
  - Add a one-line entry: `- [x] Arch Linux Docker smoke test harness (local; see docs/docker-testing.md)` (mark checked because plan is delivering it)
  - If a specific "Multi-distro testing" or similar subsection doesn't exist, create a small one with this single entry plus pending entries for ubuntu/debian/fedora/opensuse marked unchecked
  - Do NOT modify any other ROADMAP entry

  **Must NOT do**:
  - Reorder or rewrite existing milestones
  - Mark unrelated items as done
  - Add entries for ubuntu/debian/fedora/opensuse as DONE (those are deferred plans, mark as `- [ ]`)
  - Add SEO-keyword stuffing (CLAUDE.md's docs-seo trigger may run, but this content is purely engineering progress)

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: 1-3 line edit
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 3
  - **Blocks**: None
  - **Blocked By**: T10 (only mark as done after POC actually passes)

  **References**:

  **Pattern References**:
  - `ROADMAP.md` (read full file to find existing checkbox style/section organization)

  **External References**:
  - None

  **WHY Each Reference Matters**:
  - ROADMAP.md's existing structure dictates where the new entry belongs and what checkbox notation to use

  **Acceptance Criteria**:

  **QA Scenarios (MANDATORY)**:
  ```
  Scenario: New entry added (happy path)
    Tool: Bash
    Preconditions: T12 complete
    Steps:
      1. grep -E 'Arch.*Docker.*harness|docker/archlinux' ROADMAP.md
      2. grep -q 'docs/docker-testing.md' ROADMAP.md
    Expected Result: at least one line mentions Arch Docker harness and links to the new doc
    Evidence: .sisyphus/evidence/task-12-roadmap-entry.txt

  Scenario: No unrelated changes (negative)
    Tool: Bash
    Preconditions: T12 complete
    Steps:
      1. git diff ROADMAP.md | grep -E '^[-+]' | grep -v '^[-+]\{3\}' | wc -l   # count changed lines
      2. Assert count is reasonable (≤ 8 changed lines = single-section addition)
    Expected Result: small, focused diff
    Evidence: .sisyphus/evidence/task-12-diff-size.txt
  ```

  **Evidence to Capture**:
  - [ ] `.sisyphus/evidence/task-12-roadmap-entry.txt`
  - [ ] `.sisyphus/evidence/task-12-diff-size.txt`

  **Commit**: YES (part of C4)
  - Message: `docs(docker): document test harness usage`
  - Files: `ROADMAP.md`
  - Pre-commit: `grep -q 'docker/archlinux\|Arch.*Docker' ROADMAP.md`

- [x] 2. Decide test app + write `docker/smoke_app.qml` (vendored QML smoke target)

  **What to do**:
  - Lock the test-app decision (per librarian research, summarized below):
    - **Chosen**: Vendored QML smoke file (`docker/smoke_app.qml`) launched via `qml6` binary
    - **Justification**: `qml6` is provided by `qt6-declarative`, which `kwin` ALREADY depends on (zero additional Arch packages); Qt Quick `Accessible` type provides deterministic AT-SPI2 names/roles; QML file is ~30 lines, lives in our repo so we fully control widget identity (no locale fragility, no upstream UI changes); native Wayland (no XWayland)
    - **Backup if `qml6` fails the POC**: `python-pyqt6` (~25.4 MiB extra) — documented in `docker/runtime-contract.md` "Test app" section as fallback only
  - Write `docker/smoke_app.qml` with EXACTLY these widgets and accessible names (smoke_test.py will target these strings):
    - `ApplicationWindow` 320×180, title "a11y smoke", `visible: true`
    - `TextField` with `Accessible.name: "Smoke entry"` and `Accessible.id: "entry-field"`
    - `Button` with `Accessible.name: "Ping button"` and `Accessible.id: "ping-button"`, text "Ping"
    - `Label` with `Accessible.name: "Status text"` and `Accessible.id: "status-text"`, text initially "ready", changes to entry text or "clicked" on button press
  - Update `docker/runtime-contract.md` "Test app" section with: name (`smoke_app.qml`), launch command (`qml6 /opt/docker/smoke_app.qml`), Arch package (none — covered by `kwin` deps), accessible name/id table (Smoke entry, Ping button, Status text)
  - The base sketch from librarian (license-free; original; refine if needed):
    ```qml
    import QtQuick
    import QtQuick.Controls

    ApplicationWindow {
        width: 320; height: 180
        visible: true
        title: "a11y smoke"
        Column {
            anchors.centerIn: parent
            spacing: 12
            TextField {
                id: entry
                width: 220
                placeholderText: "Type here"
                Accessible.id: "entry-field"
                Accessible.name: "Smoke entry"
            }
            Button {
                id: ping
                text: "Ping"
                Accessible.id: "ping-button"
                Accessible.name: "Ping button"
                onClicked: status.text = entry.text || "clicked"
            }
            Label {
                id: status
                text: "ready"
                Accessible.id: "status-text"
                Accessible.name: "Status text"
            }
        }
    }
    ```

  **Must NOT do**:
  - Add ANY new Arch package for the test app (the whole point is zero extras)
  - Use locale-sensitive strings as accessible names (English ASCII only)
  - Make the QML depend on platform-specific Qt modules beyond `QtQuick` and `QtQuick.Controls`
  - Add JavaScript logic beyond the trivial `onClicked` handler — keep it deterministic
  - Make the window invisible or off-screen (must render visibly so screenshot captures it)
  - Hardcode UI text matching anywhere — assertions go on accessible IDs/names, not display text

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: Decision-recording + small QML authoring; not deep, not visual-engineering grade, just careful execution
  - **Skills**: none
    - `visual-engineering` is overkill for a 30-line static QML; `artistry` doesn't apply

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1 (with T1, T3, T4, T5)
  - **Blocks**: T6 (needs to know if test-app pulls extra packages; answer: no), T8 (smoke_test.py targets these widget names)
  - **Blocked By**: None

  **References**:

  **Pattern References**:
  - `src/kwin_mcp/core.py:331-335` — `accessibility_tree(role="")` — smoke_test.py will use this to find our widgets by role + accessible name
  - `src/kwin_mcp/core.py:654-660` — `wait_for_element(query="Ping button", timeout_ms=5000)` — the `Accessible.name` strings we set ARE the queries

  **External References**:
  - Qt Quick Accessible type — https://doc.qt.io/qt-6/qml-qtquick-accessible.html — `Accessible.name`, `Accessible.role`, action exposure
  - Qt AT-SPI bridge implementation — https://github.com/qt/qtbase/blob/e40473cf5458f18d6321da0fdb82ed18465a3bd8/src/gui/accessible/linux/atspiadaptor.cpp#L24-L31 — proves Linux AT-SPI integration is in qtbase
  - Qt accessible bridge header — https://github.com/qt/qtbase/blob/e40473cf5458f18d6321da0fdb82ed18465a3bd8/src/gui/accessible/linux/qspiaccessiblebridge_p.h#L27-L35

  **WHY Each Reference Matters**:
  - core.py:654-660 is the smoke_test.py contract — `Accessible.name` strings here become the `query` argument there; mismatches break the whole test
  - Qt Quick Accessible doc is the authoritative spec for the QML attached properties — our QML uses exactly the documented names
  - qtbase atspiadaptor.cpp is hard evidence that Qt apps publish AT-SPI on Linux without extra setup (closes Metis's assumption #3)

  **Acceptance Criteria**:

  **QA Scenarios (MANDATORY)**:
  ```
  Scenario: QML file renders to text correctly + has all 3 accessible names (happy path)
    Tool: Bash
    Preconditions: T2 complete; docker/smoke_app.qml written
    Steps:
      1. test -s docker/smoke_app.qml
      2. grep -q 'Accessible.name: "Smoke entry"' docker/smoke_app.qml
      3. grep -q 'Accessible.name: "Ping button"' docker/smoke_app.qml
      4. grep -q 'Accessible.name: "Status text"' docker/smoke_app.qml
      5. grep -q 'import QtQuick' docker/smoke_app.qml
      6. grep -q 'import QtQuick.Controls' docker/smoke_app.qml
      7. grep -q 'visible: true' docker/smoke_app.qml
    Expected Result: file exists, 3 named widgets present, imports + visibility correct
    Failure Indicators: missing widget, missing import, hidden window
    Evidence: .sisyphus/evidence/task-2-qml-structure.txt

  Scenario: Runtime contract updated (happy path)
    Tool: Bash
    Preconditions: T2 complete; T3 file exists (T3 may be done first or T2 updates afterwards)
    Steps:
      1. grep -A 5 '^## Test app' docker/runtime-contract.md | grep -q 'smoke_app.qml'
      2. grep -A 10 '^## Test app' docker/runtime-contract.md | grep -q 'qml6'
      3. grep -A 20 '^## Test app' docker/runtime-contract.md | grep -q 'Smoke entry'
    Expected Result: contract's "Test app" section names the QML file, qml6 launcher, and at least one accessible name
    Evidence: .sisyphus/evidence/task-2-contract-updated.txt

  Scenario: No new Arch package introduced for test app (negative)
    Tool: Bash
    Preconditions: T2 complete
    Steps:
      1. grep -A 5 '^## Test app' docker/runtime-contract.md | grep -i 'pacman\s*-S\s\+\(python-pyqt6\|gtk4\|gnome-calculator\|zenity\|yad\)' && exit 1
      2. exit 0
    Expected Result: chosen option does NOT mention any extra package install
    Evidence: .sisyphus/evidence/task-2-no-extra-package.txt
  ```

  **Evidence to Capture**:
  - [ ] `.sisyphus/evidence/task-2-qml-structure.txt`
  - [ ] `.sisyphus/evidence/task-2-contract-updated.txt`
  - [ ] `.sisyphus/evidence/task-2-no-extra-package.txt`

  **Commit**: YES (part of C1)
  - Message: `chore(docker): scaffold test harness directory + runtime contract`
  - Files: `docker/smoke_app.qml`, `docker/runtime-contract.md` (updated)
  - Pre-commit: `test -s docker/smoke_app.qml && grep -q 'Smoke entry' docker/smoke_app.qml`

- [x] 6. Write `docker/archlinux.Dockerfile` (single multi-arch Dockerfile)

  **What to do**:
  - Single-stage Dockerfile (no `base-devel` needed since we don't compile anything in-image)
  - The filename is `archlinux.Dockerfile` because it's the user-facing distro-family slot in `scripts/test-distro.sh archlinux`. Internally the FROM line points at Manjaro purely because Manjaro is multi-arch (linux/amd64 + linux/arm64) AND pacman-based (Arch parity). Add a leading comment block (lines 1-6) explaining this:
    ```
    # docker/archlinux.Dockerfile - Arch-family test image (multi-arch).
    # FROM line uses manjarolinux/base because the official archlinux:base is
    # amd64-only on Docker Hub; Manjaro ships archlinux-keyring + manjaro-keyring,
    # is pacman-based, and is multi-arch (linux/amd64 + linux/arm64). One Dockerfile
    # therefore covers both architectures from the user-facing 'archlinux' slot.
    ```
  - `FROM` line: **date-tag pinned** per T1's decision. Format: `FROM manjarolinux/base:YYYYMMDD` (e.g. `FROM manjarolinux/base:20260322`). NEVER `:latest`, NEVER `:main`, NEVER `manjarolinux/base` without date suffix, NEVER `@sha256:` digest. We deliberately use date-tags for human-readable pinning and predictable rebuild cycles. The exact tag value is filled from T1's evidence note.
  - First `RUN` (mandatory ordering — note `--populate archlinux manjaro`, not just `archlinux`, because Manjaro ships both keyrings and both must be populated):
    ```
    RUN pacman-key --init \
     && pacman-key --populate archlinux manjaro \
     && pacman -Syu --noconfirm --needed \
     && pacman -S --noconfirm --needed \
          kwin spectacle at-spi2-core python-gobject dbus-python-common \
          mesa wl-clipboard wtype wayland-utils \
          python uv \
     && pacman -Scc --noconfirm \
     && rm -rf /var/cache/pacman/pkg/* /var/lib/pacman/sync/*.db
    ```
    - `kwin` transitively pulls `qt6-base`, `qt6-declarative` (provides `qml6`), `qt6-tools`, `libqaccessibilityclient-qt6`. NO need to list these explicitly.
    - `mesa` provides llvmpipe for software rendering when no `/dev/dri`.
    - NO `base-devel`, NO compilers, NO docs.
  - Locale: rely on glibc's built-in `C.UTF-8` (no locale-gen needed since glibc 2.35+); set `ENV LANG=C.UTF-8 LC_ALL=C.UTF-8`
  - User creation: `RUN groupadd -g 1000 kwinmcp && useradd -m -u 1000 -g 1000 -s /bin/bash kwinmcp`
  - XDG_RUNTIME_DIR setup: `RUN mkdir -p /run/user/1000 && chown 1000:1000 /run/user/1000 && chmod 0700 /run/user/1000`; `ENV XDG_RUNTIME_DIR=/run/user/1000`
  - Pre-create venv: `RUN install -d -o 1000 -g 1000 /opt/kwinmcp-venv && su kwinmcp -c "uv venv /opt/kwinmcp-venv"` (or equivalent — venv must be owned by kwinmcp so entrypoint can `uv pip install` into it without sudo)
  - `ENV PATH=/opt/kwinmcp-venv/bin:$PATH PYTHONUNBUFFERED=1`
  - Mountpoint dirs: `RUN install -d -o 1000 -g 1000 /opt/docker /wheels /evidence` (entrypoint mount targets exist with right ownership)
  - Copy entrypoint: `COPY --chown=1000:1000 entrypoint.sh /opt/docker/entrypoint.sh` then `RUN chmod +x /opt/docker/entrypoint.sh`
  - `WORKDIR /home/kwinmcp`
  - `USER kwinmcp`
  - `ENTRYPOINT ["/opt/docker/entrypoint.sh"]`
  - Add a HEALTHCHECK? **NO** — out of scope; harness is one-shot, not long-running

  **Must NOT do**:
  - Use `:latest`, `:main`, or any floating/unpinned base image tag — must be a specific date-tag like `manjarolinux/base:YYYYMMDD`. Also forbid `@sha256:` digest pinning (we use date-tags by policy).
  - Switch the FROM line to `archlinux:base...` — the official Arch image is amd64-only and would break the arm64 path; Manjaro is the deliberate single-base choice
  - Drop `manjaro` from `pacman-key --populate archlinux manjaro` — Manjaro ships both keyrings and both must be populated, otherwise package signature verification will fail mid-build
  - Install `base-devel`, gcc, make, or any compiler (we don't compile in-image)
  - Install `python-pyqt6`, `gtk4`, `gnome-calculator`, `zenity`, `yad`, `kate`, or any extra GUI app — `qml6` from qt6-declarative is what we use (T2)
  - Bake the kwin-mcp wheel into the image (`COPY dist/...whl` is forbidden)
  - Use `--privileged`, `--cap-add=SYS_ADMIN`, `--device=/dev/uinput`, `--device=/dev/input`, or `--device=/dev/dri` — these exact 5 flag-strings must NEVER appear in any `docker run` invocation generated by the harness, and the Dockerfile must not produce an image that requires any of them at runtime (no kernel modules loaded, no /dev/* device assumptions)
  - Run pacman with just `-Sy` (always `-Syu` to avoid partial-upgrade breakage)
  - Skip cache cleanup (image bloat)
  - Run as root in `ENTRYPOINT` (USER kwinmcp must be the last identity)
  - Add multi-stage build (single stage is sufficient and clearer)
  - Pin specific Arch package versions (Arch is rolling; pin the BASE IMAGE date-tag only, package versions follow from whatever the rolling repo has at that date)

  **Recommended Agent Profile**:
  - **Category**: `deep`
    - Reason: Many concrete decisions interact (user creation order, XDG perms, venv ownership, entrypoint path); errors compound silently
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 2 (with T7, T8, T9)
  - **Blocks**: T10 (POC needs the image)
  - **Blocked By**: T1 (FROM date-tag), T2 (test-app decision: confirms zero extra packages), T3 (contract: paths/uid/perms), T5 (docker/ exists)

  **References**:

  **Pattern References**:
  - `CONTRIBUTING.md:17-24` — exact Arch package list — Dockerfile mirrors this verbatim
  - `README.md:343-356` — same package list, same screening
  - `src/kwin_mcp/session.py:331-379` — wrapper script that boots virtual session — informs which binaries must be on PATH (kwin_wayland, dbus-run-session, at-spi-bus-launcher, dbus-update-activation-environment, dbus-send, spectacle)

  **API/Type References**:
  - `docker/runtime-contract.md` (T3) — sections "User", "Venv", "XDG_RUNTIME_DIR", "Locale", "Env vars by source" — Dockerfile is the FIRST implementor of all these clauses

  **External References**:
  - Docker Hub `archlinux` — https://hub.docker.com/_/archlinux — pacman-key init pattern
  - ArchWiki Pacman — https://wiki.archlinux.org/title/Pacman — `paccache` / `pacman -Scc` rationale
  - freedesktop XDG Basedir — https://specifications.freedesktop.org/basedir-spec/latest/ — XDG_RUNTIME_DIR mode 0700 requirement

  **WHY Each Reference Matters**:
  - CONTRIBUTING.md is the canonical package list; Dockerfile is its container-environment translation
  - session.py:331-379 dictates the binary surface; missing any of these = runtime failure with cryptic errors
  - runtime-contract.md is what enforces consistency across future distros — Dockerfile must match the contract exactly

  **Acceptance Criteria**:

  **QA Scenarios (MANDATORY)**:
  ```
  Scenario: docker build succeeds (happy path)
    Tool: Bash
    Preconditions: T6 complete; T7 entrypoint.sh exists in docker/
    Steps:
      1. docker build -f docker/archlinux.Dockerfile -t kwin-mcp-test:archlinux docker/ 2>&1 | tee .sisyphus/evidence/task-6-build.log
      2. echo "exit=${PIPESTATUS[0]}" >> .sisyphus/evidence/task-6-build.log   # capture docker build's exit code, not tee's
      3. grep -q 'exit=0' .sisyphus/evidence/task-6-build.log
      4. docker images kwin-mcp-test:archlinux --format '{{.Size}}' | tee .sisyphus/evidence/task-6-image-size.txt
    Expected Result: build exits 0; image present; size reported (informational)
    Failure Indicators: pacman key error, package not found, missing transitive dep, permission error during venv creation
    Evidence: .sisyphus/evidence/task-6-build.log, .sisyphus/evidence/task-6-image-size.txt

  Scenario: All required binaries on PATH inside container (happy path)
    Tool: Bash
    Preconditions: image built
    Steps:
      1. docker run --rm --entrypoint=bash kwin-mcp-test:archlinux -c 'for b in kwin_wayland dbus-run-session at-spi-bus-launcher dbus-update-activation-environment dbus-send spectacle qml6 wtype wl-copy wl-paste wayland-info uv python; do command -v $b || echo MISSING:$b; done' | tee .sisyphus/evidence/task-6-binaries.txt
      2. ! grep -q '^MISSING:' .sisyphus/evidence/task-6-binaries.txt
    Expected Result: every binary resolves; no MISSING lines
    Evidence: .sisyphus/evidence/task-6-binaries.txt

  Scenario: User + perms correct (happy path)
    Tool: Bash
    Preconditions: image built
    Steps:
      1. docker run --rm --entrypoint=bash kwin-mcp-test:archlinux -c 'id -u && id -g && stat -c "%a %u" /run/user/1000 && ls -ld /opt/kwinmcp-venv' > .sisyphus/evidence/task-6-perms.txt
      2. head -1 .sisyphus/evidence/task-6-perms.txt | grep -q '^1000$'
      3. grep -q '^700 1000' .sisyphus/evidence/task-6-perms.txt
    Expected Result: container runs as uid 1000 by default; XDG_RUNTIME_DIR is 0700 owned by 1000; venv owned by kwinmcp
    Evidence: .sisyphus/evidence/task-6-perms.txt

  Scenario: Forbidden patterns absent (negative)
    Tool: Bash
    Preconditions: T6 complete
    Steps:
      1. ! grep -i 'base-devel\|gcc\|make\|libtool' docker/archlinux.Dockerfile
      2. ! grep -E '^FROM manjarolinux/base:(latest|main)$|^FROM manjarolinux/base\s*$' docker/archlinux.Dockerfile   # forbid floating tag (must be manjarolinux/base:YYYYMMDD)
      3. ! grep -E '^FROM archlinux:' docker/archlinux.Dockerfile   # the rejected base must not appear (would break arm64; Manjaro is the deliberate choice)
      4. ! grep -E 'COPY .*kwin_mcp.*\.whl' docker/archlinux.Dockerfile   # no wheel bake-in
      5. ! grep -E '^USER root|^USER 0' docker/archlinux.Dockerfile   # ENTRYPOINT must not run as root
      6. ! grep -E '@sha256:' docker/archlinux.Dockerfile   # digest pinning is forbidden by policy — use date-tag instead
      7. grep -qE '^FROM manjarolinux/base:[0-9]{8}\s*$' docker/archlinux.Dockerfile   # date-tag pinned (YYYYMMDD format)
      8. grep -qE 'pacman-key --populate archlinux manjaro' docker/archlinux.Dockerfile   # both keyrings populated (Manjaro requires both)
    Expected Result: no compiler tools, no floating tag, no archlinux: base, no wheel inclusion, no root entrypoint, no @sha256 digest, FROM manjarolinux/base date-tag pinned (matches `YYYYMMDD`), both keyrings populated
    Evidence: .sisyphus/evidence/task-6-no-forbidden-patterns.txt
  ```

  **Evidence to Capture**:
  - [ ] `.sisyphus/evidence/task-6-build.log`
  - [ ] `.sisyphus/evidence/task-6-image-size.txt`
  - [ ] `.sisyphus/evidence/task-6-binaries.txt`
  - [ ] `.sisyphus/evidence/task-6-perms.txt`
  - [ ] `.sisyphus/evidence/task-6-no-forbidden-patterns.txt`

  **Commit**: YES (part of C2)
  - Message: `feat(docker): arch linux smoke test harness`
  - Files: `docker/archlinux.Dockerfile`
  - Pre-commit: `docker build -f docker/archlinux.Dockerfile -t kwin-mcp-test:archlinux docker/` (must exit 0)

- [x] 8. Write `docker/smoke_test.py`

  **What to do**:
  - Single Python file. No external test framework. Self-contained except for `kwin_mcp` (installed into venv from mounted wheel by entrypoint).
  - **CRITICAL API NOTE** (corrects an earlier draft assumption): `AutomationEngine.accessibility_tree()` and `AutomationEngine.find_ui_elements()` BOTH return **formatted text strings, NOT dicts/JSON**. Verified in `src/kwin_mcp/core.py:331-335` and `src/kwin_mcp/accessibility.py:37-74`. The internal `ElementInfo` dataclass (`src/kwin_mcp/accessibility.py:20-35`) has fields `role, name, description, states, x, y, width, height, actions, children_count, depth` — but these are NOT exposed as a Python object via the public API; they appear formatted into the returned string. We therefore extract coordinates by **regex-parsing** the `find_ui_elements()` text output (see `src/kwin_mcp/core.py:357-362` for the exact line format we parse).
  - Imports: `import sys, os, json, hashlib, time, datetime, re, pathlib`; `from kwin_mcp.core import AutomationEngine`
  - Entry point pattern:
    ```python
    EVIDENCE = pathlib.Path(os.environ["EVIDENCE_DIR"])
    summary = {
        "verdict": "error",
        "started_at": datetime.datetime.utcnow().isoformat() + "Z",
        "scenarios": [],
    }
    engine = AutomationEngine()
    try:
        run_smoke(engine)
        summary["verdict"] = "pass"
    except AssertionError as e:
        summary["verdict"] = "fail"; summary["error"] = str(e); summary["error_type"] = "assertion"
        sys.exit(1)
    except Exception as e:
        summary["verdict"] = "error"; summary["error"] = repr(e); summary["error_type"] = type(e).__name__
        sys.exit(10)
    finally:
        try: engine.session_stop()
        except Exception: pass
        # Merge install metadata captured by T7 entrypoint into summary["install"]
        install_path = EVIDENCE / "install.json"
        if install_path.exists():
            try:
                summary["install"] = json.loads(install_path.read_text())
            except Exception as ie:
                summary["install"] = {"error": f"could not parse install.json: {ie!r}"}
        else:
            summary["install"] = {"error": "install.json missing — T7 entrypoint did not write it"}
        # tasks_passed = number of scenario entries that have no "error" key
        summary["tasks_passed"] = sum(1 for s in summary.get("scenarios", []) if "error" not in s)
        (EVIDENCE / "summary.json").write_text(json.dumps(summary, indent=2))
    ```
  - **Canonical summary.json schema** (final shape after the finally block runs):
    - `verdict`: `"pass" | "fail" | "error"`
    - `started_at`: ISO-8601 UTC timestamp
    - `error` / `error_type`: present iff verdict ≠ pass
    - `scenarios`: list of `{name, result, ...}` entries (one per run_smoke step)
    - `tasks_passed`: int — count of scenarios without an `"error"` key (≥ 5 on success: session_start, launch_app, render, click, type)
    - `screenshot_sha`: `{initial, post_click, post_typing}` — three SHA-256 hex strings (must all differ)
    - `install`: `{wheel_basename, wheel_sha256, kwin_mcp_version, package_versions, image_tag}` — merged from T7's `install.json`
  - Helper functions (string parsing on real public API output — NO tree-dict-walking):
    ```python
    def sha256(p: pathlib.Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    # find_ui_elements() output line format (core.py:357-362):
    #   - [{role}] "{name}" @ ({x}, {y}, {width}x{height}) [actions: ...]
    FIND_RE = re.compile(
        r'^- \[(?P<role>[^\]]+)\] "(?P<name>[^"]+)" @ \((?P<x>\d+), (?P<y>\d+), (?P<w>\d+)x(?P<h>\d+)\)',
        re.MULTILINE,
    )

    def find_center(find_output: str, name: str) -> tuple[int, int]:
        for m in FIND_RE.finditer(find_output):
            if m.group("name") == name:
                x, y, w, h = (int(m.group(k)) for k in ("x", "y", "w", "h"))
                return x + w // 2, y + h // 2
        raise AssertionError(
            f"element not found by accessible name={name!r}\n"
            f"--- find_ui_elements output ---\n{find_output}"
        )

    # screenshot() returns "Screenshot saved: /tmp/screenshot_*.png (X.X KB)"
    SCREENSHOT_RE = re.compile(r"Screenshot saved: (?P<path>\S+)")

    def parse_screenshot_path(out: str) -> pathlib.Path:
        m = SCREENSHOT_RE.search(out)
        assert m, f"could not parse screenshot path from: {out!r}"
        return pathlib.Path(m.group("path"))

    def copy_to_evidence(src: pathlib.Path, dst_name: str) -> pathlib.Path:
        dst = EVIDENCE / "screenshots" / dst_name
        dst.write_bytes(src.read_bytes())
        return dst
    ```
  - `run_smoke(engine)` performs (using only public string-returning API):
    1. `engine.session_start(screen_width=1920, screen_height=1080)` → record return text in `summary["scenarios"]`
    2. `engine.launch_app("qml6 /opt/docker/smoke_app.qml")` → record returned text (contains PID + log path)
    3. `engine.wait_for_element(query="Ping button", timeout_ms=20000)` — raises TimeoutError if QML never renders / AT-SPI2 never publishes the tree; that exception is caught at top-level → exit 10
    3a. `engine.wait_for_element(query="Smoke entry", timeout_ms=5000)` — confirms the TextField widget is also published (T2's QML declares 3 distinct accessible names; T8 MUST verify all 3 exist before proceeding)
    3b. `engine.wait_for_element(query="Status text", timeout_ms=5000)` — confirms the Label widget is also published (third of T2's declared 3 accessible names: "Smoke entry", "Ping button", "Status text")
    4. `tree_before = engine.accessibility_tree(max_depth=10)` (string). Write to `EVIDENCE/a11y/before.txt`
    5. `find_before = engine.find_ui_elements(query="Ping button")` (string). `bx, by = find_center(find_before, "Ping button")`
    6. `out = engine.screenshot()`; `src = parse_screenshot_path(out)`; `initial = copy_to_evidence(src, "initial.png")`; `assert initial.stat().st_size > 1024, "initial screenshot suspiciously small"`; `initial_sha = sha256(initial)`
    7. `engine.mouse_click(x=bx, y=by)` → record return text
    8. `time.sleep(0.3)` (sole settle tick #1 — sub-second; NOT a UI poll)
    9. `out = engine.screenshot()`; `post_click = copy_to_evidence(parse_screenshot_path(out), "post-click.png")`; `post_click_sha = sha256(post_click)`. **Assert `post_click_sha != initial_sha`** — proves the click changed pixels (Status label text changed from "ready" to "clicked", which IS a pixel-level change even though we never string-match on display text)
    10. `find_entry = engine.find_ui_elements(query="Smoke entry")`; `ex, ey = find_center(find_entry, "Smoke entry")`
    11. `engine.mouse_click(x=ex, y=ey)` (focus the entry)
    12. `time.sleep(0.2)` (settle tick #2)
    13. `engine.keyboard_type("hello")` → record return text
    14. `time.sleep(0.3)` (settle tick #3)
    15. `out = engine.screenshot()`; `post_typing = copy_to_evidence(parse_screenshot_path(out), "post-typing.png")`; `post_typing_sha = sha256(post_typing)`. **Assert `post_typing_sha != post_click_sha`** — proves keyboard input changed pixels
    16. `tree_after = engine.accessibility_tree(max_depth=10)` (string). Write to `EVIDENCE/a11y/after.txt`
    17. **Top-level assertions** (string-equality + pixel-hash inequality, NOT field-key-walking on dicts that don't exist):
        - `assert tree_after != tree_before, "accessibility tree text did not change"` — overall AT-SPI2 surface differs (proves at least one accessible attribute moved)
        - `assert len({initial_sha, post_click_sha, post_typing_sha}) == 3, "screenshots not all distinct"` — three distinct rendered states
        - Together these prove input reached the app at BOTH the AT-SPI2 layer AND the rendering layer
    18. Record `summary["screenshot_sha"] = {"initial": ..., "post_click": ..., "post_typing": ...}`; append a `summary["scenarios"]` entry per step with name + result + (where parsed) coords/PID
  - File size target: ~120-180 lines including comments. No deps beyond Python stdlib + `kwin_mcp`.

  **Must NOT do**:
  - Use `time.sleep(N)` as a UI poll — use `wait_for_element` for UI state. Only three sub-second settle ticks (0.3, 0.2, 0.3) are allowed for input-event flushing
  - Reference fields named `accessible_name`, `value`, or `children` — these are NOT in `ElementInfo` (real fields: `name, role, description, states, x, y, width, height, actions, children_count, depth`) and the public string API doesn't expose any of them as dict keys anyway
  - Treat the return of `accessibility_tree()` or `find_ui_elements()` as a dict / JSON object — both return STRINGS; parse with regex or string operations only
  - Match on UI display text content (e.g. assert "ready" or "clicked" appears in the tree string) — matching is on **accessible name** (which we set deterministically via `Accessible.name` in QML) and on **pixel-hash inequality**
  - Shell out to `kwin-mcp-cli` (the whole point is direct in-process API)
  - Modify `src/kwin_mcp/` (read-only consumer)
  - Silently catch + drop exceptions — every catch must record to `summary["error"]`
  - Skip `engine.session_stop()` on failure (must run in `finally`)
  - Hardcode pixel coordinates — must come from `find_ui_elements` regex parse
  - Use `pytest`, `unittest`, or any test framework
  - Add new pip dependencies (Python stdlib + the `kwin_mcp` wheel ONLY)
  - Use locale-translated strings as accessible-name queries — our QML uses ASCII English names (`"Smoke entry"`, `"Ping button"`, `"Status text"`) which we fully control
  - Reference any of the 5 forbidden runtime flag-strings (`--privileged`, `--cap-add=SYS_ADMIN`, `--device=/dev/uinput`, `--device=/dev/input`, `--device=/dev/dri`) — smoke_test.py is in-process Python so it does not invoke `docker run`, but it MUST NOT shell-out, subprocess, or document any of these strings even as comments; consistency check across the whole plan

  **Recommended Agent Profile**:
  - **Category**: `deep`
    - Reason: Multi-step state machine with strict invariants (no string matching, no sleep polls, structured evidence, error-path coverage); subtle bugs in tree traversal would silently false-pass
  - **Skills**: none
    - `visual-engineering` does not apply (no UI authoring); `artistry` does not apply

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 2 (with T6, T7, T9)
  - **Blocks**: T10
  - **Blocked By**: T2 (accessible names of widgets), T3 (contract: env vars, evidence layout), T5 (docker/ exists)

  **References**:

  **Pattern References**:
  - `src/kwin_mcp/core.py:170-179` — `session_start` signature and defaults
  - `src/kwin_mcp/core.py:331-335` — `accessibility_tree(app_name, max_depth, role)` shape — informs `find_node` traversal
  - `src/kwin_mcp/core.py:654-660` — `wait_for_element(query, app_name, timeout_ms, poll_interval_ms, expected_states)` — replaces all `sleep` polls
  - `src/kwin_mcp/core.py:696-701` — `launch_app(command, env=None) -> {pid, log_path}` — used to spawn qml6
  - `src/kwin_mcp/core.py:703-713` — `list_windows`, `focus_window` if needed for window targeting

  **API/Type References**:
  - `docker/smoke_app.qml` (T2) — accessible names "Smoke entry", "Ping button", "Status text" — these are the ONLY strings smoke_test.py matches against
  - `docker/runtime-contract.md` (T3) — env `EVIDENCE_DIR`, exit codes 0/1/10, evidence layout

  **External References**:
  - None required — using only kwin_mcp public API + Python stdlib

  **WHY Each Reference Matters**:
  - core.py:654-660 dictates we MUST use `wait_for_element` not `sleep`-loops; following it closes Metis's flaky-timing concern
  - smoke_app.qml accessible names are the contract surface — divergence between T2's QML and T8's queries breaks everything
  - core.py:696-701 returns the launched PID, which we record in summary for forensics

  **Acceptance Criteria**:

  **QA Scenarios (MANDATORY)**:
  ```
  Scenario: Static checks pass (happy path)
    Tool: Bash
    Preconditions: T8 complete
    Steps:
      1. python -m py_compile docker/smoke_test.py
      2. uv run ruff check docker/smoke_test.py
      3. grep -q 'from kwin_mcp.core import AutomationEngine' docker/smoke_test.py
      4. grep -q 'wait_for_element' docker/smoke_test.py
      5. grep -q 'find_ui_elements' docker/smoke_test.py
      6. grep -q 'accessibility_tree' docker/smoke_test.py
      7. grep -q 'EVIDENCE_DIR' docker/smoke_test.py
      8. grep -q 'finally' docker/smoke_test.py
      9. grep -q 'session_stop' docker/smoke_test.py
     10. grep -q '"Ping button"' docker/smoke_test.py     # all 3 of T2's accessible names must appear
     11. grep -q '"Smoke entry"' docker/smoke_test.py
     12. grep -q '"Status text"' docker/smoke_test.py
    Expected Result: compiles, ruff passes, all required structural elements present, all 3 declared accessible names from T2's QML appear in smoke_test.py
    Evidence: .sisyphus/evidence/task-8-static-checks.txt

  Scenario: No forbidden patterns / no nonexistent-API field references (negative)
    Tool: Bash
    Preconditions: T8 complete
    Steps:
      1. ! grep -E 'time\.sleep\([0-9]+\)' docker/smoke_test.py | grep -vE 'time\.sleep\(0\.[0-5]\)'   # no big sleeps; only sub-second settle ticks allowed
      2. ! grep -E 'subprocess.*kwin-mcp-cli|os\.system' docker/smoke_test.py   # no CLI shell-out
      3. ! grep -E 'import pytest|import unittest' docker/smoke_test.py   # no test framework
      4. ! grep -E '\["children"\]|\.get\("children"\)' docker/smoke_test.py   # accessibility_tree returns a string; "children" is not a dict key in our public API
      5. ! grep -E '"accessible_name"|\.accessible_name' docker/smoke_test.py   # not a real field name (real ElementInfo field is "name")
      6. ! grep -E '\["value"\]|\.get\("value"\)' docker/smoke_test.py   # "value" is not in ElementInfo
      7. ! grep -E '"ready"|"clicked"' docker/smoke_test.py   # no display-text matching (we control accessible names via Accessible.name in QML)
    Expected Result: no big sleeps, no CLI shell-out, no test framework, no nonexistent-API field references, no display-text matching
    Evidence: .sisyphus/evidence/task-8-no-forbidden-patterns.txt

  Scenario: find_center regex parses real find_ui_elements format (happy path)
    Tool: Bash
    Preconditions: T8 complete
    Steps:
      1. cat > /tmp/t8-find-center.py <<'PYEOF'
import sys
sys.path.insert(0, "docker")
from smoke_test import find_center
sample = """Found 1 elements matching query='Ping button':

- [push button] "Ping button" @ (140, 90, 60x30) [actions: press]"""
cx, cy = find_center(sample, "Ping button")
assert cx == 170 and cy == 105, f"got ({cx},{cy}), expected (170,105)"
print("ok")
PYEOF
      2. python /tmp/t8-find-center.py 2>&1 | tee .sisyphus/evidence/task-8-find-center-fixture.txt
      3. grep -q '^ok' .sisyphus/evidence/task-8-find-center-fixture.txt
    Expected Result: regex correctly extracts center coordinates from the real string format documented at src/kwin_mcp/core.py:357-362
    Failure Indicators: regex doesn't match the real format → smoke test will fail at runtime with "element not found"
    Evidence: .sisyphus/evidence/task-8-find-center-fixture.txt
  ```

  **Evidence to Capture**:
  - [ ] `.sisyphus/evidence/task-8-static-checks.txt`
  - [ ] `.sisyphus/evidence/task-8-no-forbidden-patterns.txt`
  - [ ] `.sisyphus/evidence/task-8-find-center-fixture.txt`

  **Commit**: YES (part of C2)
  - Message: `feat(docker): arch linux smoke test harness`
  - Files: `docker/smoke_test.py`
  - Pre-commit: `python -m py_compile docker/smoke_test.py && uv run ruff check docker/smoke_test.py`

- [x] 10. End-to-end POC: run `scripts/test-distro.sh archlinux`, debug, iterate to green

  **What to do**:
  - Goal: prove the assembled harness ACTUALLY runs from a clean checkout. This is the proof Metis demanded — assumptions become verified facts here.
  - On a workstation with Docker daemon running: `cd <repo>`; `scripts/test-distro.sh archlinux`
  - First run is EXPECTED to fail in some way. Debug systematically:
    1. If `uv build` fails → fix `pyproject.toml` issue (must NOT touch src/) — but this should not occur on a known-good tree
    2. If `docker build` fails → inspect `.sisyphus/evidence/task-6-build.log` patterns; common: missing pacman key (forgot `manjaro` in `pacman-key --populate archlinux manjaro`), wrong/expired date-tag (try a more recent one from Docker Hub), package rename, multi-arch manifest mismatch (rare — a tag missing the arm64 layer). Fix `docker/archlinux.Dockerfile` and re-run.
    3. If `docker run` exits non-zero before smoke runs → inspect `entrypoint.sh` paths, venv perms, wheel mount. Fix `docker/entrypoint.sh` and re-run.
    4. If smoke_test.py crashes → inspect `.sisyphus/evidence/archlinux/<ts>/{stdout,stderr}.log`. Common patterns:
       - `dbus-run-session: command not found` → wrong package; install `dbus`
       - `kwin_wayland: failed to start` with no output → check XDG_RUNTIME_DIR perms; check whether software rendering needed `LIBGL_ALWAYS_SOFTWARE=1`
       - `qml6: command not found` → kwin's qt6-declarative dep not pulled correctly; install explicitly
       - AT-SPI2 tree empty → at-spi-bus-launcher race; smoke_test.py's `wait_for_element` should already cover this with longer timeout
       - `mouse_click` had no effect → libei socket binding race; check session.py:410-420 socket wait logic
    5. Iterate until exit 0
  - Document every fix made (what was wrong, what was changed in which file) in `.sisyphus/evidence/task-10-debug-log.md` — these become T11 doc inputs
  - Verify SECOND run also exits 0 (idempotency)
  - Verify evidence shape matches contract (summary.json verdict=pass, 3 screenshots > 1KB with all 3 SHAs distinct, both `a11y/{before,after}.txt` exist and differ)

  **Must NOT do**:
  - Add `--privileged` or any forbidden flag as a "quick fix" — fix the actual cause
  - Modify `src/kwin_mcp/` to make the test pass — the test must work with the existing source (read-only consumer)
  - Skip the second-run idempotency check
  - Mark task complete if first run fails but tests "would probably pass next time"
  - Commit broken intermediate state — commits happen only after green
  - Ignore screenshots that are blank (must verify pixels were captured)
  - Suppress evidence on success (evidence required on success too — that's the WHOLE point)

  **Recommended Agent Profile**:
  - **Category**: `deep`
    - Reason: Open-ended debugging across Docker, Wayland, AT-SPI2, libei surfaces — needs autonomous problem-solving and willingness to read kwin-mcp source for context
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: NO (sequential — gates Wave 3 docs/ROADMAP)
  - **Parallel Group**: Wave 3 (alone in critical path; T11/T12 can run after this)
  - **Blocks**: F1, F2, F3, F4 (final review)
  - **Blocked By**: T6, T7, T8, T9 (need full harness assembled — single Dockerfile resolves multi-arch automatically; POC runs end-to-end on host arch)

  **References**:

  **Pattern References**:
  - `src/kwin_mcp/session.py:148-185` — virtual session boot sequence — debugging session-start failures starts here
  - `src/kwin_mcp/session.py:331-379` — wrapper script — when "kwin won't start" debug, read this to understand expected flow
  - `src/kwin_mcp/session.py:410-420` — socket wait logic — when "session never ready" appears, this is the polling code

  **API/Type References**:
  - `docker/runtime-contract.md` (T3) — exit code semantics — match observed exits to documented meaning

  **External References**:
  - libei issues — https://gitlab.freedesktop.org/libinput/libei/-/issues — search for known container failure modes
  - KWin invent issues — https://invent.kde.org/plasma/kwin/-/issues — search for `--virtual` + container reports

  **WHY Each Reference Matters**:
  - session.py:148-185 + 331-379 + 410-420 are the ENTIRE virtual-session lifecycle; ~90% of POC failures will trace back to one of these flows; reading them first saves hours of guessing
  - libei/kwin issue trackers contain almost-identical container reports — patterns there often point at the right fix

  **Acceptance Criteria**:

  **QA Scenarios (MANDATORY)**:
  ```
  Scenario: Single golden command exits 0 (happy path)
    Tool: Bash
    Preconditions: T6-T9 complete; Docker daemon running; clean working tree
    Steps:
      1. scripts/test-distro.sh archlinux 2>&1 | tee .sisyphus/evidence/task-10-run1.log
      2. echo "exit=${PIPESTATUS[0]}" >> .sisyphus/evidence/task-10-run1.log   # capture wrapper's exit code, not tee's
      3. grep -q '^exit=0' .sisyphus/evidence/task-10-run1.log
    Expected Result: exit 0
    Failure Indicators: any non-zero exit; debug log captures the actual error
    Evidence: .sisyphus/evidence/task-10-run1.log

  Scenario: Evidence shape matches contract (happy path)
    Tool: Bash
    Preconditions: run completed
    Steps:
      1. latest=$(ls -td .sisyphus/evidence/archlinux/*/ | head -1)
      2. test -f "$latest/summary.json"
      3. test -f "$latest/stdout.log"
      4. test -f "$latest/stderr.log"
      5. test $(stat -c '%s' "$latest/screenshots/initial.png") -gt 1024
      6. test $(stat -c '%s' "$latest/screenshots/post-click.png") -gt 1024
      7. test $(stat -c '%s' "$latest/screenshots/post-typing.png") -gt 1024
      8. jq -e '.verdict == "pass"' "$latest/summary.json"
      9. test -s "$latest/a11y/before.txt"   # accessibility_tree returns a STRING; we store the formatted text
     10. test -s "$latest/a11y/after.txt"
     11. # all 3 screenshot SHAs must be distinct
     12. test $(sha256sum "$latest/screenshots/"{initial,post-click,post-typing}.png | awk '{print $1}' | sort -u | wc -l) -eq 3
    Expected Result: every required file present, screenshots non-trivial AND all 3 SHAs distinct, both a11y txt files non-empty, verdict=pass
    Evidence: .sisyphus/evidence/task-10-evidence-shape.txt

  Scenario: Idempotency — second run also exits 0 (happy path)
    Tool: Bash
    Preconditions: first run succeeded
    Steps:
      1. scripts/test-distro.sh archlinux 2>&1 | tee .sisyphus/evidence/task-10-run2.log
      2. echo "exit=${PIPESTATUS[0]}" >> .sisyphus/evidence/task-10-run2.log   # idempotency: same trick
      3. grep -q '^exit=0' .sisyphus/evidence/task-10-run2.log
    Expected Result: exit 0; new timestamped evidence dir created (does not overwrite previous)
    Evidence: .sisyphus/evidence/task-10-run2.log

  Scenario: A11y tree text actually changed between before and after (negative-on-false-positive)
    Tool: Bash
    Preconditions: run completed
    Steps:
      1. latest=$(ls -td .sisyphus/evidence/archlinux/*/ | head -1)
      2. ! diff -q "$latest/a11y/before.txt" "$latest/a11y/after.txt"   # they MUST differ; if identical, input never reached app
    Expected Result: before.txt and after.txt differ (proves input reached app at AT-SPI2 layer)
    Failure Indicators: identical files = false-positive smoke pass = test is broken even though exit was 0
    Evidence: .sisyphus/evidence/task-10-a11y-diff-confirmed.txt

  Scenario: Debug log captures any fixes made (informational)
    Tool: Bash
    Preconditions: T10 complete
    Steps:
      1. test -f .sisyphus/evidence/task-10-debug-log.md
      2. wc -l .sisyphus/evidence/task-10-debug-log.md
    Expected Result: debug log exists (may be short if no fixes were needed; should at least state "first-run green, no fixes needed")
    Evidence: .sisyphus/evidence/task-10-debug-log.md
  ```

  **Evidence to Capture**:
  - [ ] `.sisyphus/evidence/task-10-run1.log`
  - [ ] `.sisyphus/evidence/task-10-evidence-shape.txt`
  - [ ] `.sisyphus/evidence/task-10-run2.log`
  - [ ] `.sisyphus/evidence/task-10-a11y-diff-confirmed.txt`
  - [ ] `.sisyphus/evidence/task-10-debug-log.md`

  **Commit**: CONDITIONAL (part of C3 only if fixes to C2 files were needed)
  - Message: `test(docker): verify arch linux smoke harness end-to-end`
  - Files: any C2 file that was patched during T10 debugging
  - Pre-commit: full `scripts/test-distro.sh archlinux` exits 0

<!-- TASKS_INSERTION_POINT -->

---

## Final Verification Wave (MANDATORY — after ALL implementation tasks)

> 4 review agents run in PARALLEL. ALL must APPROVE. Present consolidated results to user and get explicit "okay" before completing.
> **Do NOT auto-proceed after verification. Wait for user's explicit approval before marking work complete.**
> **Never mark F1-F4 as checked before getting user's okay.** Rejection or user feedback → fix → re-run → present again → wait for okay.

- [x] F1. **Plan Compliance Audit** — `oracle`
  Read this plan end-to-end. For each "Must Have": verify implementation exists (read file, run command). For each "Must NOT Have": grep/inspect for forbidden patterns — reject with file:line if found. Verify the **exact 5 forbidden flag-strings** `--privileged`, `--cap-add=SYS_ADMIN`, `--device=/dev/uinput`, `--device=/dev/input`, `--device=/dev/dri` are NOT present in any **runtime-affecting** file. Run: `! grep -rE --include='*.sh' --include='*.Dockerfile' --include='Dockerfile' --include='*.py' --include='*.qml' '\-\-privileged|\-\-cap-add=SYS_ADMIN|\-\-device=/dev/uinput|\-\-device=/dev/input|\-\-device=/dev/dri' scripts/ docker/ docs/` (zero matches required). This audit DELIBERATELY OMITS `*.md` files: `docker/runtime-contract.md` lists the flag strings verbatim by design as the single source of truth (T3 verifies their *presence* there), and `docs/docker-testing.md` uses generic wording per T11 fix. F1 audits only files that run (shell, Dockerfile, Python, QML). Verify no file under `src/kwin_mcp/` was modified (`git diff src/kwin_mcp/` must be empty). Verify no `.github/workflows/*.yml` was added/modified. Verify no GHCR push commands exist anywhere. Compare deliverables 1-8 against actual repo state.
  Output: `Must Have [N/N] | Must NOT Have [N/N] | Tasks [N/N] | Forbidden flags [CLEAN/N matches] | VERDICT: APPROVE/REJECT`

- [x] F2. **Code Quality Review** — `unspecified-high`
  Run `bash -n docker/entrypoint.sh scripts/test-distro.sh` (syntax). Run `shellcheck docker/entrypoint.sh scripts/test-distro.sh` if available. Run `python -m py_compile docker/smoke_test.py`. Run `uv run ruff check docker/smoke_test.py` (use the project's existing ruff config). Run `uv run ty check docker/smoke_test.py` (will likely flag dynamic imports — acceptable if `# type: ignore` is justified). Inspect the single Dockerfile (`docker/archlinux.Dockerfile`) for: pinned date-tag (NOT digest pinning — `@sha256:` is forbidden by policy; correct format is `manjarolinux/base:YYYYMMDD`), no `:latest`/`:main` or other floating tags, no `archlinux:base...` reintroduction (rejected base), `pacman-key --populate archlinux manjaro` (both keyrings), single `RUN` for pacman with cache cleanup, no leaked secrets, no UID/GID hardcoded outside user creation. Inspect `scripts/test-distro.sh` for: no `uname -m` branching (would regress to dual-Dockerfile design), single `$1.Dockerfile` resolution. Inspect smoke_test.py for: no `time.sleep` polls (must use `wait_for_element`), no string-matching on UI text, no shell-out to `kwin-mcp-cli`, evidence written before any potential failure point.
  Output: `Bash syntax [PASS/FAIL] | shellcheck [PASS/FAIL] | py_compile [PASS/FAIL] | ruff [PASS/FAIL] | ty [PASS/FAIL] | Dockerfile audit [N issues] | wrapper audit [N issues] | smoke_test.py audit [N issues] | VERDICT`

- [x] F3. **Real Manual QA** — `unspecified-high`
  From a clean working tree, run `scripts/test-distro.sh archlinux` (single command). Verify exit code is 0. Verify `.sisyphus/evidence/archlinux/<latest>/` contains `summary.json`, `stdout.log`, `stderr.log`, three screenshots > 1 KB each (`initial.png`, `post-click.png`, `post-typing.png`), `a11y/before.txt`, `a11y/after.txt` (text dumps of the formatted accessibility-tree strings — NOT JSON, since `accessibility_tree()` returns `str` per `src/kwin_mcp/core.py:331-335`). Parse `summary.json`: `verdict` must be `"pass"`. Run `diff -q a11y/before.txt a11y/after.txt`: files MUST differ (proves AT-SPI2 surface changed → input reached the app). Compare the three screenshots' SHA-256: all three hashes MUST be distinct (proves three distinct rendered states). Re-run the script a SECOND time: must still exit 0 (proves idempotency). Run `docker images | grep kwin-mcp-test`: image present. Run `docker ps -a | grep kwin-mcp-test`: container cleaned up (no zombies). Run `! grep -E '\-\-privileged|\-\-cap-add=SYS_ADMIN|\-\-device=/dev/uinput|\-\-device=/dev/input|\-\-device=/dev/dri' scripts/test-distro.sh` (zero matches required).
  Output: `Exit code [0/non-0] | Evidence files [N/N] | Screenshot SHA distinct [PASS/FAIL] | A11y text diff [PASS/FAIL] | Idempotency [PASS/FAIL] | Container cleanup [PASS/FAIL] | Forbidden flags [CLEAN/N matches] | VERDICT`

- [x] F4. **Scope Fidelity Check** — `deep`
  For each task T1-T12: read "What to do", read git diff for the files it claims to touch. Verify 1:1 — everything in spec was built (no missing), nothing beyond spec was built (no creep). Specifically verify NO files under `src/kwin_mcp/` were touched. Verify NO `.github/workflows/*` was modified. Verify no `tests/` directory was created. Verify no `pyproject.toml` modifications (no new deps were added to runtime). Verify the only `pyproject.toml`-touching change (if any) is in `[dependency-groups.dev]` if at all (and even that is unlikely — most likely no pyproject changes). Detect cross-task contamination: e.g. T6 (Dockerfile) editing T8 (smoke_test.py). **Independent forbidden-flag audit** (runtime files only): run `grep -rE --include='*.sh' --include='*.Dockerfile' --include='Dockerfile' --include='*.py' --include='*.qml' '\-\-privileged|\-\-cap-add=SYS_ADMIN|\-\-device=/dev/uinput|\-\-device=/dev/input|\-\-device=/dev/dri' scripts/ docker/ docs/` — must produce zero lines. This deliberately omits `*.md` documentation (which legitimately lists the strings in runtime-contract.md per T3, and uses generic wording in docs/docker-testing.md per T11). F4 only audits files that actually execute.
  Output: `Tasks [N/N compliant] | Contamination [CLEAN/N issues] | Unaccounted [CLEAN/N files] | Forbidden flags [CLEAN/N rogue matches] | VERDICT`

---

## Approved Scope Expansions (Round-2)

> Round 1 of F1-F4 surfaced 5 implementation realities that the original plan did not anticipate. After F3 PROVED the harness works end-to-end (verdict=pass on two independent runs), and given the m0207 user precedent for "PR-worthy SDK fixes benefiting CI/headless/container users", the following expansions are explicitly approved as plan amendments. F1, F2, F4 reviewers MUST honor these waivers in Round 2 audits.

### Waiver A: Dockerfile may install `gcc pkgconf` (T6)
**Why**: kwin-mcp's runtime dep `dbus-python>=1.3.2` is a PyPI source-only package (no binary wheel exists). When `uv pip install` resolves the wheel inside the container, it must build dbus-python from source, which requires a C compiler and pkg-config. This is an ECOSYSTEM CONSTRAINT, not a discretionary scope choice. The minimum footprint required is `gcc + pkgconf` only; `base-devel` (full toolchain) remains forbidden.
**Bound**: Only `gcc pkgconf` permitted. `base-devel`, `make`, `libtool`, `binutils-extras`, `autoconf`, `automake` remain forbidden by Plan T6 Must NOT (line 1078).

### Waiver B (REVERTED — no longer applicable)
The conditional `--device /dev/dri/renderD128/129` passthrough block in `scripts/test-distro.sh` is REVERTED in Round 2. Software rendering via `LIBGL_ALWAYS_SOFTWARE=1 + GALLIUM_DRIVER=llvmpipe` (set in `src/kwin_mcp/session.py` per Waiver C below) is sufficient and is the canonical guardrail-compliant path.

### Waiver C: src/kwin_mcp/ modifications (T10 PR-worthy SDK fixes)
**Plan Must NOT line 117** says no file under `src/kwin_mcp/` may be modified. m0207 user pre-authorized a NARROW exception (3 specific session.py changes). Round-1 review revealed commit `4871368` exceeded that authorization with additional PR-worthy SDK fixes. Each is approved here as plan amendment:

1. **`src/kwin_mcp/session.py` — kded6/kglobalacceld guards** (lines 354-364, m0207 authorized)
   - KWin 6.6 hangs in headless without StatusNotifierWatcher host (kded6) and KGlobalAccel registrar (kglobalacceld). Each guarded with `command -v` for graceful degradation on non-Manjaro distros.

2. **`src/kwin_mcp/session.py` — socket path double-prefix fix x2** (lines ~159, ~375, m0207 authorized)
   - `f"{xdg}/wayland-mcp-1-{self.socket_name}"` was double-prefixed; fix to `f"{xdg}/{self.socket_name}"`.

3. **`src/kwin_mcp/session.py` — env var hygiene** (NEW expansion):
   - Removed `KDE_FULL_SESSION` and `KDE_SESSION_VERSION` (CI/headless contexts should NOT claim a full KDE session — caused subtle KWin behavior).
   - Added `LIBGL_ALWAYS_SOFTWARE=1` and `GALLIUM_DRIVER=llvmpipe` (forces software OpenGL when no GPU is exposed; works on any host).

4. **`src/kwin_mcp/session.py` — robustness improvements** (NEW expansion):
   - `select()` readiness loop replaces blind sleep-poll for KWin socket appearance.
   - kwin stderr redirect/deadlock handling avoids zombie children when KWin crashes early.

5. **`src/kwin_mcp/screenshot.py` — D-Bus method correction** (NEW expansion):
   - `CaptureActiveScreen` → `CaptureWorkspace`. CaptureActiveScreen returns a blank image in virtual sessions because there is no "active screen" concept — the workspace itself is the only renderable surface. CaptureWorkspace is the correct KWin ScreenShot2 D-Bus method for virtual/headless sessions. This is a pure SDK bug fix.

**Bound**: No further `src/kwin_mcp/` changes beyond the above 5 items. F1/F4 must verify diff stays at exactly these 5 items.

### Waiver D: smoke_test.py 1.5-second sleeps x3 (T8)
**Plan T8 Must NOT line 1289** restricted `time.sleep(N)` polls to sub-second settle ticks (`0.3, 0.2, 0.3`). Round-1 review found three `time.sleep(1.5)` calls at `docker/smoke_test.py:159, 176, 181`.
**Why**: These are NOT accessible-element waits (which use `wait_for_element`). They are pixel-rendering completion waits — after AT-SPI screen-offset detection scans the initial screenshot for the QML window's white-pixel band, the smoke runner needs the window's repaint cycle to finish before the next screenshot. `wait_for_element` operates on the AT-SPI tree (already populated long before pixel rendering completes) and cannot detect rendering-completion. There is no public KWin API to wait for "frame rendered".
**Bound**: Maximum 3 occurrences of `time.sleep(1.5)` allowed in `smoke_test.py`, ONLY for rendering-completion settle. Any additional or longer sleeps remain forbidden. Sub-second settle ticks (0.3, 0.2, 0.3) for input-event flushing also remain in scope.

### Waiver scope summary
- Plan **Must Have line 106** (`ONLY listed packages`): superseded by Waiver A for `gcc pkgconf` only.
- Plan **Must NOT line 117** (`no src/kwin_mcp/ modifications`): superseded by Waiver C for the 5 enumerated changes only.
- Plan **T8 Must NOT line 1289** (`no big sleeps`): superseded by Waiver D for 3 enumerated 1.5-second rendering-settle sleeps only.
- Plan **Must NOT line 119** (`no --device=/dev/dri`): UPHELD; Waiver B section above documents the revert of the temporary render-node passthrough that triggered Round-1 rejection.

---

## Commit Strategy

> Single commit per logical unit. Conventional Commits style.

- **C1** (after T1-T5): `chore(docker): scaffold test harness directory + runtime contract` — files: `docker/runtime-contract.md`, `docker/README.md`, `docker/smoke_app.qml`, `.gitignore`. Pre-commit: `bash -n` n/a, file existence checks.
- **C2** (after T6-T9): `feat(docker): arch linux smoke test harness` — files: `docker/archlinux.Dockerfile`, `docker/entrypoint.sh`, `docker/smoke_test.py`, `scripts/test-distro.sh`. Pre-commit: `bash -n docker/entrypoint.sh scripts/test-distro.sh`, `python -m py_compile docker/smoke_test.py`, `uv run ruff check docker/smoke_test.py`, AND `grep -qE '^FROM manjarolinux/base:[0-9]{8}' docker/archlinux.Dockerfile && ! grep -q 'uname -m' scripts/test-distro.sh` (single multi-arch Dockerfile pattern, no host-arch branching).
- **C3** (after T10): `test(docker): verify arch linux smoke harness end-to-end` — files: maybe small bug-fix tweaks to C2 files; if no fixes needed, no commit. Pre-commit: full `scripts/test-distro.sh archlinux` run exits 0.
- **C4** (after T11-T12): `docs(docker): document test harness usage` — files: `docs/docker-testing.md`, `ROADMAP.md`. Pre-commit: `grep -q '## Quick Start' docs/docker-testing.md` etc.

> Sisyphus may merge C1+C2 if T1-T9 land cleanly together — that's fine. Splitting only matters for atomic-revert convenience.

---

## Success Criteria

### Verification Commands
```bash
# Single golden command: must exit 0 from clean checkout
scripts/test-distro.sh archlinux

# Evidence shape
latest=$(ls -td .sisyphus/evidence/archlinux/*/ | head -1)
test -f "$latest/summary.json"
[ "$(jq -r '.verdict' "$latest/summary.json")" = "pass" ]
[ "$(jq '.tasks_passed' "$latest/summary.json")" -ge 5 ]   # session_start + launch_app + render + click + type
jq -e '.install.wheel_sha256' "$latest/summary.json" >/dev/null
jq -e '.install.kwin_mcp_version' "$latest/summary.json" >/dev/null
jq -e '.install.package_versions' "$latest/summary.json" >/dev/null
jq -e '.screenshot_sha.initial' "$latest/summary.json" >/dev/null
jq -e '.screenshot_sha.post_click' "$latest/summary.json" >/dev/null
jq -e '.screenshot_sha.post_typing' "$latest/summary.json" >/dev/null
test -s "$latest/a11y/before.txt"
test -s "$latest/a11y/after.txt"
! diff -q "$latest/a11y/before.txt" "$latest/a11y/after.txt"

# Cleanliness
git diff --quiet src/kwin_mcp/   # no source changes
git diff --quiet .github/workflows/   # no workflow changes
test ! -e .github/workflows/distro-tests.yml   # no new workflow file

# Image pinning (date-tag, NOT digest)
grep -qE '^FROM manjarolinux/base:[0-9]{8}' docker/archlinux.Dockerfile   # single multi-arch base
! grep -E '@sha256:' docker/archlinux.Dockerfile   # digest pinning is forbidden by policy
! grep -E '^FROM archlinux:' docker/archlinux.Dockerfile   # rejected base must not be reintroduced (would break arm64)

# No forbidden flags in invocation (exact 5 flag-strings, separately listed — NOT collapsed via /dev/u?input)
! grep -E '\-\-privileged|\-\-cap-add=SYS_ADMIN|\-\-device=/dev/uinput|\-\-device=/dev/input|\-\-device=/dev/dri' scripts/test-distro.sh

# Idempotency
scripts/test-distro.sh archlinux   # second run also exits 0

# Future-proofing smoke check (must NOT yet pass — proves wrapper recognizes args)
scripts/test-distro.sh ubuntu 2>&1 | grep -qi 'not.*supported\|no.*dockerfile'   # graceful failure
```

### Final Checklist
- [x] All "Must Have" present and verified
- [x] All "Must NOT Have" absent and verified
- [x] Wave FINAL (F1-F4) all APPROVE
- [x] User explicitly says "okay" after seeing F1-F4 reports
- [x] Draft file `.sisyphus/drafts/docker-multi-distro-testing.md` deleted
