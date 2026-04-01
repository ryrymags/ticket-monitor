"""State persistence — tracks what has been seen and notified."""

from __future__ import annotations

import copy
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

# Cross-platform file locking: fcntl on Unix, msvcrt on Windows.
if sys.platform == "win32":
    import msvcrt

    def _lock_file(handle, *, shared: bool):
        """Acquire a lock on *handle* (Windows)."""
        # msvcrt.locking operates on a byte range; lock 1 byte at position 0.
        handle.seek(0)
        mode = msvcrt.LK_NBRLCK if shared else msvcrt.LK_NBLCK
        try:
            msvcrt.locking(handle.fileno(), mode, 1)
        except OSError:
            # Blocking fallback — retry with blocking variant.
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock_file(handle):
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _lock_file(handle, *, shared: bool):
        """Acquire a lock on *handle* (Unix)."""
        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(handle.fileno(), mode)

    def _unlock_file(handle):
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


logger = logging.getLogger(__name__)
_DELETE_SENTINEL = object()


def _dt_to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _iso_to_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


class MonitorState:
    """Persists monitor state to JSON to survive restarts."""

    def __init__(self, state_file: str = "state.json"):
        self.state_file = state_file
        self._lock_file = f"{state_file}.lock"
        self._state: dict = {"events": {}}
        self._baseline_state: dict = copy.deepcopy(self._state)
        self.load()

    # ---- Legacy event status/price tracking ----

    def get_last_status(self, event_id: str) -> str | None:
        return self._event(event_id).get("last_status")

    def set_last_status(self, event_id: str, status: str):
        self._event(event_id)["last_status"] = status
        self.save()

    def has_status_changed(self, event_id: str, new_status: str) -> bool:
        old = self.get_last_status(event_id)
        return old is not None and old != new_status

    def get_had_price_ranges(self, event_id: str) -> bool | None:
        val = self._event(event_id).get("had_price_ranges")
        if val is None:
            return None
        return bool(val)

    def set_had_price_ranges(self, event_id: str, had_ranges: bool):
        self._event(event_id)["had_price_ranges"] = had_ranges
        self.save()

    def get_last_price_key(self, event_id: str) -> str | None:
        return self._event(event_id).get("last_price_key")

    def set_last_price_key(self, event_id: str, key: str):
        self._event(event_id)["last_price_key"] = key
        self.save()

    def get_last_check(self, event_id: str) -> datetime | None:
        return _iso_to_dt(self._event(event_id).get("last_check"))

    def set_last_check(self, event_id: str):
        self._event(event_id)["last_check"] = _dt_to_iso(datetime.now(timezone.utc))
        self.save()

    # ---- Monitor run tracking ----

    def get_last_successful_check(self) -> datetime | None:
        return _iso_to_dt(self._state.get("last_successful_check"))

    def set_last_successful_check(self):
        self._state["last_successful_check"] = _dt_to_iso(datetime.now(timezone.utc))
        self.save()

    def get_monitor_start_time(self) -> datetime | None:
        return _iso_to_dt(self._state.get("monitor_started"))

    def set_monitor_start_time(self, dt: datetime):
        if "monitor_started" not in self._state:
            self._state["monitor_started"] = _dt_to_iso(dt)
            self.save()

    # ---- Health state ----

    def get_last_cycle_started_at(self) -> datetime | None:
        return _iso_to_dt(self._health().get("last_cycle_started_at"))

    def set_last_cycle_started_at(self, dt: datetime | None = None):
        health = self._health()
        health["last_cycle_started_at"] = _dt_to_iso(dt or datetime.now(timezone.utc))
        self.save()

    def get_last_cycle_completed_at(self) -> datetime | None:
        return _iso_to_dt(self._health().get("last_cycle_completed_at"))

    def set_last_cycle_completed_at(self, dt: datetime | None = None):
        health = self._health()
        health["last_cycle_completed_at"] = _dt_to_iso(dt or datetime.now(timezone.utc))
        self.save()

    def get_last_error_type(self) -> str | None:
        value = self._health().get("last_error_type")
        return str(value) if value else None

    def get_last_error_message(self) -> str | None:
        value = self._health().get("last_error_message")
        return str(value) if value else None

    def set_last_error(self, error_type: str, message: str):
        health = self._health()
        health["last_error_type"] = str(error_type)[:120]
        health["last_error_message"] = str(message)[:1200]
        self.save()

    def clear_last_error(self):
        health = self._health()
        health["last_error_type"] = None
        health["last_error_message"] = None
        self.save()

    def get_browser_restart_count_24h(self) -> int:
        self._prune_health_windows(save=True)
        return int(self._health().get("browser_restart_count_24h", 0))

    def get_browser_restart_count_recent(self, window_seconds: int, now: datetime | None = None) -> int:
        if window_seconds < 1:
            return 0
        return self._count_iso_list_within(
            self._health().get("browser_restart_events", []),
            now=now or datetime.now(timezone.utc),
            window=timedelta(seconds=window_seconds),
        )

    def record_browser_restart(self, dt: datetime | None = None):
        now = dt or datetime.now(timezone.utc)
        health = self._health()
        events = health.setdefault("browser_restart_events", [])
        events.append(_dt_to_iso(now))
        health["last_auto_fix_at"] = _dt_to_iso(now)
        self._prune_health_windows(now=now, save=False)
        self.save()

    def get_process_restart_requests_24h(self) -> int:
        self._prune_health_windows(save=True)
        return int(self._health().get("process_restart_requests_24h", 0))

    def get_process_restart_requests_recent(self, window_seconds: int, now: datetime | None = None) -> int:
        if window_seconds < 1:
            return 0
        return self._count_iso_list_within(
            self._health().get("process_restart_request_events", []),
            now=now or datetime.now(timezone.utc),
            window=timedelta(seconds=window_seconds),
        )

    def record_process_restart_request(self, dt: datetime | None = None):
        now = dt or datetime.now(timezone.utc)
        health = self._health()
        events = health.setdefault("process_restart_request_events", [])
        events.append(_dt_to_iso(now))
        health["last_auto_fix_at"] = _dt_to_iso(now)
        self._prune_health_windows(now=now, save=False)
        self.save()

    def get_last_auto_fix_at(self) -> datetime | None:
        return _iso_to_dt(self._health().get("last_auto_fix_at"))

    def set_last_auto_fix_at(self, dt: datetime | None = None):
        health = self._health()
        health["last_auto_fix_at"] = _dt_to_iso(dt or datetime.now(timezone.utc))
        self.save()

    def get_last_code_fingerprint(self) -> str:
        return str(self._health().get("last_code_fingerprint", ""))

    def set_last_code_fingerprint(self, fingerprint: str):
        self._health()["last_code_fingerprint"] = str(fingerprint)
        self.save()

    def get_guardian_pause_until(self) -> datetime | None:
        return _iso_to_dt(self._health().get("guardian_pause_until"))

    def set_guardian_pause_until(self, dt: datetime | None):
        self._health()["guardian_pause_until"] = _dt_to_iso(dt) if dt else None
        self.save()

    def get_guardian_last_critical_alert_at(self) -> datetime | None:
        return _iso_to_dt(self._health().get("guardian_last_critical_alert_at"))

    def set_guardian_last_critical_alert_at(self, dt: datetime | None):
        self._health()["guardian_last_critical_alert_at"] = _dt_to_iso(dt) if dt else None
        self.save()

    def get_guardian_fix_attempts_last_hour(self) -> int:
        self._prune_health_windows(save=True)
        return int(self._health().get("guardian_fix_attempts_last_hour", 0))

    def get_auth_reauth_attempts_last_hour(self) -> int:
        self._prune_health_windows(save=True)
        return int(self._health().get("auth_reauth_attempts_last_hour", 0))

    def get_auth_reauth_attempts_recent(self, window_seconds: int, now: datetime | None = None) -> int:
        if window_seconds < 1:
            return 0
        return self._count_iso_list_within(
            self._health().get("auth_reauth_attempt_events", []),
            now=now or datetime.now(timezone.utc),
            window=timedelta(seconds=window_seconds),
        )

    def record_auth_reauth_attempt(self, dt: datetime | None = None):
        now = dt or datetime.now(timezone.utc)
        health = self._health()
        events = health.setdefault("auth_reauth_attempt_events", [])
        events.append(_dt_to_iso(now))
        self._prune_health_windows(now=now, save=False)
        self.save()

    def get_auth_pause_until(self) -> datetime | None:
        return _iso_to_dt(self._health().get("auth_pause_until"))

    def set_auth_pause_until(self, dt: datetime | None):
        self._health()["auth_pause_until"] = _dt_to_iso(dt) if dt else None
        self.save()

    def record_guardian_fix_attempt(self, dt: datetime | None = None):
        now = dt or datetime.now(timezone.utc)
        health = self._health()
        events = health.setdefault("guardian_fix_attempt_events", [])
        events.append(_dt_to_iso(now))
        self._prune_health_windows(now=now, save=False)
        self.save()

    def get_health_snapshot(self) -> dict:
        self._prune_health_windows(save=True)
        health = dict(self._health())
        # Internal arrays stay internal; keep public snapshot concise.
        health.pop("browser_restart_events", None)
        health.pop("process_restart_request_events", None)
        health.pop("guardian_fix_attempt_events", None)
        health.pop("auth_reauth_attempt_events", None)
        return health

    # ---- Heartbeat + recap ----

    def get_last_heartbeat_date(self) -> str | None:
        return self._state.get("last_heartbeat_date")

    def set_last_heartbeat_date(self, date_str: str):
        self._state["last_heartbeat_date"] = date_str
        self.save()

    def get_last_heartbeat_at(self) -> datetime | None:
        return _iso_to_dt(self._state.get("last_heartbeat_at"))

    def set_last_heartbeat_at(self, dt: datetime):
        self._state["last_heartbeat_at"] = _dt_to_iso(dt)
        self.save()

    def get_last_session_health_check_at(self) -> datetime | None:
        return _iso_to_dt(self._state.get("last_session_health_check_at"))

    def set_last_session_health_check_at(self, dt: datetime | None = None):
        self._state["last_session_health_check_at"] = _dt_to_iso(dt or datetime.now(timezone.utc))
        self.save()

    def get_last_recap_date(self) -> str | None:
        return self._state.get("last_recap_date")

    def set_last_recap_date(self, date_str: str):
        self._state["last_recap_date"] = date_str
        self.save()

    # ---- Browser/detection state ----

    def get_last_availability_signature(self, event_id: str) -> str | None:
        return self._event(event_id).get("last_availability_signature")

    def set_last_availability_signature(self, event_id: str, signature: str):
        self._event(event_id)["last_availability_signature"] = signature
        self.save()

    def get_last_available_at(self, event_id: str) -> datetime | None:
        return _iso_to_dt(self._event(event_id).get("last_available_at"))

    def set_last_available_at(self, event_id: str, dt: datetime):
        self._event(event_id)["last_available_at"] = _dt_to_iso(dt)
        self.save()

    def get_last_alert_at(self, event_id: str) -> datetime | None:
        return _iso_to_dt(self._event(event_id).get("last_alert_at"))

    def set_last_alert_at(self, event_id: str, dt: datetime):
        self._event(event_id)["last_alert_at"] = _dt_to_iso(dt)
        self.save()

    def get_mention_burst_started_at(self, event_id: str) -> datetime | None:
        return _iso_to_dt(self._event(event_id).get("mention_burst_started_at"))

    def set_mention_burst_started_at(self, event_id: str, dt: datetime | None):
        self._event(event_id)["mention_burst_started_at"] = _dt_to_iso(dt) if dt else None
        self.save()

    def get_mention_burst_last_mention_at(self, event_id: str) -> datetime | None:
        return _iso_to_dt(self._event(event_id).get("mention_burst_last_mention_at"))

    def set_mention_burst_last_mention_at(self, event_id: str, dt: datetime | None):
        self._event(event_id)["mention_burst_last_mention_at"] = _dt_to_iso(dt) if dt else None
        self.save()

    def get_mention_burst_sent_count(self, event_id: str) -> int:
        return int(self._event(event_id).get("mention_burst_sent_count", 0))

    def set_mention_burst_sent_count(self, event_id: str, count: int):
        self._event(event_id)["mention_burst_sent_count"] = max(0, int(count))
        self.save()

    def increment_mention_burst_sent_count(self, event_id: str) -> int:
        event = self._event(event_id)
        next_count = int(event.get("mention_burst_sent_count", 0)) + 1
        event["mention_burst_sent_count"] = next_count
        self.save()
        return next_count

    def get_mention_burst_completed_for_episode(self, event_id: str) -> bool:
        return bool(self._event(event_id).get("mention_burst_completed_for_episode", False))

    def set_mention_burst_completed_for_episode(self, event_id: str, completed: bool):
        self._event(event_id)["mention_burst_completed_for_episode"] = bool(completed)
        self.save()

    def reset_mention_burst(self, event_id: str):
        event = self._event(event_id)
        event["mention_burst_started_at"] = None
        event["mention_burst_last_mention_at"] = None
        event["mention_burst_sent_count"] = 0
        event["mention_burst_completed_for_episode"] = False
        self.save()

    def get_consecutive_blocked(self, event_id: str) -> int:
        return int(self._event(event_id).get("consecutive_blocked", 0))

    def increment_consecutive_blocked(self, event_id: str) -> int:
        event = self._event(event_id)
        event["consecutive_blocked"] = int(event.get("consecutive_blocked", 0)) + 1
        self.save()
        return int(event["consecutive_blocked"])

    def reset_consecutive_blocked(self, event_id: str):
        self._event(event_id)["consecutive_blocked"] = 0
        self.save()

    def get_in_outage_state(self, event_id: str) -> bool:
        return bool(self._event(event_id).get("in_outage_state", False))

    def set_in_outage_state(self, event_id: str, value: bool):
        self._event(event_id)["in_outage_state"] = bool(value)
        self.save()

    def get_last_probe_success_at(self, event_id: str) -> datetime | None:
        return _iso_to_dt(self._event(event_id).get("last_probe_success_at"))

    def set_last_probe_success_at(self, event_id: str, dt: datetime):
        self._event(event_id)["last_probe_success_at"] = _dt_to_iso(dt)
        self.save()

    def get_last_operational_alert_fingerprint(self, event_id: str) -> str | None:
        value = self._event(event_id).get("last_operational_alert_fingerprint")
        return str(value) if value else None

    def get_last_operational_alert_at(self, event_id: str) -> datetime | None:
        return _iso_to_dt(self._event(event_id).get("last_operational_alert_at"))

    def set_last_operational_alert(self, event_id: str, fingerprint: str, dt: datetime):
        event = self._event(event_id)
        event["last_operational_alert_fingerprint"] = str(fingerprint)
        event["last_operational_alert_at"] = _dt_to_iso(dt)
        self.save()

    def clear_last_operational_alert(self, event_id: str):
        event = self._event(event_id)
        event["last_operational_alert_fingerprint"] = ""
        event["last_operational_alert_at"] = None
        self.save()

    # ---- Persistence ----

    def load(self):
        lock_handle = None
        try:
            lock_handle = self._acquire_state_lock(shared=True)
            if not os.path.exists(self.state_file):
                logger.debug("No state file found, starting fresh")
                self._state = {"events": {}}
            else:
                self._state = self._read_state_file_unlocked()
                logger.debug("Loaded state from %s", self.state_file)
        except (json.JSONDecodeError, OSError, ValueError) as e:
            logger.warning("Could not load state file %s: %s — starting fresh", self.state_file, e)
            self._state = {"events": {}}
        finally:
            if lock_handle is not None:
                self._release_state_lock(lock_handle)

        self._migrate_state()
        self._baseline_state = copy.deepcopy(self._state)

    def save(self):
        lock_handle = None
        try:
            lock_handle = self._acquire_state_lock(shared=False)
            latest_on_disk = self._read_state_file_unlocked(default={"events": {}})
            self._migrate_state_dict(latest_on_disk)
            updates = self._collect_state_updates(self._baseline_state, self._state)
            merged_state = self._apply_state_updates(latest_on_disk, updates)
            self._migrate_state_dict(merged_state)
            self._write_state_file_unlocked(merged_state)
            self._state = merged_state
            self._baseline_state = copy.deepcopy(merged_state)
        except json.JSONDecodeError as e:
            logger.warning("State file %s is invalid JSON during save; rewriting fresh: %s", self.state_file, e)
            try:
                self._migrate_state()
                self._write_state_file_unlocked(self._state)
                self._baseline_state = copy.deepcopy(self._state)
            except OSError as rewrite_error:
                logger.error("Failed to rewrite corrupt state file: %s", rewrite_error)
        except OSError as e:
            logger.error("Failed to save state: %s", e)
        finally:
            if lock_handle is not None:
                self._release_state_lock(lock_handle)

    # ---- Helpers ----

    def _acquire_state_lock(self, *, shared: bool):
        lock_dir = os.path.dirname(self._lock_file) or "."
        os.makedirs(lock_dir, exist_ok=True)
        lock_handle = open(self._lock_file, "a+", encoding="utf-8")
        _lock_file(lock_handle, shared=shared)
        return lock_handle

    @staticmethod
    def _release_state_lock(lock_handle):
        try:
            _unlock_file(lock_handle)
        finally:
            lock_handle.close()

    def _read_state_file_unlocked(self, default: dict | None = None) -> dict:
        fallback = copy.deepcopy(default) if default is not None else {"events": {}}
        if not os.path.exists(self.state_file):
            return fallback
        with open(self.state_file, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            raise ValueError("State file root must be an object")
        return loaded

    def _write_state_file_unlocked(self, payload: dict):
        dir_name = os.path.dirname(self.state_file) or "."
        os.makedirs(dir_name, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
            os.replace(tmp_path, self.state_file)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    @classmethod
    def _collect_state_updates(cls, previous: object, current: object, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], object]]:
        updates: list[tuple[tuple[str, ...], object]] = []
        if isinstance(previous, dict) and isinstance(current, dict):
            previous_keys = set(previous.keys())
            current_keys = set(current.keys())
            for key in current_keys - previous_keys:
                updates.append((path + (str(key),), copy.deepcopy(current[key])))
            for key in previous_keys - current_keys:
                updates.append((path + (str(key),), _DELETE_SENTINEL))
            for key in previous_keys & current_keys:
                updates.extend(cls._collect_state_updates(previous[key], current[key], path + (str(key),)))
            return updates

        if previous != current:
            updates.append((path, copy.deepcopy(current)))
        return updates

    @staticmethod
    def _apply_state_updates(base_state: dict, updates: list[tuple[tuple[str, ...], object]]) -> dict:
        merged = copy.deepcopy(base_state)
        for path, value in updates:
            if not path:
                if isinstance(value, dict):
                    merged = copy.deepcopy(value)
                    continue
                raise ValueError("Top-level state must remain a dictionary")

            target = merged
            for key in path[:-1]:
                child = target.get(key)
                if not isinstance(child, dict):
                    child = {}
                    target[key] = child
                target = child

            leaf_key = path[-1]
            if value is _DELETE_SENTINEL:
                target.pop(leaf_key, None)
            else:
                target[leaf_key] = copy.deepcopy(value)
        return merged

    def _event(self, event_id: str) -> dict:
        events = self._state.setdefault("events", {})
        if not isinstance(events, dict):
            events = {}
            self._state["events"] = events
        if event_id not in events:
            events[event_id] = {}
            self._migrate_event(events[event_id])
        return events[event_id]

    def _migrate_state(self):
        self._migrate_state_dict(self._state)

    def _migrate_state_dict(self, state: dict):
        events = state.setdefault("events", {})
        if not isinstance(events, dict):
            events = {}
            state["events"] = events
        health = state.setdefault("health", {})
        if not isinstance(health, dict):
            health = {}
            state["health"] = health
        self._migrate_health(health)
        for event in events.values():
            if not isinstance(event, dict):
                continue
            self._migrate_event(event)

    @staticmethod
    def _migrate_event(event: dict):
        event.setdefault("last_availability_signature", "")
        event.setdefault("last_available_at", None)
        event.setdefault("last_alert_at", None)
        event.setdefault("consecutive_blocked", 0)
        event.setdefault("in_outage_state", False)
        event.setdefault("last_probe_success_at", None)
        event.setdefault("last_operational_alert_fingerprint", "")
        event.setdefault("last_operational_alert_at", None)
        event.setdefault("mention_burst_started_at", None)
        event.setdefault("mention_burst_last_mention_at", None)
        event.setdefault("mention_burst_sent_count", 0)
        event.setdefault("mention_burst_completed_for_episode", False)

    @staticmethod
    def _migrate_health(health: dict):
        health.setdefault("last_cycle_started_at", None)
        health.setdefault("last_cycle_completed_at", None)
        health.setdefault("last_error_type", None)
        health.setdefault("last_error_message", None)
        health.setdefault("browser_restart_count_24h", 0)
        health.setdefault("process_restart_requests_24h", 0)
        health.setdefault("last_auto_fix_at", None)
        health.setdefault("last_code_fingerprint", "")
        health.setdefault("browser_restart_events", [])
        health.setdefault("process_restart_request_events", [])
        health.setdefault("guardian_pause_until", None)
        health.setdefault("guardian_last_critical_alert_at", None)
        health.setdefault("guardian_fix_attempt_events", [])
        health.setdefault("guardian_fix_attempts_last_hour", 0)
        health.setdefault("auth_reauth_attempt_events", [])
        health.setdefault("auth_reauth_attempts_last_hour", 0)
        health.setdefault("auth_pause_until", None)

    def _health(self) -> dict:
        health = self._state.setdefault("health", {})
        if not isinstance(health, dict):
            health = {}
            self._state["health"] = health
        self._migrate_health(health)
        return health

    def _prune_health_windows(self, now: datetime | None = None, save: bool = False):
        now = now or datetime.now(timezone.utc)
        health = self._health()

        browser_events = self._prune_iso_list(
            health.get("browser_restart_events", []),
            now=now,
            window=timedelta(hours=24),
        )
        process_events = self._prune_iso_list(
            health.get("process_restart_request_events", []),
            now=now,
            window=timedelta(hours=24),
        )
        guardian_attempts = self._prune_iso_list(
            health.get("guardian_fix_attempt_events", []),
            now=now,
            window=timedelta(hours=1),
        )
        auth_attempts = self._prune_iso_list(
            health.get("auth_reauth_attempt_events", []),
            now=now,
            window=timedelta(hours=1),
        )

        health["browser_restart_events"] = browser_events
        health["process_restart_request_events"] = process_events
        health["guardian_fix_attempt_events"] = guardian_attempts
        health["auth_reauth_attempt_events"] = auth_attempts
        health["browser_restart_count_24h"] = len(browser_events)
        health["process_restart_requests_24h"] = len(process_events)
        health["guardian_fix_attempts_last_hour"] = len(guardian_attempts)
        health["auth_reauth_attempts_last_hour"] = len(auth_attempts)

        if save:
            self.save()

    @staticmethod
    def _prune_iso_list(values: list, now: datetime, window: timedelta) -> list[str]:
        cutoff = now - window
        kept: list[str] = []
        for raw in values:
            dt = _iso_to_dt(raw)
            if dt is None:
                continue
            if dt >= cutoff:
                kept.append(_dt_to_iso(dt))
        return kept

    @staticmethod
    def _count_iso_list_within(values: list, now: datetime, window: timedelta) -> int:
        cutoff = now - window
        count = 0
        for raw in values:
            dt = _iso_to_dt(raw)
            if dt is None:
                continue
            if dt >= cutoff:
                count += 1
        return count
