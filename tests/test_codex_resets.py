"""No live auth, app-server, subscription ledger, or redemption is touched."""
import base64
import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import time
import types
import contextvars
from pathlib import Path

import pytest

from _hermes_ai_usage_ledger_v2 import codex_resets as resets
REAL_COORD_ROOT = resets._coord_root
FIXTURE_RESET_AT = None


def snapshot(*, count=2, used=100, allowed=False, reached='rate_limit_reached',
             account='org-fixture', minutes=300, reset=None):
    assert reset is not None or FIXTURE_RESET_AT is not None
    return {'accountId': account, 'ordinaryUsageAllowed': allowed,
            'rateLimitResetCredits': {'availableCount': count, 'credits': None},
            'rateLimits': {'limitId': 'codex', 'rateLimitReachedType': reached,
                'primary': {'usedPercent': used, 'windowDurationMins': minutes,
                            'resetsAt': FIXTURE_RESET_AT if reset is None else reset}}}


class FakeServer:
    state = None
    calls = []
    fail_after_consume = False
    def __init__(self, token, account):
        assert token == 'fixture-token' and account == 'org-fixture'
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass
    def limits(self):
        self.calls.append('read')
        if self.state['accountId'] != 'org-fixture':
            raise resets.Unavailable('Account mismatch')
        return json.loads(json.dumps(self.state))
    def call(self, method, params=None):
        assert method == 'account/rateLimitResetCredit/consume'
        assert params['idempotencyKey']
        self.calls.append(('consume', params['idempotencyKey']))
        if self.fail_after_consume:
            raise resets.Unavailable('Timeout after send')
        self.state['rateLimitResetCredits']['availableCount'] -= 1
        self.state['rateLimits']['primary']['usedPercent'] = 0
        self.state['ordinaryUsageAllowed'] = True
        self.state['rateLimits']['rateLimitReachedType'] = None
        return {'outcome': 'reset'}


@pytest.fixture
def fixture(monkeypatch, tmp_path):
    monkeypatch.setattr(sys.modules[__name__], 'FIXTURE_RESET_AT', int(time.time()) + 3600)
    FakeServer.state = snapshot()
    FakeServer.calls = []
    FakeServer.fail_after_consume = False
    monkeypatch.setattr(resets, 'AppServer', FakeServer)
    monkeypatch.setattr(resets, '_credentials', lambda: ('fixture-token', 'org-fixture', 'fixture-binding'))
    monkeypatch.setattr(resets, '_coord_root', lambda home: tmp_path)
    scoped = contextvars.ContextVar('fixture_hermes_home', default=None)
    constants = types.ModuleType('hermes_constants')
    constants.set_hermes_home_override = scoped.set
    constants.reset_hermes_home_override = scoped.reset
    constants.get_hermes_home = scoped.get
    monkeypatch.setitem(sys.modules, 'hermes_constants', constants)
    return tmp_path


def test_opt_in_manual_and_duplicate_guard(fixture):
    view = resets.observe(fixture)
    assert view['auto'] is False and view['redeemable'] and view['count'] == 2
    assert resets.automatic_tick(fixture,lambda:True) is None
    assert not any(isinstance(x, tuple) for x in FakeServer.calls)
    resets.set_auto(fixture, view['binding'], True)
    result = resets.automatic_tick(fixture,lambda:True)
    assert result['outcome'] == 'reset' and result['view']['count'] == 1
    assert resets.automatic_tick(fixture,lambda:True) is None
    assert len([x for x in FakeServer.calls if isinstance(x, tuple)]) == 1
    with pytest.raises(resets.Unavailable):
        resets.redeem(fixture, expected_binding=view['binding'], expected_episode=view['episode'], expected_count=2)


@pytest.mark.parametrize('changes', [dict(count=0), dict(count=None), dict(used=99),
    dict(allowed=None), dict(allowed=True), dict(reached=None),
    dict(reached='workspace_owner_credits_depleted'), dict(minutes=60),
    dict(reset=1)])
def test_unknown_or_wrong_limit_is_not_redeemable(fixture, changes):
    FakeServer.state = snapshot(**changes)
    view = resets.observe(fixture)
    assert view['redeemable'] is False
    with pytest.raises(resets.Unavailable):
        resets.redeem(fixture, expected_binding=view['binding'], expected_episode=view['episode'], expected_count=2)
    assert not any(isinstance(x, tuple) for x in FakeServer.calls)


def test_ambiguous_consume_is_durably_blocked(fixture):
    view = resets.observe(fixture)
    FakeServer.fail_after_consume = True
    with pytest.raises(resets.Unavailable):
        resets.redeem(fixture, expected_binding=view['binding'], expected_episode=view['episode'], expected_count=2)
    blocked = resets.observe(fixture)
    assert blocked['blocked'] and not blocked['redeemable']
    resets.set_auto(fixture, view['binding'], True)
    assert resets.automatic_tick(fixture,lambda:True) is None
    assert len([x for x in FakeServer.calls if isinstance(x, tuple)]) == 1
    with sqlite3.connect(resets._db_path(fixture)) as conn:
        assert conn.execute('SELECT attempt,episode FROM state').fetchone()[0]


def test_account_switch_disables_old_opt_in(fixture, monkeypatch):
    view = resets.observe(fixture)
    resets.set_auto(fixture, view['binding'], True)
    monkeypatch.setattr(resets, '_credentials', lambda: ('fixture-token', 'org-fixture', 'other-principal'))
    assert not resets.observe(fixture)['auto']
    with pytest.raises(resets.Unavailable):
        resets.set_auto(fixture, view['binding'], True)
    assert resets.automatic_tick(fixture,lambda:True) is None


def test_plugin_disable_prevents_automatic_consume(fixture):
    view=resets.observe(fixture)
    resets.set_auto(fixture,view['binding'],True)
    with pytest.raises(resets.Unavailable,match='disabled'):
        resets.automatic_tick(fixture,lambda:False)
    assert not any(isinstance(x,tuple) for x in FakeServer.calls)


def test_account_response_mismatch_and_windows(fixture):
    FakeServer.state = snapshot(account='org-other')
    with pytest.raises(resets.Unavailable):
        resets.observe(fixture)
    assert resets._episode(snapshot(minutes=10080))
    assert resets._episode(snapshot(allowed=None)) is None
    assert resets._episode(snapshot(used=100, reached=None)) is None


def test_same_episode_not_spent_twice_even_if_provider_reports_stale_exhaustion(fixture):
    view = resets.observe(fixture)
    resets.redeem(fixture, expected_binding=view['binding'], expected_episode=view['episode'], expected_count=2)
    FakeServer.state = snapshot(count=1, reset=FakeServer.state['rateLimits']['primary']['resetsAt'])
    with pytest.raises(resets.Unavailable):
        resets.redeem(fixture, expected_binding=view['binding'], expected_episode=view['episode'], expected_count=1)
    assert len([x for x in FakeServer.calls if isinstance(x, tuple)]) == 1


def test_new_reset_window_is_a_distinct_episode(fixture):
    old = resets.observe(fixture)
    assert old['episode'] == resets._episode(snapshot(reset=FIXTURE_RESET_AT))
    resets.redeem(fixture, expected_binding=old['binding'],
                  expected_episode=old['episode'], expected_count=old['count'])
    assert FIXTURE_RESET_AT is not None
    setattr(FakeServer, 'state', snapshot(count=1, reset=FIXTURE_RESET_AT + 3600))
    new = resets.observe(fixture)
    assert new['episode'] != old['episode'] and new['redeemable']
    assert resets.redeem(fixture, expected_binding=new['binding'],
                         expected_episode=new['episode'], expected_count=1)['outcome'] == 'reset'
    assert len([x for x in FakeServer.calls if isinstance(x, tuple)]) == 2


def test_concurrent_manual_requests_have_one_winner(fixture,monkeypatch):
    import threading
    view=resets.observe(fixture)
    original=FakeServer.call
    def slow(self,method,params=None):
        time.sleep(0.25)
        return original(self,method,params)
    monkeypatch.setattr(FakeServer,'call',slow)
    barrier=threading.Barrier(3)
    results=[]
    def run():
        barrier.wait()
        try:
            results.append(resets.redeem(fixture,expected_binding=view['binding'],
                          expected_episode=view['episode'],expected_count=view['count'])['outcome'])
        except resets.Unavailable:
            results.append('blocked')
    threads=[threading.Thread(target=run) for _ in range(2)]
    for thread in threads: thread.start()
    barrier.wait()
    for thread in threads: thread.join(timeout=2)
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(results)==['blocked','reset']
    assert len([x for x in FakeServer.calls if isinstance(x,tuple)])==1


def _redeem_process(home, binding, episode, output, gate):
    gate.wait()
    try:
        output.put(resets.redeem(home, expected_binding=binding,
                                expected_episode=episode, expected_count=2)['outcome'])
    except resets.Unavailable:
        output.put('blocked')


def test_canonical_root_and_symlink_fail_closed(fixture, monkeypatch):
    import hermes_constants
    hermes_constants.get_default_hermes_root = lambda: fixture
    monkeypatch.setattr(resets, '_coord_root', REAL_COORD_ROOT)
    other = fixture / 'profiles' / 'named'
    other.mkdir(parents=True)
    assert resets._db_path(fixture) == resets._db_path(other)
    assert resets._db_path(fixture).parent.stat().st_mode & 0o777 == 0o700
    link = fixture / 'profiles' / 'alias'
    link.symlink_to(other, target_is_directory=True)
    with pytest.raises(resets.Unavailable): resets._db_path(link)
    outside = fixture / 'unrelated'
    outside.mkdir()
    with pytest.raises(resets.Unavailable): resets._db_path(outside)
    authority = resets._db_path(fixture).parent
    authority.chmod(0o755)
    with pytest.raises(resets.Unavailable): resets._db_path(fixture)


def test_database_symlink_is_rejected_without_touching_target(fixture):
    folder = resets._db_path(fixture).parent
    sentinel = fixture / 'untouched'
    sentinel.write_text('keep')
    (folder / 'state.sqlite3').symlink_to(sentinel)
    with pytest.raises(OSError): resets.observe(fixture)
    assert sentinel.read_text() == 'keep'


def test_two_profile_homes_two_processes_share_episode_authority(fixture):
    other = fixture / 'profiles' / 'second'
    other.mkdir(parents=True)
    view = resets.observe(fixture)
    resets.set_auto(fixture, view['binding'], True)
    assert not resets.observe(other)['auto']
    ctx = multiprocessing.get_context('fork')
    output, gate = ctx.Queue(), ctx.Event()
    workers = [ctx.Process(target=_redeem_process,
               args=(home, view['binding'], view['episode'], output, gate))
               for home in (fixture, other)]
    for worker in workers: worker.start()
    gate.set()
    for worker in workers: worker.join(timeout=5)
    assert all(worker.exitcode == 0 for worker in workers)
    assert sorted([output.get(timeout=1) for _ in workers]) == ['blocked', 'reset']
    with pytest.raises(resets.Unavailable):
        resets.redeem(other, expected_binding=view['binding'],
                      expected_episode=view['episode'], expected_count=2)


def test_ambiguous_attempt_blocks_other_home_even_with_other_subject(fixture, monkeypatch):
    other = fixture / 'profiles' / 'second'
    other.mkdir(parents=True)
    view = resets.observe(fixture)
    FakeServer.fail_after_consume = True
    with pytest.raises(resets.Unavailable):
        resets.redeem(fixture, expected_binding=view['binding'],
                      expected_episode=view['episode'], expected_count=2)
    monkeypatch.setattr(resets, '_credentials', lambda: ('fixture-token', 'org-fixture', 'other-subject-binding'))
    blocked = resets.observe(other)
    assert blocked['blocked'] and not blocked['redeemable'] and not blocked['auto']
    with pytest.raises(resets.Unavailable):
        resets.redeem(other, expected_binding=blocked['binding'],
                      expected_episode=blocked['episode'], expected_count=2)


def test_different_accounts_have_independent_authority(fixture, monkeypatch):
    other = fixture / 'profiles' / 'second'
    other.mkdir(parents=True)
    view = resets.observe(fixture)
    FakeServer.fail_after_consume = True
    with pytest.raises(resets.Unavailable):
        resets.redeem(fixture, expected_binding=view['binding'],
                      expected_episode=view['episode'], expected_count=2)
    class OtherServer(FakeServer):
        def __init__(self, token, account):
            assert account == 'org-other' and token == 'fixture-token'
        def limits(self):
            return snapshot(account='org-other')
        def call(self, method, params=None):
            return {'outcome':'nothingToReset'}
    monkeypatch.setattr(resets, 'AppServer', OtherServer)
    monkeypatch.setattr(resets, '_credentials', lambda: ('fixture-token', 'org-other', 'other-binding'))
    fresh = resets.observe(other)
    assert fresh['redeemable'] and not fresh['blocked']
    assert resets.redeem(other, expected_binding=fresh['binding'],
                         expected_episode=fresh['episode'], expected_count=2)['outcome'] == 'nothingToReset'


# Per-worker-process handshake for the concurrent-accounts test (set after fork).
_OVERLAP = {}
OVERLAP_WAIT_SECONDS = 10


def _account_process(home, account, output, role, handshake):
    # The two accounts' short SQLite transactions (observe, admission,
    # finalisation) are sequenced by explicit events, so they never contend for
    # the shared authority's SQLite lock; only the two consume RPCs, which hold
    # no SQLite lock, are in flight at the same time. Lock contention across
    # finalisation is covered by the finalisation tests below. No sleep decides
    # the order.
    resets._credentials = lambda: ('fixture-token', account, 'binding-' + account)
    _OVERLAP.update(role=role, **handshake)
    result = {'account': account}
    try:
        if role == 'second' and not handshake['first_in_consume'].wait(OVERLAP_WAIT_SECONDS):
            raise AssertionError('first consume never started')
        view = resets.observe(home)
        result.update(blocked=view['blocked'], redeemable=view['redeemable'])
        try:
            result['outcome'] = resets.redeem(home, expected_binding=view['binding'],
                expected_episode=view['episode'], expected_count=2)['outcome']
        except resets.Unavailable as exc:
            result['outcome'] = 'blocked: %s' % exc
        if role == 'first':
            handshake['first_done'].set()
    finally:
        output.put(result)


@pytest.mark.parametrize('repeat', range(4))
def test_different_accounts_can_consume_concurrently(fixture, monkeypatch, repeat):
    other = fixture / 'profiles' / 'second'
    other.mkdir(parents=True)
    class MultiServer:
        def __init__(self, token, account): self.account = account
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def limits(self): return snapshot(account=self.account)
        def call(self, method, params=None):
            assert method == 'account/rateLimitResetCredit/consume'
            if _OVERLAP['role'] == 'first':
                # Hold the first consume in flight (its pending attempt committed)
                # until the other account has passed admission and is consuming too.
                _OVERLAP['first_in_consume'].set()
                if not _OVERLAP['second_in_consume'].wait(OVERLAP_WAIT_SECONDS):
                    raise AssertionError('second account was not admitted during first consume')
            else:
                with sqlite3.connect(resets._db_path(fixture)) as conn:
                    _OVERLAP['pending_during_overlap'].value = conn.execute(
                        'SELECT COUNT(*) FROM state WHERE attempt IS NOT NULL').fetchone()[0]
                _OVERLAP['second_in_consume'].set()
                # Finalise only after the first account has finalised.
                if not _OVERLAP['first_done'].wait(OVERLAP_WAIT_SECONDS):
                    raise AssertionError('first account did not finalise')
            return {'outcome': 'nothingToReset'}
    monkeypatch.setattr(resets, 'AppServer', MultiServer)
    ctx = multiprocessing.get_context('fork')
    output = ctx.Queue()
    # Three ordering events plus a shared counter for the overlap observation.
    handshake: dict = {name: ctx.Event() for name in ('first_in_consume', 'second_in_consume', 'first_done')}
    handshake['pending_during_overlap'] = ctx.Value('i', -1)
    workers = [ctx.Process(target=_account_process, args=(home, account, output, role, handshake))
               for home, account, role in ((fixture, 'org-fixture', 'first'),
                                           (other, 'org-other', 'second'))]
    for worker in workers: worker.start()
    for worker in workers: worker.join(timeout=3 * OVERLAP_WAIT_SECONDS)
    assert all(worker.exitcode == 0 for worker in workers)
    results = {r['account']: r for r in (output.get(timeout=1) for _ in workers)}
    # Both accounts' attempts were pending at once: the first account's in-flight
    # consume blocked neither the other account's observation nor its admission.
    assert handshake['pending_during_overlap'].value == 2
    assert results == {account: {'account': account, 'blocked': False, 'redeemable': True,
                                 'outcome': 'nothingToReset'}
                       for account in ('org-fixture', 'org-other')}
    with sqlite3.connect(resets._db_path(fixture)) as conn:
        assert conn.execute('SELECT attempt, result FROM state').fetchall() == [
            (None, 'nothingToReset')] * 2


def _consumes():
    return [x for x in FakeServer.calls if isinstance(x, tuple)]


def _state_rows(home):
    conn = sqlite3.connect(resets._db_path(home))
    try:
        return conn.execute('SELECT attempt, result FROM state').fetchall()
    finally:
        conn.close()


class _Holder:
    """Another connection holding the authority lock across finalisation."""
    def __init__(self, home, mode):
        self.conn = sqlite3.connect(resets._db_path(home), isolation_level=None, check_same_thread=False)
        if mode == 'writer':
            self.conn.execute('BEGIN IMMEDIATE')  # RESERVED
        else:
            self.conn.execute('BEGIN')
            self.conn.execute('SELECT * FROM state').fetchall()  # SHARED blocks COMMIT (journal_mode=delete)
    def release(self):
        if self.conn is not None:
            self.conn.execute('COMMIT')
            self.conn.close()
            self.conn = None


def _hold_during_consume(monkeypatch, home, mode, holders):
    original = FakeServer.call
    def call(self, method, params=None):
        result = original(self, method, params)
        holders.append(_Holder(home, mode))  # taken after admission committed
        return result
    monkeypatch.setattr(FakeServer, 'call', call)


@pytest.mark.parametrize('mode', ['reader', 'writer'])
def test_finalisation_outlasts_lock_beyond_old_budget(fixture, monkeypatch, mode):
    """A lock held past one full 1 s try (old budget 0.1 s) is ridden out."""
    view = resets.observe(fixture)
    holders, tries, pauses = [], [], []
    _hold_during_consume(monkeypatch, fixture, mode, holders)
    original_state = resets._state
    def state(home, busy=None):
        tries.append(busy)
        return original_state(home, busy)
    monkeypatch.setattr(resets, '_state', state)
    # Released by the first backoff, i.e. only after a try has already failed
    # with the lock held for its whole busy wait. No sleep orders this test.
    monkeypatch.setattr(resets, '_pause', lambda seconds: pauses.append(seconds) or holders[0].release())
    try:
        result = resets.redeem(fixture, expected_binding=view['binding'],
                               expected_episode=view['episode'], expected_count=2)
    finally:
        for holder in holders: holder.release()
    assert result['outcome'] == 'reset' and result['view']['blocked'] is False
    # Admission uses the default 1 s budget; finalisation failed one full try
    # with the lock held throughout, then recorded on the next.
    assert tries == [None, resets.FINALISE_TRY_SECONDS, resets.FINALISE_TRY_SECONDS]
    assert resets.FINALISE_TRY_SECONDS > 0.1 and len(pauses) == 1
    assert _state_rows(fixture) == [(None, 'reset')]
    assert len(_consumes()) == 1
    assert not list(resets._db_path(fixture).parent.glob('outcome-*'))
    after = resets.observe(fixture)
    assert after['blocked'] is False and after['last_outcome'] == 'reset'


def _strand(fixture, monkeypatch, mode='writer'):
    """Redeem while a lock outlasts the whole (shortened) finalisation budget."""
    view = resets.observe(fixture)
    holders = []
    _hold_during_consume(monkeypatch, fixture, mode, holders)
    monkeypatch.setattr(resets, 'FINALISE_SECONDS', 0.3)
    monkeypatch.setattr(resets, 'FINALISE_TRY_SECONDS', 0.15)
    monkeypatch.setattr(resets, '_pause', lambda seconds: None)
    try:
        with pytest.raises(resets.Unrecorded) as raised:
            resets.redeem(fixture, expected_binding=view['binding'],
                          expected_episode=view['episode'], expected_count=2)
        rows = _state_rows(fixture) if mode == 'reader' else None
    finally:
        for holder in holders: holder.release()
    return view, raised.value, rows


@pytest.mark.parametrize('recover', ['observe', 'redeem', 'set_auto'])
def test_lock_beyond_budget_is_unrecorded_then_reconciled_without_second_consume(fixture, monkeypatch, recover):
    view, error, _ = _strand(fixture, monkeypatch)
    assert isinstance(error, resets.Unavailable) and error.reconcilable and error.outcome == 'reset'
    assert str(error) == 'Reset was used but could not be recorded; it will be reconciled on the next check.'
    assert 'Another redemption' not in str(error)
    [(attempt, result)] = _state_rows(fixture)
    assert attempt and result == 'pending'
    journal = resets._db_path(fixture).parent / ('outcome-' + attempt)
    assert journal.is_file() and journal.stat().st_mode & 0o777 == 0o600
    # The provider still reports the old exhausted episode (stale), so only the
    # authority's recorded episode can stop a second spend.
    FakeServer.state = snapshot(count=1, reset=FIXTURE_RESET_AT)
    if recover == 'observe':
        after = resets.observe(fixture)
        assert after['blocked'] is False and after['last_outcome'] == 'reset'
        with pytest.raises(resets.Unavailable, match='Balance or limit changed'):
            resets.redeem(fixture, expected_binding=view['binding'],
                          expected_episode=view['episode'], expected_count=1)
    elif recover == 'redeem':
        # Admission reconciles first, then refuses the already-spent episode.
        with pytest.raises(resets.Unavailable, match='Balance or limit changed'):
            resets.redeem(fixture, expected_binding=view['binding'],
                          expected_episode=view['episode'], expected_count=1)
    else:
        assert resets.set_auto(fixture, view['binding'], True)['auto'] is True
    assert _state_rows(fixture) == [(None, 'reset')]
    assert not journal.exists()
    assert len(_consumes()) == 1
    later = resets.observe(fixture)
    assert later['last_outcome'] == 'reset' and later['blocked'] is False


def test_reader_lock_beyond_budget_rolls_back_and_reconciles(fixture, monkeypatch):
    _, error, rows = _strand(fixture, monkeypatch, mode='reader')
    assert error.reconcilable and rows[0][1] == 'pending'  # COMMIT was refused, write rolled back
    assert resets.observe(fixture)['last_outcome'] == 'reset'
    assert _state_rows(fixture) == [(None, 'reset')] and len(_consumes()) == 1


def test_non_reset_outcome_is_reconciled_as_itself(fixture, monkeypatch):
    def nothing(self, method, params=None):
        self.calls.append(('consume', params['idempotencyKey']))
        return {'outcome': 'nothingToReset'}
    monkeypatch.setattr(FakeServer, 'call', nothing)
    _, error, _ = _strand(fixture, monkeypatch)
    assert error.outcome == 'nothingToReset' and error.reconcilable
    assert str(error).startswith('Codex returned nothingToReset but it could not be recorded')
    assert resets.observe(fixture)['last_outcome'] == 'nothingToReset'
    assert _state_rows(fixture) == [(None, 'nothingToReset')] and len(_consumes()) == 1


def test_late_finaliser_is_idempotent_after_reconciliation(fixture, monkeypatch):
    """A reconciling check can win the race; the finaliser then records nothing twice."""
    view = resets.observe(fixture)
    original_finalise = resets._finalise
    def race(home, key, attempt, outcome, journalled):
        assert journalled
        assert resets.observe(home)['last_outcome'] == 'reset'  # another profile's check
        original_finalise(home, key, attempt, outcome, journalled)
    monkeypatch.setattr(resets, '_finalise', race)
    result = resets.redeem(fixture, expected_binding=view['binding'],
                           expected_episode=view['episode'], expected_count=2)
    assert result['outcome'] == 'reset'
    assert _state_rows(fixture) == [(None, 'reset')] and len(_consumes()) == 1
    with sqlite3.connect(resets._db_path(fixture)) as conn:
        assert conn.execute('SELECT outcome FROM attempts').fetchall() == [('reset',)]


class _Crash(BaseException):
    pass


def test_crash_before_any_record_stays_blocked_as_unrecorded(fixture, monkeypatch):
    view = resets.observe(fixture)
    real_write = resets._write_outcome
    def crash(*_):
        raise _Crash()  # process dies after the RPC returned, before any write
    monkeypatch.setattr(resets, '_write_outcome', crash)
    with pytest.raises(_Crash):
        resets.redeem(fixture, expected_binding=view['binding'],
                      expected_episode=view['episode'], expected_count=2)
    monkeypatch.setattr(resets, '_write_outcome', real_write)
    FakeServer.state = snapshot(count=1)
    # Within the in-flight window a live redeem could still record it.
    inflight = resets.observe(fixture)
    assert inflight['blocked'] and inflight['last_outcome'] == 'pending'
    monkeypatch.setattr(resets, 'INFLIGHT_SECONDS', -1)
    stale = resets.observe(fixture)
    assert stale['blocked'] and not stale['redeemable'] and stale['last_outcome'] == 'unrecorded'
    resets.set_auto(fixture, view['binding'], True)
    assert resets.automatic_tick(fixture, lambda: True) is None
    with pytest.raises(resets.Unavailable, match='blocked'):
        resets.redeem(fixture, expected_binding=view['binding'],
                      expected_episode=view['episode'], expected_count=1)
    # The outcome is never invented: the attempt stays pending in the authority.
    [(attempt, result)] = _state_rows(fixture)
    assert attempt and result == 'pending' and len(_consumes()) == 1


def test_unjournalled_unrecordable_outcome_is_distinct_and_stays_blocked(fixture, monkeypatch):
    monkeypatch.setattr(resets, '_write_outcome', lambda *a: False)
    _, error, _ = _strand(fixture, monkeypatch)
    assert error.outcome == 'reset' and not error.reconcilable
    assert str(error).startswith('Reset was used but could not be recorded; redemption stays blocked.')
    monkeypatch.setattr(resets, 'INFLIGHT_SECONDS', -1)
    view = resets.observe(fixture)
    assert view['blocked'] and view['last_outcome'] == 'unrecorded' and len(_consumes()) == 1


def test_ambiguous_consume_reports_unrecorded_immediately(fixture):
    view = resets.observe(fixture)
    FakeServer.fail_after_consume = True
    with pytest.raises(resets.Unavailable, match='Timeout after send'):
        resets.redeem(fixture, expected_binding=view['binding'],
                      expected_episode=view['episode'], expected_count=2)
    blocked = resets.observe(fixture)
    assert blocked['blocked'] and blocked['last_outcome'] == 'unrecorded'
    [(attempt, result)] = _state_rows(fixture)
    assert attempt and result == 'unrecorded'


def test_forged_or_foreign_journal_is_ignored(fixture, monkeypatch):
    view = resets.observe(fixture)
    FakeServer.fail_after_consume = True
    with pytest.raises(resets.Unavailable):
        resets.redeem(fixture, expected_binding=view['binding'],
                      expected_episode=view['episode'], expected_count=2)
    [(attempt, _)] = _state_rows(fixture)
    journal = resets._db_path(fixture).parent / ('outcome-' + attempt)
    for body, mode in (('other-account\nreset\n', 0o600),
                       (resets._account_key('org-fixture') + '\nreset', 0o600),  # torn
                       (resets._account_key('org-fixture') + '\nsurprise\n', 0o600),
                       (resets._account_key('org-fixture') + '\nreset\n', 0o644)):
        journal.write_text(body)
        journal.chmod(mode)
        assert resets.observe(fixture)['blocked']
        journal.unlink()
    assert _state_rows(fixture)[0][0] == attempt


def test_failed_read_back_after_definite_outcome_reconciles_on_next_check(fixture, monkeypatch):
    view = resets.observe(fixture)
    original = FakeServer.limits
    def limits(self):
        if any(isinstance(x, tuple) for x in self.calls) and not getattr(FakeServer, 'reads_ok', False):
            raise resets.Unavailable('Codex app-server timed out; redemption status may be uncertain.')
        return original(self)
    monkeypatch.setattr(FakeServer, 'limits', limits)
    with pytest.raises(resets.Unrecorded) as raised:
        resets.redeem(fixture, expected_binding=view['binding'],
                      expected_episode=view['episode'], expected_count=2)
    assert raised.value.reconcilable and _state_rows(fixture)[0][1] == 'pending'
    # Clearing always follows a fresh account read: the next check's own read.
    monkeypatch.setattr(FakeServer, 'reads_ok', True, raising=False)
    assert resets.observe(fixture)['last_outcome'] == 'reset'
    assert _state_rows(fixture) == [(None, 'reset')] and len(_consumes()) == 1


def test_unrecorded_route_error_is_distinct_409():
    import importlib.util
    from fastapi import HTTPException
    spec = importlib.util.spec_from_file_location('fixture_reset_error',
        Path(__file__).resolve().parents[1] / 'dashboard/plugin_api.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(HTTPException) as raised:
        module._reset_error(resets.Unrecorded('reset', True))
    assert raised.value.status_code == 409
    assert raised.value.detail == 'Reset was used but could not be recorded; it will be reconciled on the next check.'


def test_short_transactions_use_modest_busy_budget(fixture, monkeypatch):
    timeouts = []
    real = sqlite3.connect
    def connect(*args, **kwargs):
        timeouts.append(kwargs.get('timeout'))
        return real(*args, **kwargs)
    monkeypatch.setattr(resets.sqlite3, 'connect', connect)
    view = resets.observe(fixture)
    resets.set_auto(fixture, view['binding'], False)
    resets.redeem(fixture, expected_binding=view['binding'],
                  expected_episode=view['episode'], expected_count=2)
    assert resets.BUSY_SECONDS == 1.0 and resets.FINALISE_SECONDS == 5.0
    assert timeouts == [1.0, 1.0, 1.0, resets.FINALISE_TRY_SECONDS]


def test_old_shape_authority_upgrades_in_place(fixture, monkeypatch):
    path = resets._db_path(fixture)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    key = resets._account_key('org-fixture')
    conn = sqlite3.connect(path)
    with conn:
        conn.execute('CREATE TABLE state (binding TEXT PRIMARY KEY, episode TEXT, attempt TEXT, result TEXT)')
        conn.execute('CREATE TABLE optin (binding TEXT NOT NULL, profile TEXT NOT NULL, auto INTEGER NOT NULL, PRIMARY KEY(binding,profile))')
        conn.execute('INSERT INTO state VALUES (?,?,?,?)', (key, 'old-episode', None, 'reset'))
        conn.execute('INSERT INTO optin VALUES (?,?,?)', ('fixture-binding', resets._profile_key(fixture), 1))
        conn.execute('INSERT INTO state VALUES (?,?,?,?)', (resets._account_key('org-stuck'), 'e', 'old-attempt', 'pending'))
    conn.close()
    view = resets.observe(fixture)
    assert view['auto'] is True and view['last_outcome'] == 'reset' and view['redeemable']
    for _ in range(2):  # reopening is idempotent
        resets.observe(fixture)
    conn = sqlite3.connect(path)
    try:
        assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {
            'state', 'optin', 'attempts'}
        assert conn.execute('SELECT episode, attempt, result FROM state WHERE binding=?', (key,)).fetchone() == (
            'old-episode', None, 'reset')
    finally:
        conn.close()
    # A pre-upgrade pending attempt has no journal: blocked and unrecorded, never invented.
    class Stuck(FakeServer):
        def __init__(self, token, account): pass
        def limits(self): return snapshot(account='org-stuck')
    with monkeypatch.context() as scoped:
        scoped.setattr(resets, 'AppServer', Stuck)
        scoped.setattr(resets, '_credentials', lambda: ('fixture-token', 'org-stuck', 'stuck-binding'))
        stuck = resets.observe(fixture)
    assert stuck['blocked'] and stuck['last_outcome'] == 'unrecorded'
    assert resets.redeem(fixture, expected_binding=view['binding'], expected_episode=view['episode'],
                         expected_count=2)['outcome'] == 'reset'
    assert len(_consumes()) == 1


def test_credential_resolver_requires_supported_read_only_mode(monkeypatch):
    auth = types.ModuleType('hermes_cli.auth')
    cli = types.ModuleType('hermes_cli')
    cli.auth = auth
    monkeypatch.setitem(sys.modules, 'hermes_cli', cli)
    monkeypatch.setitem(sys.modules, 'hermes_cli.auth', auth)
    calls = []
    auth.resolve_codex_runtime_credentials = lambda **kwargs: calls.append(kwargs) or {'api_key':'not-a-jwt'}
    with pytest.raises(resets.Unavailable): resets._credentials()
    assert calls == [{'read_only': True, 'refresh_if_expiring': False}]
    def unsupported(*, refresh_if_expiring=True):
        raise AssertionError('unsafe fallback')
    auth.resolve_codex_runtime_credentials = unsupported
    with pytest.raises(resets.Unavailable): resets._credentials()


def test_blocked_stdin_write_is_bounded_and_child_reaped(tmp_path, monkeypatch):
    child = tmp_path / 'never-read'
    pidfile = tmp_path / 'pid'
    child.write_text('#!/usr/bin/env python3\nimport os,time\nopen('+repr(str(pidfile))+',"w").write(str(os.getpid()))\ntime.sleep(30)\n')
    child.chmod(0o700)
    monkeypatch.setattr(resets.shutil, 'which', lambda _: str(child))
    monkeypatch.setattr(resets, 'RPC_SECONDS', .2)
    app = resets.AppServer('fixture-token', 'org-fixture')
    with pytest.raises(resets.Unavailable, match='timed out'):
        app.__enter__()
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError): os.kill(pid, 0)
    app = resets.AppServer('fixture-token', 'org-fixture')
    app.proc = subprocess.Popen([str(child)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                start_new_session=True)
    try:
        start = time.monotonic()
        with pytest.raises(resets.Unavailable, match='timed out'):
            app._send({'payload': 'x' * 2_000_000})
        assert time.monotonic() - start < 2
    finally:
        pid = app.proc.pid
        app.__exit__(None, None, None)
    with pytest.raises(ProcessLookupError): os.kill(pid, 0)


def test_cleanup_budget_escalates_and_reaps_uncooperative_fixture(tmp_path, monkeypatch):
    import signal
    import tempfile
    monkeypatch.setattr(resets, 'CLEANUP_GRACE_SECONDS', .05)
    monkeypatch.setattr(resets, 'CLEANUP_KILL_SECONDS', 1)
    app = resets.AppServer('fixture-token', 'org-fixture')
    app.tmp = tempfile.TemporaryDirectory(dir=tmp_path, prefix='fake-app-server-')
    owned_dir = Path(app.tmp.name)
    app.proc = subprocess.Popen([sys.executable, '-u', '-c',
        'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
        'print("ready",flush=True); time.sleep(30)'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True)
    assert app.proc.stdout is not None
    try:
        assert app.proc.stdout.readline() == b'ready\n'
        pid = app.proc.pid
        app.__exit__(None, None, None)
        assert app.proc.returncode == -signal.SIGKILL
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert not owned_dir.exists()
    finally:
        if app.proc.poll() is None:
            app.proc.kill()
            app.proc.wait(timeout=2)
        app.__exit__(None, None, None)


def test_token_claims_require_identity_and_expiry():
    def jwt(claims):
        return 'a.' + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=') + '.sig'
    base = {'sub': 'user-fixture', 'exp': time.time()+300,
            'https://api.openai.com/auth': {'chatgpt_account_id': 'org-fixture'}}
    assert resets._principal(jwt(base)) == ('org-fixture', 'user-fixture')
    for bad in ({**base, 'sub': ''}, {**base, 'exp': 1},
                {**base, 'https://api.openai.com/auth': {}}):
        with pytest.raises(resets.Unavailable): resets._principal(jwt(bad))


def test_real_stdio_rpc_against_disposable_fake_codex(tmp_path, monkeypatch):
    """Exercise the actual process/RPC/cleanup seam, but never launch Codex."""
    script = tmp_path / 'fake-codex'
    script.write_text('''#!/usr/bin/env python3
import json, sys, os
assert 'UNRELATED_SECRET_SENTINEL' not in os.environ
for line in sys.stdin:
    req=json.loads(line)
    if 'id' not in req: continue
    method=req['method']
    if method=='initialize': value={}
    elif method=='account/login/start':
        assert req['params']['type']=='chatgptAuthTokens'
        assert req['params']['accessToken']=='fixture-token'
        assert req['params']['chatgptAccountId']=='org-fixture'
        value={'type':'chatgptAuthTokens'}
    elif method=='account/read': value={'account':{'type':'chatgpt'}}
    elif method=='account/rateLimits/read':
        value={'accountId':'org-fixture','ordinaryUsageAllowed':False,
          'rateLimitResetCredits':{'availableCount':1},
          'rateLimits':{'limitId':'codex','rateLimitReachedType':'rate_limit_reached',
           'primary':{'usedPercent':100,'windowDurationMins':300,'resetsAt':9999999999}}}
    elif method=='account/rateLimitResetCredit/consume':
        assert req['params']['idempotencyKey']=='fixture-attempt'
        value={'outcome':'nothingToReset'}
    else: raise AssertionError(method)
    print(json.dumps({'id':req['id'],'result':value}),flush=True)
''')
    script.chmod(0o700)
    monkeypatch.setenv('UNRELATED_SECRET_SENTINEL','must-not-reach-child')
    monkeypatch.setattr(resets.shutil,'which',lambda _: str(script))
    original = resets.AppServer
    with original('fixture-token','org-fixture') as app:
        result = app.limits()
        assert result['accountId']=='org-fixture'
        assert app.call('account/rateLimitResetCredit/consume',{'idempotencyKey':'fixture-attempt'}) == {'outcome':'nothingToReset'}
    assert not any(tmp_path.glob('codex-resets-*'))


def test_timeout_kills_disposable_appserver_process(tmp_path,monkeypatch):
    import os
    script=tmp_path/'hung-codex'
    pidfile=tmp_path/'pid'
    script.write_text('#!/usr/bin/env python3\nimport os,time\nopen('+repr(str(pidfile))+',"w").write(str(os.getpid()))\ntime.sleep(30)\n')
    script.chmod(0o700)
    monkeypatch.setattr(resets.shutil,'which',lambda _:str(script))
    monkeypatch.setattr(resets,'RPC_SECONDS',0.2)
    with pytest.raises(resets.Unavailable,match='timed out'):
        with resets.AppServer('fixture-token','org-fixture'):
            pass
    pid=int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid,0)


def test_mounted_plugin_routes_are_profile_scoped(tmp_path, fixture, monkeypatch):
    import importlib.util
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('fixture_codex_routes',root/'dashboard/plugin_api.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, '_profile_rows', lambda: [
        {'name':'fixture','path':str(tmp_path),'is_server':True},
        {'name':'other','path':str(tmp_path/'other'),'is_server':False}])
    # Exercise the real ContextVar boundary rather than bypassing it.
    import hermes_constants
    assert module._run_reset_home(tmp_path, lambda: Path(hermes_constants.get_hermes_home()).resolve()) == tmp_path.resolve()
    app = FastAPI()
    app.include_router(module.router, prefix='/api/plugins/ai-usage-tracker')
    client = TestClient(app)
    path='/api/plugins/ai-usage-tracker/codex/resets'
    assert client.get(path+'?profile=missing').status_code == 404
    observed=client.get(path+'?profile=fixture')
    assert observed.status_code==200
    data=observed.json()
    assert data['profile']=='fixture' and data['count']==2 and not data['auto']
    assert client.post(path+'/auto',json={'profile':'fixture','binding':'other','enabled':True}).status_code==409
    opt=client.post(path+'/auto',json={'profile':'fixture','binding':data['binding'],'enabled':True})
    assert opt.status_code==200
    assert client.get(path+'?profile=fixture').json()['auto'] is True
    body={'profile':'fixture','binding':data['binding'],'episode':data['episode'],'count':2}
    assert client.post(path+'/redeem',json={**body,'count':0}).status_code==400
    done=client.post(path+'/redeem',json=body)
    assert done.status_code==200 and done.json()['outcome']=='reset'
    assert client.post(path+'/redeem',json=body).status_code==409
    assert len([x for x in FakeServer.calls if isinstance(x,tuple)])==1
    calls_before = list(FakeServer.calls)
    monkeypatch.setitem(sys.modules, 'hermes_constants', None)
    assert client.get(path+'?profile=fixture').status_code == 503
    assert client.post(path+'/auto',json={'profile':'fixture','binding':data['binding'],'enabled':False}).status_code == 503
    assert client.post(path+'/redeem',json=body).status_code == 503
    assert FakeServer.calls == calls_before
    monkeypatch.setitem(sys.modules, 'hermes_constants', hermes_constants)
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: tmp_path / 'wrong-profile')
    assert client.get(path+'?profile=fixture').status_code == 503
    assert client.post(path+'/redeem',json=body).status_code == 503
    assert FakeServer.calls == calls_before


def test_installed_hermes_config_gate_controls_mounted_monitor(tmp_path, monkeypatch):
    """Exercise server-profile YAML policy with fixture-only profiles."""
    import importlib.util
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    server_home = tmp_path / 'profiles' / 'server'
    opted_home = tmp_path / 'profiles' / 'opted'
    server_home.mkdir(parents=True)
    opted_home.mkdir(parents=True)
    config = server_home / 'config.yaml'
    config.write_text(json.dumps({'plugins': {'enabled': [], 'disabled': []}}))
    (opted_home / 'config.yaml').write_text(json.dumps(
        {'plugins': {'enabled': [], 'disabled': ['ai-usage-tracker']}}))
    monkeypatch.setenv('HERMES_HOME', str(server_home))
    scoped = contextvars.ContextVar('monitor_home', default=None)
    constants = types.ModuleType('hermes_constants')
    setattr(constants, 'set_hermes_home_override', scoped.set)
    setattr(constants, 'reset_hermes_home_override', scoped.reset)
    setattr(constants, 'get_hermes_home', scoped.get)
    setattr(constants, 'get_process_hermes_home', lambda: server_home)
    monkeypatch.setitem(sys.modules, 'hermes_constants', constants)
    monkeypatch.setattr(resets, '_coord_root', lambda home: tmp_path)
    monkeypatch.setattr(resets, '_credentials', lambda: ('fixture-token', 'org-fixture', 'fixture-binding'))
    monkeypatch.setattr(resets, 'AppServer', FakeServer)
    monkeypatch.setattr(resets, 'POLL_SECONDS', .025)
    setattr(FakeServer, 'state', snapshot(reset=int(time.time()) + 3600))
    FakeServer.calls = []
    FakeServer.fail_after_consume = False
    view = resets.observe(opted_home)
    FakeServer.calls = []

    spec = importlib.util.spec_from_file_location('fixture_installed_gate',
        Path(__file__).resolve().parents[1] / 'dashboard/plugin_api.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, '_profile_rows', lambda: [
        {'name': 'opted', 'path': str(opted_home), 'is_server': False}])
    app = FastAPI()
    app.include_router(module.router, prefix='/api/plugins/ai-usage-tracker')

    def wait_for_consume(count):
        # Wait for the redemption's post-consume read-back too, so later call
        # snapshots are not racing the tail of that redemption.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            calls = list(FakeServer.calls)
            if (len([x for x in calls if isinstance(x, tuple)]) == count
                    and calls[-1] == 'read' and isinstance(calls[-2], tuple)):
                return
            time.sleep(.01)
        pytest.fail('opted-in fixture did not consume after enabling the real config gate')

    with TestClient(app):
        assert not module._reset_plugin_enabled()
        time.sleep(.12)
        assert FakeServer.calls == []  # no opt-in credential read when plugin is off
        config.write_text(json.dumps({'plugins': {'enabled': ['ai-usage-tracker'], 'disabled': []}}))
        assert module._reset_plugin_enabled()
        time.sleep(.12)
        assert FakeServer.calls == []  # enabled alone never touches credentials
        resets.set_auto(opted_home, view['binding'], True)
        wait_for_consume(1)
        config.write_text(json.dumps({'plugins': {'enabled': ['ai-usage-tracker'],
                                                 'disabled': ['ai-usage-tracker']}}))
        assert not module._reset_plugin_enabled()  # deny-list wins
        setattr(FakeServer, 'state', snapshot(count=1, reset=int(time.time()) + 7200))
        calls = list(FakeServer.calls)
        time.sleep(.12)
        assert FakeServer.calls == calls
        config.write_text(json.dumps({'plugins': ['unknown']}))
        assert not module._reset_plugin_enabled()  # malformed gate fails closed
        time.sleep(.12)
        assert FakeServer.calls == calls
        config.write_text(json.dumps({'plugins': {'enabled': ['ai-usage-tracker'], 'disabled': []}}))
        assert module._reset_plugin_enabled()
        wait_for_consume(2)
    assert resets._worker is not None and not resets._worker.is_alive()


def test_actual_installed_hermes_config_gate_contract(tmp_path):
    """Installed profile resolution and repeated policy reads never mutate fixture trees."""
    hermes_python = Path.home() / '.hermes/hermes-agent/venv/bin/python'
    if not hermes_python.is_file():
        pytest.skip('installed Hermes interpreter unavailable')
    home = tmp_path / 'profiles' / 'server'
    other = tmp_path / 'profiles' / 'other'
    home.mkdir(parents=True)
    other.mkdir(parents=True)
    config = home / 'config.yaml'
    (other / 'config.yaml').write_text('plugins:\n  enabled: []\n')
    code = '''import hashlib, importlib.util, os, pathlib, sys
from hermes_constants import (get_process_hermes_home, set_hermes_home_override,
                              reset_hermes_home_override, get_hermes_home)
home = pathlib.Path(sys.argv[1]); config = home / 'config.yaml'
root = home.parent.parent
spec = importlib.util.spec_from_file_location('fixture_real_gate', sys.argv[2])
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
assert get_process_hermes_home() == home

def inventory():
    entries = {str(p.relative_to(root)): ('directory', p.stat().st_mtime_ns,
               p.stat().st_mode) for p in root.rglob('*') if p.is_dir()}
    entries.update({str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(),
            p.stat().st_mtime_ns, p.stat().st_mode) for p in root.rglob('*') if p.is_file()})
    return entries

def check(text, expected):
    if text is None:
        config.unlink(missing_ok=True)
    else:
        config.write_text(text)
    before = inventory()
    for _ in range(6):
        assert module._reset_plugin_enabled() is expected, repr(text)
        assert inventory() == before, 'activation read changed fixture tree'

check(None, False)
missing_home = root / 'missing-server'
os.environ['HERMES_HOME'] = str(missing_home)
before = inventory()
for _ in range(6):
    assert module._reset_plugin_enabled() is False
    assert inventory() == before
assert not missing_home.exists()
os.environ['HERMES_HOME'] = str(home)
check('', False)
check('plugins:\\n  enabled: [ai-usage-tracker]\\n', True)  # absent disabled = empty
check('plugins:\\n  enabled: []\\n', False)
check('plugins:\\n  enabled: [ai-usage-tracker]\\n  disabled: [ai-usage-tracker]\\n', False)
check('plugins:\\n  enabled: [ai-usage-tracker]\\n  disabled: []\\n', True)
# An active request for a different profile never switches the monitor's authority.
token = set_hermes_home_override(home.parent / 'other')
try:
    assert get_hermes_home() == home.parent / 'other'
    before = inventory()
    assert module._reset_plugin_enabled() is True
    assert inventory() == before
finally:
    reset_hermes_home_override(token)
check('plugins: [ai-usage-tracker]\\n', False)
check('plugins:\\n  enabled: ai-usage-tracker\\n', False)
check('plugins:\\n  enabled: [ai-usage-tracker]\\n  disabled: unknown\\n', False)
check('plugins:\\n  enabled: [ai-usage-tracker, 1]\\n', False)
check('plugins:\\n  enabled: [ai-usage-tracker]\\n  enabled: []\\n', False)
check('plugins:\\n  enabled: [ai-usage-tracker]\\nplugins: {}\\n', False)
check('plugins: [\\n', False)  # invalid YAML after a valid allow-list
check('plugins:\\n  enabled: [ai-usage-tracker]\\ninvalid: [\\n', False)
check('plugins:\\n  enabled: [ai-usage-tracker]\\n  disabled: []\\n' + ' ' * (4 * 1024 * 1024), False)
print('installed gate: fixture-only, no writes, server profile, fail-closed')
'''
    env = {'HOME': str(tmp_path), 'HERMES_HOME': str(home),
           'PATH': os.environ.get('PATH', '/usr/bin:/bin')}
    result = subprocess.run([str(hermes_python), '-c', code, str(home),
                             str(Path(__file__).resolve().parents[1] / 'dashboard/plugin_api.py')],
                            env=env, cwd=str(tmp_path), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'installed gate: fixture-only, no writes, server profile, fail-closed'


def test_malformed_plugin_config_never_logs_secret_or_exception(tmp_path, monkeypatch, caplog):
    """YAML parser diagnostics can quote the offending config line at DEBUG."""
    import importlib.util
    import logging

    home = tmp_path / 'server'
    home.mkdir()
    config = home / 'config.yaml'
    config.write_text('plugins:\n  enabled: [ai-usage-tracker]\n')
    constants = types.ModuleType('hermes_constants')
    setattr(constants, 'get_process_hermes_home', lambda: home)
    monkeypatch.setitem(sys.modules, 'hermes_constants', constants)
    spec = importlib.util.spec_from_file_location('fixture_secret_safe_gate',
        Path(__file__).resolve().parents[1] / 'dashboard/plugin_api.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._reset_plugin_enabled() is True

    sentinel = 'SENTINEL_PRIVATE_KEY_DO_NOT_LOG'
    config.write_text('plugins:\n  enabled: [ai-usage-tracker]\nprivate_key: ['
                      + sentinel + '\n')
    with caplog.at_level(logging.DEBUG, logger=module.log.name):
        assert module._reset_plugin_enabled() is False
    records = [record for record in caplog.records
               if record.name == module.log.name and 'Codex reset monitor' in record.getMessage()]
    assert len(records) == 1
    assert records[0].getMessage() == 'Codex reset monitor could not verify plugin activation'
    assert records[0].exc_info is None and records[0].stack_info is None
    assert records[0].args == ()
    assert sentinel not in caplog.text
    assert 'Traceback' not in caplog.text


def test_reset_observer_start_failure_logs_no_exception(monkeypatch, caplog):
    import asyncio
    import importlib.util
    import logging

    spec = importlib.util.spec_from_file_location('fixture_secret_safe_start',
        Path(__file__).resolve().parents[1] / 'dashboard/plugin_api.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sentinel = 'SENTINEL_STARTUP_SECRET_DO_NOT_LOG'

    def fail_start(*_):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(resets, 'start_monitor', fail_start)
    monkeypatch.setattr(resets, 'stop_monitor', lambda: None)

    async def run():
        async with module._reset_lifespan(None):
            pass

    with caplog.at_level(logging.WARNING, logger=module.log.name):
        asyncio.run(run())
    records = [record for record in caplog.records if record.name == module.log.name]
    assert len(records) == 1
    assert records[0].getMessage() == 'Codex reset observer unavailable'
    assert records[0].args == ()
    assert records[0].exc_info is None and records[0].stack_info is None
    assert sentinel not in caplog.text and 'Traceback' not in caplog.text


def test_backend_monitor_redeems_without_an_open_page(fixture, monkeypatch):
    view=resets.observe(fixture)
    resets.set_auto(fixture,view['binding'],True)
    monkeypatch.setattr(resets,'POLL_SECONDS',0.02)
    resets.start_monitor(lambda:[{'path':str(fixture)}],lambda home,fn:fn(),lambda:True)
    try:
        deadline=time.monotonic()+2
        while time.monotonic()<deadline and not any(isinstance(x,tuple) for x in FakeServer.calls):
            time.sleep(0.02)
        assert len([x for x in FakeServer.calls if isinstance(x,tuple)])==1
        assert resets.observe(fixture)['count']==1
    finally:
        resets.stop_monitor()


def test_mounted_fastapi_lifespan_starts_monitor_for_opted_profile(fixture, monkeypatch):
    import importlib.util
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    view=resets.observe(fixture)
    resets.set_auto(fixture,view['binding'],True)
    monkeypatch.setattr(resets,'POLL_SECONDS',0.02)
    root=Path(__file__).resolve().parents[1]
    spec=importlib.util.spec_from_file_location('fixture_codex_lifespan',root/'dashboard/plugin_api.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    monkeypatch.setattr(module,'_profile_rows',lambda:[{'path':str(fixture),'name':'fixture','is_server':True}])

    monkeypatch.setattr(module,'_reset_plugin_enabled',lambda:True)
    app=FastAPI()
    app.include_router(module.router,prefix='/api/plugins/ai-usage-tracker')
    with TestClient(app):
        deadline=time.monotonic()+2
        while time.monotonic()<deadline and not any(isinstance(x,tuple) for x in FakeServer.calls):
            time.sleep(0.02)
        assert len([x for x in FakeServer.calls if isinstance(x,tuple)])==1
    assert resets._worker is not None and not resets._worker.is_alive()
