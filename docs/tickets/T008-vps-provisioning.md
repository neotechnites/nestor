# T008 — VPS provisioning + systemd timer + deploy secrets

**Priority:** P1 · **Status:** todo · **Gated on:** Ryan picks a VPS provider

## Goal
Stand up the always-on host, schedule the daily weather run, and wire the
GitHub → VPS deploy.

## Scope
- Provision script/docs for the chosen provider (Rust toolchain optional — we
  ship a prebuilt binary from CI; box only needs the binary + .env + secrets).
- `systemd --user` service + timer: `nestor-weather.timer` at 09:00 ET (box in
  UTC; DST handled in code). Reconcile timer next morning.
- Add repo secrets `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`; enable deploy.yml.
- Alerting webhook on run/trade/error.

## Done when
- Push to main ships the binary; the timer fires the daily run on the VPS;
  a trade/skip line reaches the alert channel.
