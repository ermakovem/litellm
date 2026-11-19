from concurrent.futures import ThreadPoolExecutor

import pytest

from litellm.repositories.subscription_quota import QuotaLedger, Snapshot


def test_parallel_profiles_share_a_single_observed_delta(tmp_path):
    ledger = QuotaLedger(tmp_path / "quota.db")
    ledger.set_share("codex", "alice", 50)
    ledger.set_share("codex", "bob", 25)
    ledger.observe("codex", "week", Snapshot(10, 100, 10))
    ledger.record("r1", "codex", "alice", 12, 3)
    ledger.record("r2", "codex", "bob", 13, 1)
    snapshot = Snapshot(14, 100, 20)
    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(lambda _: ledger.observe("codex", "week", snapshot), range(2)))

    assert ledger.usage("alice")[0].remaining_percent == 94
    assert ledger.usage("bob")[0].remaining_percent == 96
    assert ledger.usage("alice")[0].unallocated_points == 0


def test_delayed_usage_and_reset_are_independent_per_window(tmp_path):
    ledger = QuotaLedger(tmp_path / "quota.db")
    ledger.set_share("codex", "alice", 50)
    ledger.observe("codex", "week", Snapshot(0, 100, 10))
    ledger.observe("codex", "session", Snapshot(0, 30, 10))
    ledger.record("r1", "codex", "alice", 12, 2)
    ledger.observe("codex", "week", Snapshot(0, 100, 15))
    ledger.observe("codex", "week", Snapshot(10, 100, 20))
    ledger.observe("codex", "session", Snapshot(25, 30, 20))
    ledger.observe("codex", "session", Snapshot(0, 60, 31))

    usage = {window.window: window for window in ledger.usage("alice")}
    assert usage["week"].remaining_percent == 80
    assert usage["session"].remaining_percent == 100
    assert usage["session"].resets_at == 60


def test_unattributed_usage_and_share_reduction(tmp_path):
    ledger = QuotaLedger(tmp_path / "quota.db")
    ledger.set_share("codex", "alice", 60)
    ledger.set_share("codex", "bob", 30)
    with pytest.raises(ValueError, match="100"):
        ledger.set_share("codex", "bob", 41)
    ledger.observe("codex", "week", Snapshot(5, 100, 10))
    ledger.observe("codex", "week", Snapshot(7, 100, 11))
    ledger.record("r1", "codex", "alice", 13, 1)
    ledger.record("r1", "codex", "alice", 13, 1)
    ledger.observe("codex", "week", Snapshot(17, 100, 20))
    ledger.set_share("codex", "alice", 10)

    quota = ledger.usage("alice")[0]
    assert quota.remaining_percent == 0
    assert quota.unallocated_points == 2
    assert ledger.usage("bob")[0].remaining_percent == 100

def test_late_usage_callback_is_available_for_the_next_observation(tmp_path):
    ledger = QuotaLedger(tmp_path / "quota.db")
    ledger.set_share("codex", "alice", 50)
    ledger.observe("codex", "week", Snapshot(10, 100, 10))
    ledger.observe("codex", "week", Snapshot(12, 100, 20))
    ledger.record("late", "codex", "alice", 19, 3)
    ledger.observe("codex", "week", Snapshot(14, 100, 22))

    window = ledger.usage("alice")[0]
    assert window.allocated_points == 2
    assert window.unallocated_points == 2

def test_shared_quota_uses_provider_balance_without_profile_shares(tmp_path):
    ledger = QuotaLedger(tmp_path / "quota.db")
    ledger.set_share("codex", "restricted", 1)
    ledger.observe("codex", "week", Snapshot(10, 100, 10))
    ledger.record("r1", "codex", "restricted", 12, 3)
    ledger.observe("codex", "week", Snapshot(14, 100, 20))
    ledger.observe("another", "session", Snapshot(100, 50, 20))

    quota = ledger.shared_usage("codex")
    assert len(quota) == 1
    assert quota[0].remaining_percent == 86
    assert quota[0].resets_at == 100
    assert {item.source for item in ledger.shared_usage()} == {"codex", "another"}
    assert ledger.usage("restricted")[0].remaining_percent == 0
