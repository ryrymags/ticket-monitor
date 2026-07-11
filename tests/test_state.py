"""Tests for MonitorState — persistence, status tracking, and price ranges."""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from src.state import MonitorState


@pytest.fixture
def state_file(tmp_path):
    return str(tmp_path / "test_state.json")


@pytest.fixture
def state(state_file):
    return MonitorState(state_file=state_file)



class TestPersistence:
    def test_save_and_load(self, state_file):
        state = MonitorState(state_file=state_file)
        state.set_last_availability_signature("event-1", "sig-a")
        state.set_in_outage_state("event-1", True)

        # Create new state from same file
        state2 = MonitorState(state_file=state_file)
        assert state2.get_last_availability_signature("event-1") == "sig-a"
        assert state2.get_in_outage_state("event-1") is True

    def test_missing_file_starts_fresh(self, tmp_path):
        state = MonitorState(state_file=str(tmp_path / "nonexistent.json"))
        assert state.get_last_availability_signature("event-1") == ""

    def test_corrupt_file_starts_fresh(self, state_file):
        with open(state_file, "w") as f:
            f.write("not valid json{{{")
        state = MonitorState(state_file=state_file)
        assert state.get_last_availability_signature("event-1") == ""

    def test_atomic_save_creates_file(self, state_file):
        state = MonitorState(state_file=state_file)
        state.set_last_availability_signature("event-1", "sig-test")
        assert os.path.exists(state_file)
        with open(state_file) as f:
            data = json.load(f)
        assert data["events"]["event-1"]["last_availability_signature"] == "sig-test"

    def test_save_creates_sidecar_lock_file(self, state_file):
        state = MonitorState(state_file=state_file)
        state.set_last_availability_signature("event-1", "sig-a")
        assert os.path.exists(f"{state_file}.lock")

    def test_stale_writer_does_not_clobber_other_instance_updates(self, state_file):
        first = MonitorState(state_file=state_file)
        second = MonitorState(state_file=state_file)
        first.set_last_availability_signature("event-1", "sig-a")
        seen_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        second.set_last_alert_at("event-2", seen_at)

        reloaded = MonitorState(state_file=state_file)
        assert reloaded.get_last_availability_signature("event-1") == "sig-a"
        assert reloaded.get_last_alert_at("event-2") == seen_at


class TestLastCheck:
    def test_initial_last_check_is_none(self, state):
        assert state.get_last_check("event-1") is None

    def test_set_and_get_last_check(self, state):
        state.set_last_check("event-1")
        result = state.get_last_check("event-1")
        assert result is not None
        assert isinstance(result, datetime)




class TestHeartbeat:
    def test_initial_heartbeat_is_none(self, state):
        assert state.get_last_heartbeat_at() is None

    def test_set_and_get_heartbeat(self, state):
        at = datetime(2026, 2, 21, tzinfo=timezone.utc)
        state.set_last_heartbeat_at(at)
        assert state.get_last_heartbeat_at() == at


class TestBrowserDetectionState:
    def test_signature_roundtrip(self, state):
        state.set_last_availability_signature("event-1", "abc123")
        assert state.get_last_availability_signature("event-1") == "abc123"

    def test_blocked_counter_increment_and_reset(self, state):
        assert state.get_consecutive_blocked("event-1") == 0
        assert state.increment_consecutive_blocked("event-1") == 1
        assert state.increment_consecutive_blocked("event-1") == 2
        state.reset_consecutive_blocked("event-1")
        assert state.get_consecutive_blocked("event-1") == 0

    def test_outage_state_roundtrip(self, state):
        assert state.get_in_outage_state("event-1") is False
        state.set_in_outage_state("event-1", True)
        assert state.get_in_outage_state("event-1") is True

    def test_probe_success_timestamp_roundtrip(self, state):
        now = datetime.now(timezone.utc)
        state.set_last_probe_success_at("event-1", now)
        loaded = state.get_last_probe_success_at("event-1")
        assert loaded is not None
        assert isinstance(loaded, datetime)

    def test_operational_alert_fingerprint_roundtrip(self, state):
        now = datetime.now(timezone.utc)
        state.set_last_operational_alert("event-1", "critical:event-1:test", now)
        assert state.get_last_operational_alert_fingerprint("event-1") == "critical:event-1:test"
        assert state.get_last_operational_alert_at("event-1") is not None
        state.clear_last_operational_alert("event-1")
        assert state.get_last_operational_alert_fingerprint("event-1") is None
        assert state.get_last_operational_alert_at("event-1") is None

    def test_mention_burst_fields_roundtrip(self, state):
        now = datetime.now(timezone.utc)
        state.set_mention_burst_started_at("event-1", now)
        state.set_mention_burst_last_mention_at("event-1", now)
        state.set_mention_burst_sent_count("event-1", 3)
        state.set_mention_burst_completed_for_episode("event-1", True)

        assert state.get_mention_burst_started_at("event-1") is not None
        assert state.get_mention_burst_last_mention_at("event-1") is not None
        assert state.get_mention_burst_sent_count("event-1") == 3
        assert state.get_mention_burst_completed_for_episode("event-1") is True

    def test_mention_burst_reset(self, state):
        now = datetime.now(timezone.utc)
        state.set_mention_burst_started_at("event-1", now)
        state.set_mention_burst_last_mention_at("event-1", now)
        state.set_mention_burst_sent_count("event-1", 2)
        state.set_mention_burst_completed_for_episode("event-1", True)

        state.reset_mention_burst("event-1")

        assert state.get_mention_burst_started_at("event-1") is None
        assert state.get_mention_burst_last_mention_at("event-1") is None
        assert state.get_mention_burst_sent_count("event-1") == 0
        assert state.get_mention_burst_completed_for_episode("event-1") is False


class TestMigration:
    def test_old_state_file_gets_new_keys(self, state_file):
        old_state = {
            "events": {
                "event-legacy": {
                    "last_status": "offsale",
                }
            }
        }
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(old_state, f)

        state = MonitorState(state_file=state_file)
        assert state.get_consecutive_blocked("event-legacy") == 0
        assert state.get_in_outage_state("event-legacy") is False
        assert state.get_last_availability_signature("event-legacy") == ""
        assert state.get_last_operational_alert_fingerprint("event-legacy") is None
        assert state.get_mention_burst_started_at("event-legacy") is None
        assert state.get_mention_burst_last_mention_at("event-legacy") is None
        assert state.get_mention_burst_sent_count("event-legacy") == 0
        assert state.get_mention_burst_completed_for_episode("event-legacy") is False


class TestHealthState:
    def test_cycle_timestamps_roundtrip(self, state):
        now = datetime.now(timezone.utc)
        state.set_last_cycle_started_at(now)
        state.set_last_cycle_completed_at(now)
        assert state.get_last_cycle_started_at() is not None
        assert state.get_last_cycle_completed_at() is not None

    def test_error_roundtrip(self, state):
        state.set_last_error("timeout", "example failure")
        assert state.get_last_error_type() == "timeout"
        assert state.get_last_error_message() == "example failure"
        state.clear_last_error()
        assert state.get_last_error_type() is None

    def test_restart_counters_24h(self, state):
        state.record_browser_restart()
        state.record_process_restart_request()
        assert state.get_browser_restart_count_24h() == 1
        assert state.get_process_restart_requests_24h() == 1

    def test_auth_reauth_attempts_last_hour(self, state):
        now = datetime.now(timezone.utc)
        state.record_auth_reauth_attempt(now - timedelta(seconds=4000))
        state.record_auth_reauth_attempt(now - timedelta(seconds=30))
        assert state.get_auth_reauth_attempts_last_hour() == 1

    def test_auth_pause_roundtrip(self, state):
        pause_until = datetime.now(timezone.utc) + timedelta(minutes=10)
        state.set_auth_pause_until(pause_until)
        loaded = state.get_auth_pause_until()
        assert loaded is not None

    def test_session_health_fields_roundtrip(self, state):
        state.set_session_logout_pending_count(2)
        state.set_last_session_health_reason("login_page_content")

        assert state.get_session_logout_pending_count() == 2
        assert state.get_last_session_health_reason() == "login_page_content"

    def test_recent_restart_counters_respect_window(self, state):
        now = datetime.now(timezone.utc)
        state.record_browser_restart(now - timedelta(seconds=400))
        state.record_browser_restart(now - timedelta(seconds=40))
        state.record_process_restart_request(now - timedelta(seconds=500))
        state.record_process_restart_request(now - timedelta(seconds=50))

        assert state.get_browser_restart_count_recent(60, now=now) == 1
        assert state.get_process_restart_requests_recent(60, now=now) == 1

    def test_code_fingerprint_roundtrip(self, state):
        state.set_last_code_fingerprint("abc123")
        assert state.get_last_code_fingerprint() == "abc123"

    def test_health_snapshot_has_required_keys(self, state):
        snapshot = state.get_health_snapshot()
        assert "last_cycle_started_at" in snapshot
        assert "last_cycle_completed_at" in snapshot
        assert "last_error_type" in snapshot
        assert "last_error_message" in snapshot
        assert "browser_restart_count_24h" in snapshot
        assert "process_restart_requests_24h" in snapshot
        assert "last_auto_fix_at" in snapshot
        assert "last_code_fingerprint" in snapshot


class TestCheckOutcomeMetrics:
    def _state(self, tmp_path):
        from src.state import MonitorState

        return MonitorState(state_file=str(tmp_path / "state.json"))

    def test_record_updates_buckets_and_totals(self, tmp_path):
        from src.state import summarize_check_stats

        st = self._state(tmp_path)
        now = datetime(2026, 6, 23, 20, 30, tzinfo=timezone.utc)
        st.record_check_outcome("healthy", now)
        st.record_check_outcome("healthy", now)
        st.record_check_outcome("blocked", now)
        st.record_check_outcome("challenge", now)

        stats = summarize_check_stats(st._health(), hours=24, now=now)
        assert stats["healthy"] == 2
        assert stats["blocked"] == 1
        assert stats["challenge"] == 1
        assert stats["total"] == 4
        assert stats["healthy_pct"] == 50.0
        assert stats["block_pct"] == 50.0  # (blocked + challenge) / total

        totals = st._health()["check_totals"]
        assert totals["healthy"] == 2
        assert totals["blocked"] == 1
        assert totals["challenge"] == 1

    def test_invalid_outcome_ignored(self, tmp_path):
        from src.state import summarize_check_stats

        st = self._state(tmp_path)
        now = datetime(2026, 6, 23, 20, 30, tzinfo=timezone.utc)
        st.record_check_outcome("bogus", now)
        stats = summarize_check_stats(st._health(), hours=24, now=now)
        assert stats["total"] == 0

    def test_old_buckets_excluded_from_window(self, tmp_path):
        from src.state import summarize_check_stats

        st = self._state(tmp_path)
        now = datetime(2026, 6, 23, 20, 30, tzinfo=timezone.utc)
        st.record_check_outcome("healthy", now - timedelta(hours=30))  # stale, pruned
        st.record_check_outcome("healthy", now)

        stats = summarize_check_stats(st._health(), hours=24, now=now)
        assert stats["healthy"] == 1  # only the recent bucket counts
        # ...but lifetime totals keep both.
        assert st._health()["check_totals"]["healthy"] == 2

    def test_stats_survive_reload(self, tmp_path):
        from src.state import MonitorState, summarize_check_stats

        path = str(tmp_path / "state.json")
        now = datetime(2026, 6, 23, 20, 30, tzinfo=timezone.utc)
        st = MonitorState(state_file=path)
        st.record_check_outcome("healthy", now)
        st.record_check_outcome("blocked", now)

        reloaded = MonitorState(state_file=path)
        stats = summarize_check_stats(reloaded._health(), hours=24, now=now)
        assert stats["total"] == 2


def test_monitor_start_time_restamps_on_every_start(tmp_path):
    state = MonitorState(state_file=str(tmp_path / "state.json"))
    first = datetime(2026, 5, 25, tzinfo=timezone.utc)
    second = datetime(2026, 7, 9, tzinfo=timezone.utc)

    state.set_monitor_start_time(first)
    assert state.get_monitor_start_time() == first
    # A restart must record the NEW start, not keep first-install time forever.
    state.set_monitor_start_time(second)
    assert state.get_monitor_start_time() == second


class TestTransactionBatching:
    def _counting_state(self, tmp_path, monkeypatch):
        state = MonitorState(state_file=str(tmp_path / "state.json"))
        writes = []
        original = state._write_state_file_unlocked

        def counting(payload):
            writes.append(1)
            original(payload)

        monkeypatch.setattr(state, "_write_state_file_unlocked", counting)
        return state, writes

    def test_transaction_batches_many_mutations_into_one_write(self, tmp_path, monkeypatch):
        state, writes = self._counting_state(tmp_path, monkeypatch)

        with state.transaction():
            state.set_last_check("e1")
            state.record_check_outcome("healthy")
            state.set_in_outage_state("e1", False)
            state.set_last_successful_check()

        assert len(writes) == 1
        reloaded = MonitorState(state_file=state.state_file)
        assert reloaded.get_last_check("e1") is not None
        assert reloaded.get_last_successful_check() is not None

    def test_nested_transactions_coalesce(self, tmp_path, monkeypatch):
        state, writes = self._counting_state(tmp_path, monkeypatch)

        with state.transaction():
            state.set_last_check("e1")
            with state.transaction():
                state.set_in_outage_state("e1", True)

        assert len(writes) == 1
        assert MonitorState(state_file=state.state_file).get_in_outage_state("e1") is True

    def test_setters_still_save_immediately_outside_transaction(self, tmp_path, monkeypatch):
        state, writes = self._counting_state(tmp_path, monkeypatch)
        state.set_last_check("e1")
        assert len(writes) == 1

    def test_transaction_commits_even_when_body_raises(self, tmp_path, monkeypatch):
        state, writes = self._counting_state(tmp_path, monkeypatch)
        with pytest.raises(RuntimeError):
            with state.transaction():
                state.set_in_outage_state("e1", True)
                raise RuntimeError("boom")

        assert len(writes) == 1
        assert MonitorState(state_file=state.state_file).get_in_outage_state("e1") is True


class TestKnownSections:
    def test_merge_dedupes_case_insensitively_and_sorts(self, tmp_path):
        state = MonitorState(state_file=str(tmp_path / "state.json"))
        assert state.merge_known_sections("ev1", ["LOGE20", "Floor1"]) is True
        # Same names (any casing) are a no-op; new one merges in.
        assert state.merge_known_sections("ev1", ["loge20", "FLOOR1"]) is False
        assert state.merge_known_sections("ev1", ["PIT"]) is True
        assert state.get_known_sections("ev1") == ["Floor1", "LOGE20", "PIT"]

    def test_sections_survive_reload(self, tmp_path):
        path = str(tmp_path / "state.json")
        MonitorState(state_file=path).merge_known_sections("ev1", ["LOGE20"])
        assert MonitorState(state_file=path).get_known_sections("ev1") == ["LOGE20"]

    def test_empty_and_blank_names_ignored(self, tmp_path):
        state = MonitorState(state_file=str(tmp_path / "state.json"))
        assert state.merge_known_sections("ev1", ["", "  ", None]) is False
        assert state.get_known_sections("ev1") == []
