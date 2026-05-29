# Issues — archlinux-docker-harness

## [2026-05-05] Plan initialized

No issues yet. Tasks not started.

## [2026-05-05] F1-F4 Round 1 Verdicts

### F1 (oracle): REJECT
- A. Dockerfile gcc/pkgconf 추가 (Must Have line 106 위반)
- B. test-distro.sh 조건부 `--device /dev/dri/renderD128/129` 패스스루 (Must NOT line 119 정신 위반)
- C. src/kwin_mcp/ 변경 disclosure 필요: session.py extras + screenshot.py
- D. smoke_test.py time.sleep(1.5) (Must NOT line 1289)
- E. docs/docker-testing.md "validation in progress" stale text (T10 PASS와 불일치)

### F2 (Code Quality): REJECT
- F. Dockerfile UID/GID 리터럴 1000이 user-creation 외부에 존재 (lines 40/46/54)
- G. test-distro.sh missing-wheel guard가 set -e 하에서 unreachable
- D (재확인). smoke_test.py 1.5초 sleep 3곳 (lines 159/176/181)

### F3 (Real Manual QA): APPROVE ✅
- Run1 (20260505T025636Z): exit=0, verdict=pass, tasks_passed=14
- Run2 (20260505T025757Z): exit=0, idempotency 확인
- 9/9 evidence files, 3 distinct screenshot SHAs, a11y diff present
- 컨테이너 zombies=0, 이미지 보존, forbidden flags 0건

### F4 (Scope Fidelity): REJECT
- T3 CREEP: package substitution 섹션이 distro-specific 내용 포함
- T6 CREEP: gcc/pkgconf + setcap (A 재확인)
- T8 CREEP: PIL offset detection + 1.5초 sleep (D 재확인)
- T9 CREEP: render-node device 패스스루 (B 재확인)
- T10 CREEP: src/kwin_mcp/screenshot.py 변경 (C 재확인)
- T11 CONTAMINATION: docs/docker-testing.md가 4871368(T10)에 섞임
- T11 STALE: "validation in progress" 문구

### 통합 5대 분류 (사용자 결정 필요)
1. **A (Dockerfile gcc/pkgconf)**: 실용적 — dbus-python wheel 빌드용. 수용 또는 base-devel 대신 명시적 plan waiver.
2. **B (renderD128 패스스루)**: 안전 — render-node는 root-only가 아니고 GPU 가속 시 사용. 제거(strict) 또는 plan에 optional 명시.
3. **C (src/kwin_mcp/ 확장)**: PR-worthy SDK fix. session.py(env), screenshot.py(CaptureWorkspace). m0207 사전 승인 범위 초과 — 명시 OK 또는 revert.
4. **D (1.5초 sleep)**: smoke_test.py가 wait_for_element로 못 잡는 settle 시점에 사용. 수용(plan waiver) 또는 wait_for_element/state polling으로 refactor.
5. **E+F+G (sloppy fixes)**: docs stale text, UID/GID 변수화, missing-wheel guard fix — 모두 trivial 수정 가능.

## [2026-05-05 Atlas] BLOCKED on FINAL WAVE APPROVAL GATE

**State**: All 4 final-wave reviewers (F1-F4) returned APPROVE under 3 documented waivers (m0207 + Waiver A/B/C). T1-T12 all complete + committed. Evidence verified twice with idempotency.

**Block reason**: Per system instruction (m0298) "FINAL WAVE APPROVAL GATE":
> "Wait for the user's explicit approval. Do NOT auto-continue. Do NOT call task() again unless the user rejects and requests fixes."
> "DO NOT mark the final-wave checkbox complete until the user explicitly says okay."

**Conflict observed**: System's generic auto-continue prompt is firing concurrently with the GATE instruction. The GATE is more specific and was explicitly tied to F1-F4 completion event. Holding position per GATE.

**Awaiting**: User's explicit OK/REJECT response to the F1-F4 consolidated report (presented in conversation).

**On user OK**: Mark F1, F2, F3, F4, Definition of Done items, and Final Checklist items 1-3. Optionally commit residual `.sisyphus/*` files as chore commit.

**On user REJECT**: Identify rejected item, delegate fix, re-run affected reviewer.

## [2026-05-05] Regression: 8d9b30c removed dri_args, broke fresh harness runs

**State**: Commit `8d9b30c chore(docker): round-2 fixes per F1-F4 review` removed the conditional `dri_args` block from `scripts/test-distro.sh`. Subsequent fresh harness runs fail with `DBusException('Screenshot got cancelled')` after 6/14 scenarios.

**Why F1-F4 Round 2/3 didn't catch it**:
- F1: static plan-vs-repo check, no execution
- F2: static analysis, no execution
- F3: verified historical evidence (`20260504T201603Z`/`201643Z`) only; Phase D ("fresh idempotency run") was OPTIONAL and skipped
- F4: static diff review, no execution

The historical evidence was from PRE-`8d9b30c` code. Reviewers validated outdated artifacts.

**Fix**: see `archlinux-docker-harness-regression.md` plan, R1.

**Mitigation for future plans**: F3 Phase D MUST be mandatory, not optional, when reviewing any plan whose deliverable is an executable harness. Static-only review of historical evidence is insufficient.
