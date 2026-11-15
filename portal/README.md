# xp-split self-service portal

A lightweight stand-in for Backstage. A quant fills in the same questions as
`xp-backstage/quant-pricer-template.yaml`, presses **Create**, and gets a generated repo
under `/root/xp-split/generated/` with `platform/` pinned and the four guards run against it.

## Run it

```sh
python3 portal/app.py        # listens on 127.0.0.1:7007 ONLY
```

It is **not** exposed to the internet. Reach it over an SSH tunnel from your Mac:

```sh
ssh -i ~/.ssh/<ssh_key> -L 7007:localhost:7007 root@<ip>
# then open http://localhost:7007
```

## Pages

- **Create** — the form, built entirely from the template YAML (`spec.parameters`): the
  `name` pattern, the `methods` enum and defaults, and the `platform_ref` / `pricers_ref`
  dropdowns populated from the **real** `git tag -l` output (newest first). A live preview
  shows the exact `generate.sh` command. Add a method to the YAML enum and it appears here.
- **Result** — the generator output, the generated tree (2 levels), the `.copier-answers.yml`,
  and `run_guards.sh` run against the new repo from its pinned platform tag (PASS/FAIL per guard).
- **Catalog** — every repo under `generated/`, with name, `platform_ref`, `pricers_ref`,
  `methods` (from `.copier-answers.yml`) and the latest guard result.
- **Build on RHEL 8** (per repo, optional) — runs `cmake --workflow --preset ci-linux-gcc`
  in `xp-toolchain:dev`, streaming the log. Disabled with a reason if the image is absent.

## What it does on Create

Validates server-side, then shells out to the **existing** generator unchanged:

```
generate.sh <name> <platform-tag> <pricers-tag> <method...>
```

It does not reimplement any generator logic, and keeps `quant-pricer-template.yaml` as valid
Backstage so moving to real Backstage later costs nothing.

## Security

- Binds `127.0.0.1:7007` only — never `0.0.0.0`. Verify: `ss -tlnp | grep 7007`.
- Rejects any request whose `Host` header isn't `localhost:7007` / `127.0.0.1:7007`
  (DNS-rebinding protection).
- State-changing actions are **POST** and require a matching `Origin` header.
- All inputs are validated on the server: `name` against the YAML pattern; each method in the
  YAML enum with no duplicates; each tag present in the real `git tag -l`; destination must not
  already exist.
- `generate.sh` / `run_guards.sh` / `docker` run via `subprocess` **argument lists** (never
  `shell=True`), with timeouts, from a fixed working directory.
- One create/build at a time (a lock); a second concurrent request gets **409 busy**.
- No systemd unit, no firewall changes, nothing at boot. Start it by hand.
