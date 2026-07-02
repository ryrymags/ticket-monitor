# Changelog

All notable changes to Ticket Monitor are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

<!-- CHANGELOG_START -->
## [Unreleased] — 2026-07-02

### Changes

- `0332e49`  2026-07-02 10:19  Fix self-heal reboot: root-owned wrapper instead of unreadable redirect

- `36643e5`  2026-07-02 10:15  Fix doctor-lite profile clash (stray about:blank tabs), 30min reboot threshold, per-concert uptime timeline

- `5196372`  2026-07-02 10:05  Fix egress diagnostic: resolve IP over HTTPS before ip-api lookup

- `a7ea8c1`  2026-07-02 10:01  Add one-command sudo setup script for self-heal reboot

- `b1f4b1a`  2026-07-02 10:00  Slow cadence to 60-120s, variation probe, reboot self-heal, boot persistence

- `159b288`  2026-07-01 23:19  Audit fixes: atomic JSON writes, gitignore, robustness, tests

- `f6d2196`  2026-07-01 19:05  Add per-event Ticketmaster scheduler

- `c57c29d`  2026-07-01 16:51  Fix session health block handling

- `b31a0f3`  2026-07-01 16:00  Add Uptime tab, fix History-tab crash, ticket-seen stats, ntfy push UI

- `dc7fefd`  2026-06-24 23:53  Anti-block: headful Chrome, human-like nav, fast adaptive cadence

- `809c90f`  2026-06-24 23:28  Honest alert delivery, startup warmup grace, real login verification

- `f1b7054`  2026-06-24 22:50  Sync GUI/ping degraded state, add in-app fixes, challenge cooldown

- `4416213`  2026-06-24 11:18  Document ntfy push setup and iOS app deep-link mechanism in README

- `4d73db2`  2026-06-24 11:16  Add ntfy.sh push notifications with iOS app deep-linking

- `9ea780a`  2026-06-23 19:12  Make history de-duper runnable from terminal anywhere + add Mac launcher

- `6e53adc`  2026-06-23 19:08  Add re-runnable ticket-history dedupe cleanup

- `61fb6c4`  2026-06-23 19:01  Dedup repeat detections in BINGO counter and ticket history

- `94e965e`  2026-06-23 18:52  Provision Google Chrome in GUI setup scripts for the chrome channel

- `d8b7e01`  2026-06-23 18:48  Add adaptive cadence, stealth, health stats, and BINGO history counter

- `5bfa846`  2026-06-23 18:13  Fix false stall pings and stop non-BINGO @ mentions

- `992e9e4`  2026-06-20 20:08  Overhaul notifications: quiet, BINGO-only, ping only when manual action is truly needed

- `035e394`  2026-06-18 14:08  Respect non-BINGO alert toggle

- `842e0d5`  2026-06-18 14:01  Add multi-BINGO configs and clearer Discord alerts

- `94ec911`  2026-04-07 16:03  Fix Playwright bot-detection spinner and simplify login bootstrap URL

- `b5e4805`  2026-04-07 15:44  Fix Python version requirement in README and Mac setup script

- `2a7c00a`  2026-04-07 15:43  Add Chrome fallback to login bootstrap flow

- `940f77b`  2026-04-07 15:42  Fix Discord error message referencing macOS-only monitorctl.sh

- `4adf242`  2026-04-07 15:41  Fix remaining stale Chrome channel references after default change

- `516ee1b`  2026-04-07 15:19  Switch default browser channel to bundled Playwright Chromium

- `6841ac0`  2026-04-07 15:16  Fix Playwright Chrome not found error on Windows

- `29e1615`  2026-04-07 15:01  Fix Playwright channel mismatch on Windows and surface bootstrap errors immediately

- `55a3828`  2026-04-07 14:15  Fix Playwright browser hang on Windows during login bootstrap

- `e3a57a2`  2026-04-01 07:26  Add PostToolUse CI check hook

- `e1e69cb`  2026-04-01 07:23  Fix CI: lint error, deploy guard, remove dead auto-merge workflow

- `36f6dff`  2026-04-01 07:19  Initial release: v1.3.0

<!-- CHANGELOG_END -->
