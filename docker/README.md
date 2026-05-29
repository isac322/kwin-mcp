# docker/

Docker test harnesses for verifying kwin-mcp runs correctly on multiple Linux distributions.

See [`runtime-contract.md`](runtime-contract.md) for the cross-distro contract: mount paths, user, venv location, exit codes, evidence layout, and forbidden flags.

## Adding a new distro

1. Write `docker/<distro>.Dockerfile` conforming to `runtime-contract.md`
2. Add `<distro>` to the `SUPPORTED` array in `scripts/test-distro.sh`
3. Run `scripts/test-distro.sh <distro>` and iterate to green
4. Update `docs/docker-testing.md` distro list
5. Add a ROADMAP entry

## Running

```bash
scripts/test-distro.sh manjaro
```

Evidence is written to `.sisyphus/evidence/<distro>/<timestamp>/`.
