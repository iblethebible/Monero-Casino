# F07–F10: deposit rollout and reconciliation

The scanner no longer sweeps funds or contains a destination address. Deposits
remain in account 0 of the configured spending wallet. Run all web and worker
processes against the same primary MySQL database and wallet/daemon network.

## Install

Stop old web and worker processes and back up the database. After migration 001,
apply `migrations/002_deposit_safety.sql` once. For an empty sandbox, first apply
`sql/baseline.sql`, then both migrations in numeric order. MySQL DDL commits
implicitly; inspect partial application before retrying a failed migration.
This adds deposit observations, scan history and wallet spending holds.

Export the shared `.env.example` settings into each process. Set
`MONERO_DEPOSIT_MIN_CONFIRMATIONS` (default 10). Run `python check_deposits.py`
periodically under a supervised scheduler and monitor failures and review rows.
The job reads full account-0 history twice and requires a synchronized wallet,
stable daemon tip, matching recipient address, sufficient confirmations, no
double-spend flag, an unlocked transfer and zero custom unlock time. Transfers
with custom unlock times remain uncredited for manual investigation.

## Accounting and recovery

Each transaction/account/subaddress combination has one observation. The RPC
amount is already aggregated for that recipient; component amounts are validated,
not added again. Duplicate recipient entries abort the snapshot. Credits, ledger
entries, house accounting and completed scan status commit together. Replays and
concurrent scanner invocations cannot credit the same observation twice.

`deposits.tx_hash` holds the actual blockchain hash. `transaction_id` links its
ledger entry, whose unique `transactions.tx_hash` is `deposit:<observation id>`.
This allows one blockchain transaction to pay multiple users. Legacy hash-only
deposit ledger entries trigger review instead of another credit. Existing balances
and historical rows are not automatically rewritten.

An uncredited transfer absent from a successful full snapshot becomes `orphaned`
and can be credited if it returns and qualifies. A credited transfer that disappears,
changes block or loses eligibility becomes `review`. Its wallet is held without
silently reversing historical balances. Failed or changing snapshots cannot orphan
deposits or change balances. Scanner and withdrawal processor share a spending lock
when applying holds, preventing relay from racing the installation of a hold.

Held wallets cannot place new bets or reserve/send withdrawals. Any customer hold
also pauses house withdrawals. Previously prepared withdrawals remain reserved;
an already-broadcast hash can still finalize accounting. Existing bets may settle.
Holds never clear automatically, even if a transaction returns.

Review with:

```sql
SELECT id, user_id, tx_hash, amount, block_height, state, credited_at,
       transaction_id FROM deposits WHERE state='review';
SELECT id, user_id FROM wallets WHERE deposit_hold=1;
SELECT id, state, error, started_at, completed_at
FROM deposit_scan_runs ORDER BY started_at DESC LIMIT 20;
```

There is no reconciliation UI yet. An operator must verify wallet and canonical
chain evidence, original recipient allocations, ledger credits, bets and pending
withdrawals before repairing records and clearing a hold. Resolve all review rows
for a wallet together. Never clear a hold merely to resume withdrawals, or remove
an old hash-only ledger row to force automatic credit. Preserve an audit trail and
backups for every manual correction.

## Validation and limits

Validation on 28 September 2026: the full 67-test suite passed with zero skips,
including 30 disposable-MySQL integration tests. Two additional empty-response
boundary cases then passed in the expanded 14-test snapshot suite (69 distinct
tests validated in total). No live Monero funds were used.

Tests use disposable MySQL with mocked wallet/daemon responses. Run
`python -B -m unittest discover -s tests -v`; enable database tests using the
disposable-database environment described in CRITICAL-FIXES.md. Apply both
migrations before enabling them. Live stagenet testing remains necessary,
including replay, multiple recipients, confirmations, process restarts and holds.

Full-history scans prioritize correctness and may need performance work at scale.
The scanner trusts the configured RPC services and restored wallet history; their
network isolation and correct configuration remain essential. Confirmation depth
cannot eliminate reorg risk. The broader F15 solvency/accounting model and remaining
audit findings are not fixed by this batch. These changes are not launch approval.
