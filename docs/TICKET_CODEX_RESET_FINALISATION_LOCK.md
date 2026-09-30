# Ticket: codex reset finalisation can strand a completed redemption as pending

**Status: fixed in `COMMIT_PENDING` on branch `fix/codex-reset-finalisation`** (repository candidate; not merged, installed or exercised against a live account). The symptom, triggers and reproduction below describe the pre-fix code at `68515de`; the design summary is under [Resolution](#resolution).

## Symptom

In `redeem()` ([`ledger_runtime/codex_resets.py`](../ledger_runtime/codex_resets.py)), the pending attempt is committed, then the irreversible `account/rateLimitResetCredit/consume` RPC runs outside any SQLite transaction. Finalisation reopens `_state()` (`sqlite3.connect(..., timeout=0.1)` + `BEGIN IMMEDIATE`). If that finalisation hits `database is locked` beyond the 0.1 s busy timeout after the service already returned `outcome: 'reset'`:

- the `sqlite3.OperationalError` is converted to `Unavailable('Another redemption is in progress.')`, which `dashboard/plugin_api.py` `_reset_error()` returns as HTTP 409;
- the account's row stays `(attempt, 'pending')`;
- `observe()` then reports `blocked=True`, `redeemable=False`, `last_outcome='pending'` indefinitely;
- there is no UI/API recovery path. Only a successful finalisation of the same attempt clears `attempt` (the `UPDATE state SET attempt=NULL ... WHERE binding=? AND attempt=?` at `codex_resets.py:395`), and no caller retries it.

The user sees a failed redemption that actually spent a banked reset, then a permanently blocked card.

## Triggers

Any other connection holding the authority database for more than 0.1 s during finalisation:

- another profile's `observe()` poll or `set_auto()` (both take `BEGIN IMMEDIATE`);
- the 30 s monitor loop's read-only `optin` query in `start_monitor()`;
- fsync latency on the authority's filesystem lengthening any short transaction.

The database uses `journal_mode=delete`, so even a reader's SHARED lock blocks the finaliser's `COMMIT`; readers are not harmless.

## Severity

Medium. Fails closed: the pending key keeps blocking further redemption, so there is no double spend. The cost is a misreported outcome and a stuck account with no in-product recovery.

## Fix direction (as proposed before the fix)

1. Give post-spend finalisation its own longer, bounded busy timeout and/or retry loop, separate from observe/admission.
2. If finalisation still cannot be recorded, return a distinct outcome such as "reset used but could not be recorded" instead of the generic 409 "Another redemption is in progress." message, and make it recoverable later (for example by a finalise-on-next-observe path keyed to the same attempt).
3. Give `observe()`/admission a modest busy timeout, so routine cross-profile polls do not 409 on another account's short transaction (the same limitation noted in [tests/AGENTS.md](../tests/AGENTS.md)).
4. Add a regression test that holds a reader (SHARED) and, separately, a writer (RESERVED) lock across finalisation, and asserts the attempt is cleared and the outcome is `reset`.

## Evidence: reproduction sketch

Independent review of `0077690` reproduced this with a scratch probe against a disposable authority (fake app-server, patched credentials and coordination root; no live account). Both lock modes gave the same result:

```text
redeem raised Unavailable: Another redemption is in progress.
state rows: [(1, 'pending')]
observe after: {'blocked': True, 'redeemable': False, 'last_outcome': 'pending'}
journal_mode: ('delete',)
```

Essence of the probe, runnable from the repository root with the test conftest's module aliasing:

```python
import os, sqlite3, tempfile, threading, time
from pathlib import Path
os.environ['HERMES_USAGE_PRICING_OFFLINE'] = '1'
# exec tests/conftest.py first so _hermes_ai_usage_ledger_v2 resolves to ledger_runtime
from _hermes_ai_usage_ledger_v2 import codex_resets as r

root = tempfile.mkdtemp(); os.chmod(root, 0o700)
r._coord_root = lambda home: Path(root)
r._credentials = lambda: ('t', 'acct', 'b')
reset_at = int(time.time()) + 3600
snap = lambda: {'accountId': 'acct', 'ordinaryUsageAllowed': False,
                'rateLimitResetCredits': {'availableCount': 2, 'credits': None},
                'rateLimits': {'limitId': 'codex', 'rateLimitReachedType': 'rate_limit_reached',
                               'primary': {'usedPercent': 100, 'windowDurationMins': 300,
                                           'resetsAt': reset_at}}}
MODE = 'reader'  # or 'writer'

class FakeServer:
    def __init__(self, *a): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def limits(self): return snap()
    def call(self, method, params=None):
        # Another connection takes a lock that outlasts 0.1 s across finalisation.
        other = sqlite3.connect(r._db_path(root), isolation_level=None, check_same_thread=False)
        if MODE == 'writer':
            other.execute('BEGIN IMMEDIATE')
        else:
            other.execute('BEGIN'); other.execute('SELECT * FROM state').fetchall()  # SHARED
        threading.Timer(0.3, lambda: (other.execute('COMMIT'), other.close())).start()
        return {'outcome': 'reset'}

r.AppServer = FakeServer
v = r.observe(root)
try:
    r.redeem(root, expected_binding=v['binding'], expected_episode=v['episode'], expected_count=2)
except r.Unavailable as e:
    print('redeem raised Unavailable:', e)      # although the service returned 'reset'
time.sleep(0.5)
print(sqlite3.connect(r._db_path(root)).execute('SELECT attempt IS NOT NULL, result FROM state').fetchall())
print({k: r.observe(root)[k] for k in ('blocked', 'redeemable', 'last_outcome')})
```

## Scope

- Not a regression from `0077690`; that commit is test-only. `test_different_accounts_can_consume_concurrently` deliberately sequences transactions and does not cover this.
- The user approved the product change recorded below.

## Resolution

Implemented in [`ledger_runtime/codex_resets.py`](../ledger_runtime/codex_resets.py); the binding contract lives in [ledger runtime DOX](../ledger_runtime/AGENTS.md).

- **Journal before recording.** As soon as consume returns a definite outcome, it is written once to `codex-reset-authority/outcome-<attempt>` (O_EXCL, 0600, fsynced, bound to the hashed account key). This needs no SQLite lock, so no reader or writer can delay it.
- **Bounded post-spend finalisation.** After the limits read-back, `_finalise` records the outcome with its own budget: up to 5 s of ≤1 s `BEGIN IMMEDIATE` tries with 50/250 ms backoff (bounded try count). Every try re-verifies that the row still holds the same attempt id. Recording is idempotent per attempt through a new `attempts` table, so a racing reconciler and the finaliser never record twice.
- **Distinct failure.** If recording still fails, or the read-back fails, `redeem()` raises `Unrecorded` (HTTP 409): "Reset was used but could not be recorded; it will be reconciled on the next check." It never reports "Another redemption is in progress." for a completed spend. The row stays pending.
- **Reconciliation on the next check.** `observe()`, `set_auto()` and admission reconcile a pending attempt whose journal matches it: they record the outcome, clear the attempt and delete the journal. The episode remains spent, so no second consume can follow, even if the provider still reports the old exhaustion.
- **Unknown stays unknown.** A pending attempt without a valid journal (crash between RPC and journal, uncertain RPC, pre-upgrade row) is never given an outcome. It stays blocked and reads `last_outcome: 'unrecorded'` once no live redeem can still record it. There is still no in-product clear for it; no new destructive action was added. The desktop badge keeps its existing truthful "Redemption status is uncertain" blocked tooltip.
- **Busy budget.** Observe, opt-in and admission use a 1 s busy timeout instead of 0.1 s; admission keeps `BEGIN IMMEDIATE` and admits exactly one attempt. The monitor's read-only opt-in query closes its connection at once.
- **Schema.** Additive only: `CREATE TABLE IF NOT EXISTS attempts` on every open. The `state` and `optin` shapes and old rows are unchanged, with no offline migration.

Verification (implementer-run, synthetic only): the reproduction probe above now prints `redeem: reset`, `[(0, 'reset')]` and `blocked: False` for both lock modes. `tests/test_codex_resets.py` covers reader and writer locks across finalisation, a lock beyond the budget with reconciliation by observe/admission/opt-in and no second consume, crash and unjournalled cases staying `unrecorded`, forged journals, the idempotent race and an old-shape database upgrade.
