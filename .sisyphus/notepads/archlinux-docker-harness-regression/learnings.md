## [2026-05-05] R1 recovery learnings

- `8d9b30c` removed only the conditional `dri_args` declaration and docker-run expansion from `scripts/test-distro.sh`; the recovery is a narrow partial restore with Waiver D documentation.
- The R1 QA surface is static and syntax-only by design. Fresh harness execution belongs to R2 after the R-C1 commit lands.
- Evidence files for R1 were written to `.sisyphus/evidence/regression-r1-restore-check.txt` and `.sisyphus/evidence/regression-r1-docs-check.txt`, but R-C1 intentionally stages only the four acceptance files.
- R1 follow-up: reworded comment to avoid audit-grep self-match on backtick-wrapped flag literal.

## [2026-05-05] R2 fresh harness idempotency

- Captured ts_before=20260505T043008Z before the valid run pair; waited one second before run 1 so both evidence timestamps are strictly newer than ts_before.
- Run 1 evidence: .sisyphus/evidence/archlinux/20260505T043010Z/, wrapper exit 0, verdict=pass, tasks_passed=14, evidence files=9/9, screenshot hashes distinct=3, a11y before/after=changed.
- Run 2 evidence: .sisyphus/evidence/archlinux/20260505T043032Z/, wrapper exit 0, verdict=pass, tasks_passed=14, evidence files=9/9, screenshot hashes distinct=3, a11y before/after=changed.
- Idempotency confirmed: run 2 created a different evidence directory from run 1, and both passed all 14 harness scenarios after the R-C1 dri_args restore.
- Cumulative R2 log saved at .sisyphus/evidence/regression-r2-runs.log.
- Note: an earlier same-second pair also passed but was not used as R2 evidence because its first timestamp equaled ts_before rather than being strictly later.


## 2026-05-05 F4 Round 4 Scope Fidelity Check
- R-C1 `ef1158f` scope matched the authorized 4-file set: `scripts/test-distro.sh`, `docker/runtime-contract.md`, harness `decisions.md`, and harness `issues.md`; no source, workflow, tests, or pyproject changes were introduced by R-C1.
- Cumulative `f5e9fb2..HEAD` SDK changes remained limited to waivered `src/kwin_mcp/session.py` and `src/kwin_mcp/screenshot.py`; parent plan completed-checkbox count remained 31.
- Runtime forbidden-flag audit across `scripts/`, `docker/`, and `docs/` returned zero matches; Waiver D's `dri_args` block only passes `/dev/dri/renderD128` and `/dev/dri/renderD129`, never `card0`/`card1`.
- Fresh evidence directories `20260505T043010Z` and `20260505T043032Z` both contained `summary.json` with `verdict=pass` and `tasks_passed=14`.

- 2026-05-05 F3 Round 4 Real Manual QA: mandatory fresh `DOCKER_HOST=tcp://localhost:2375 scripts/test-distro.sh archlinux` run produced `.sisyphus/evidence/archlinux/20260505T043527Z/` with exit=0, verdict=pass, tasks_passed=14, 9 evidence files, 3 distinct screenshot SHA-256 hashes, and changed a11y before/after output. R2 evidence dirs `20260505T043010Z` and `20260505T043032Z` rechecked with the same pass criteria. Forbidden runtime Docker flags remained clean and `kwin-mcp-test` container zombies were 0.
