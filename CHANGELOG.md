# Changelog

All notable changes to Ticket Monitor are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

<!-- CHANGELOG_START -->
## [Unreleased] — 2026-07-18

### Changes

- `(pending)`  2026-07-18 03:38  Move platform launcher/setup scripts into installers/ folder

- `215680a`  2026-07-17 10:25  Sanitize public history and add privacy checks

- `b078b65`  2026-07-11 22:45  Per-BINGO-config notification routing: multiple ntfy topics + Discord ping toggle

- `155afde`  2026-07-11 18:28  Recover seat-map sections served from browser cache (Night 2 = 0 sections bug)

- `b57dec2`  2026-07-11 18:19  Section picker polish: sub-scroller, scan result counts, drop 1-char artifacts

- `5a360c5`  2026-07-11 18:08  Learn abbreviation pairs (CLB/CLUB) + browsable section list by family

- `029eaae`  2026-07-11 17:59  Add section-family picker options + fix progressive search matching

- `861a85d`  2026-07-11 17:49  Dedupe section naming variants + search-to-add section picker

- `1d868d5`  2026-07-11 17:41  Learn venue section names automatically + Auto-detect Sections picker

- `f327635`  2026-07-11 12:41  Add per-event scoping to BINGO configs (event_ids + GUI event picker)

- `1b72455`  2026-07-10 04:07  Add BrowserProbe.from_config factory; dedup 3 identical call sites

- `08e55aa`  2026-07-10 04:05  Split load_config into per-section helper functions

- `6d07c06`  2026-07-10 03:58  Add golden characterization tests for load_config

- `e4bcbf6`  2026-07-10 03:57  Fold consecutive same-state rows in the uptime timeline

- `86118c7`  2026-07-09 22:40  Mark audit remediation plan complete

- `7cf9761`  2026-07-09 22:40  Restore README.md as a file and finalize plan status

- `a9a9169`  2026-07-09 22:38  Record completed audit remediation plan

- `cf84627`  2026-07-09 22:38  Remove the legacy all-events cycle scheduler mode

- `b9d04ce`  2026-07-09 22:34  Remove vestigial Oracle-VM deploy workflow and Linux systemd setup

- `c01f36d`  2026-07-09 22:34  Add one-time uptime ledger repair script and fix historical data

- `8757e71`  2026-07-09 14:55  Remove hardcoded event-weight defaults and fix stale docs

- `f99dd71`  2026-07-09 14:54  Delete dead Discovery-API-era notifier and state code

- `3dccd12`  2026-07-09 14:51  Raise ConfigError from load_config instead of sys.exit

- `bbf3bd6`  2026-07-09 14:49  Pool Monitor-tab events panel widgets instead of rebuilding per poll

- `bec3199`  2026-07-09 14:48  Batch per-check state mutations into one merge-save

- `78d9a77`  2026-07-09 14:12  Tighten challenge detection and add availability diagnostics

- `0db0315`  2026-07-09 14:09  Replace mention bursts with 3 rapid pings then silence per listing

- `8135942`  2026-07-09 13:54  Finish Phase 1: 429 retry, Tk thread safety, doctor profile guard

- `0b1a80a`  2026-07-09 13:52  Honor operational_to_discord in guardian and reloader notifiers

- `e8c5e74`  2026-07-09 13:51  Re-stamp monitor_started on every monitor start

- `2529e44`  2026-07-09 13:50  Make single-instance locks cross-platform via the state lock shim

- `1a5c41f`  2026-07-09 13:47  Budget cycle work time in uptime down-gap inference

- `cef64c3`  2026-07-08 23:45  Bump CI actions to Node 24 majors

- `3d50850`  2026-07-08 23:42  Fix ruff E731 lint failure in guardian tests

- `d19630c`  2026-07-08 23:01  Fix stale commit hashes in the auto-generated changelog

- `e8b4c6e`  2026-07-08 22:09  Tie monitoring to the GUI and finish the Uptime tab fix

- `864237d`  2026-07-08 13:02  Fix Uptime tab crashes, arm the reboot tier, send ntfy first

- `60d3a62`  2026-07-08 12:43  Stop the guardian/monitor restart death spiral

- `061388d`  2026-07-02 16:21  Record blocked checks as impaired uptime

- `77f7b36`  2026-07-02 14:11  Stabilize monitor history and stop controls

- `c0fe1ce`  2026-07-02 13:31  Page history tab rendering

- `a7adb58`  2026-07-02 12:47  Fix launchd uptime status in app

- `4b483b2`  2026-07-02 12:21  Treat loaded Ticketmaster pages as healthy uptime

- `dc3e2c1`  2026-07-02 12:14  Keep macOS monitor awake under launchd

- `8943e6e`  2026-07-02 11:07  Close session-health tab after each check instead of parking it

- `c0523e2`  2026-07-02 10:47  Switch self-heal reboot from FileVault authrestart to plain reboot

- `7d275f1`  2026-07-02 10:19  Fix self-heal reboot: root-owned wrapper instead of unreadable redirect

- `d4278ab`  2026-07-02 10:15  Fix doctor-lite profile clash (stray about:blank tabs), 30min reboot threshold, per-concert uptime timeline

- `2b15740`  2026-07-02 10:05  Fix egress diagnostic: resolve IP over HTTPS before ip-api lookup

- `c497a7d`  2026-07-02 10:01  Add one-command sudo setup script for self-heal reboot

- `05d0279`  2026-07-02 10:00  Slow cadence to 60-120s, variation probe, reboot self-heal, boot persistence

- `f34ea61`  2026-07-01 23:19  Audit fixes: atomic JSON writes, gitignore, robustness, tests

- `5f75296`  2026-07-01 19:05  Add per-event Ticketmaster scheduler

- `4ea13e0`  2026-07-01 16:51  Fix session health block handling

- `6bf460a`  2026-07-01 16:00  Add Uptime tab, fix History-tab crash, ticket-seen stats, ntfy push UI

- `c03a81a`  2026-06-24 23:53  Anti-block: headful Chrome, human-like nav, fast adaptive cadence

- `611334b`  2026-06-24 23:28  Honest alert delivery, startup warmup grace, real login verification

- `5cfc559`  2026-06-24 22:50  Sync GUI/ping degraded state, add in-app fixes, challenge cooldown

- `ea0094b`  2026-06-24 11:18  Document ntfy push setup and iOS app deep-link mechanism in README

- `c56e9c0`  2026-06-24 11:16  Add ntfy.sh push notifications with iOS app deep-linking

- `91f66ef`  2026-06-23 19:12  Make history de-duper runnable from terminal anywhere + add Mac launcher

- `f8263e5`  2026-06-23 19:08  Add re-runnable ticket-history dedupe cleanup

- `3891674`  2026-06-23 19:01  Dedup repeat detections in BINGO counter and ticket history

- `13e422a`  2026-06-23 18:52  Provision Google Chrome in GUI setup scripts for the chrome channel

- `a03badb`  2026-06-23 18:48  Add adaptive cadence, stealth, health stats, and BINGO history counter

- `24d05a6`  2026-06-23 18:13  Fix false stall pings and stop non-BINGO @ mentions

- `bd9fd87`  2026-06-20 20:08  Overhaul notifications: quiet, BINGO-only, ping only when manual action is truly needed

- `44aed9f`  2026-06-18 14:08  Respect non-BINGO alert toggle

- `3cfadbe`  2026-06-18 14:01  Add multi-BINGO configs and clearer Discord alerts

- `13eb482`  2026-04-07 16:03  Fix Playwright bot-detection spinner and simplify login bootstrap URL

- `1aa4430`  2026-04-07 15:44  Fix Python version requirement in README and Mac setup script

- `d1bd3da`  2026-04-07 15:43  Add Chrome fallback to login bootstrap flow

- `92b7741`  2026-04-07 15:42  Fix Discord error message referencing macOS-only monitorctl.sh

- `e5b637f`  2026-04-07 15:41  Fix remaining stale Chrome channel references after default change

- `b59b219`  2026-04-07 15:19  Switch default browser channel to bundled Playwright Chromium

- `c7e2c84`  2026-04-07 15:16  Fix Playwright Chrome not found error on Windows

- `fb5f45a`  2026-04-07 15:01  Fix Playwright channel mismatch on Windows and surface bootstrap errors immediately

- `4975ff7`  2026-04-07 14:15  Fix Playwright browser hang on Windows during login bootstrap

- `f8eae5d`  2026-04-01 07:26  Add PostToolUse CI check hook

- `8bc2ddf`  2026-04-01 07:23  Fix CI: lint error, deploy guard, remove dead auto-merge workflow

- `0489065`  2026-04-01 07:19  Initial release: v1.3.0

<!-- CHANGELOG_END -->
