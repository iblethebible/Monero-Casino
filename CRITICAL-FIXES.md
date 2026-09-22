# F01–F04: rollout and recovery

These changes fix the audited session forgery, premature settlement, unrecorded
withdrawal and concurrent balance-debit paths. They are not certification that
the remaining audited deposit, custody, accounting or application issues are fixed.

## Database preparation

Use MySQL 8.0.16+ (CHECK enforcement required); changes were tested on MySQL 8.4.
`sql/baseline.sql` is **schema only**, extracted from the supplied March 2025 dump.
No usernames, password hashes, wallet addresses or transaction rows from that dump
are committed. Do not run baseline.sql on an existing database.

For an empty sandbox, create a database then apply `sql/baseline.sql`, followed by
`migrations/001_critical_money_safety.sql`. For an existing deployment, back up the
database, stop **all** old web and worker processes, and apply **only** the migration.
The migration is deliberately apply-once, not an automatic startup operation;
MySQL DDL commits implicitly. Record that it succeeded before restarting writers.
If a statement fails, inspect the applied DDL before retrying; do not replay the
whole file blindly.

The migration allows null pending outcomes, widens balances/payouts to consistent
precision, adds a nonnegative available-balance constraint and unique house label,
and creates durable withdrawal reservations. Existing negative balances or
duplicate house labels must be investigated before migration, not deleted to make
the constraint pass. Historical payouts are never automatically reversed.

Seed a `house` admin_wallets row in a new sandbox and map each user to its own
wallet through users.wallet_id. The supplied dump has a regular user's
admin_wallet_id unset; settlement now looks up the house explicitly. Check for
legacy stranded wagers using:

```sql
SELECT b.id, b.user_id, bl.block_height, bl.block_hash, bl.resolved_at
FROM bets b JOIN blocks bl ON bl.id=b.block_id
WHERE b.bet_status='pending' AND bl.resolved_at IS NOT NULL;
```

Investigate/refund/repair such legacy records separately. Do not reset settled
rounds wholesale: that could pay historical wins again. The new resolver can
repair an **unresolved** placeholder using a confirmed canonical daemon header.

## Configuration and startup

Install Python 3.12 and `python -m pip install -r requirements.txt` in a virtual
environment. `.env.example` lists process environment variables; merely copying
it to `.env` does not load it.

`FLASK_SECRET_KEY` is now mandatory. Generate a new random secret, store it outside
Git and share the same value across web workers. Startup rejects missing, short,
low-variation and retired keys. Old signed cookies become invalid; do not configure
the previous key as a fallback. All users must log in again.

For local HTTP QA in PowerShell, after setting database and RPC environment values:

```powershell
$env:FLASK_SECRET_KEY = python -c "import secrets; print(secrets.token_urlsafe(48))"
$env:FLASK_SESSION_COOKIE_SECURE = 'false'
python app.py
```

The app now binds loopback with debugging off. In production keep Secure cookies
enabled, use HTTPS and a production WSGI server. Configure `DB_*` for web, resolver,
sync and withdrawal worker. Daemon and wallet RPC can use HTTP Digest credentials
through their respective `MONERO_*_RPC_USER/PASSWORD` variables; protect their
network access and transport.

`check_deposits.py` remains outside this fix's scope and still has its separate
hardcoded configuration and audited defects. Do not assume `.env.example` changes
its destination or database. Use disposable stagenet assets for QA. Other audit
findings still block a real-money launch.

## Round settlement

New bets reserve their stake and insert their row in one transaction. They have
no synthetic result. The round is locked, checked for closure and checked again
against the current daemon height before acceptance. Balance debits are conditional
on sufficient available funds; short deadlock/lock-timeout retries occur only after
full rollback, never around an external broadcast.

The daemon must report a synchronized, fresh tip. Default maximum tip age is 1,200
seconds; exceptional natural mining gaps can pause betting until a fresh block.
Rounds settle at 10 confirmations by default (`tip-height+1`), configurable with
`MONERO_BET_MIN_CONFIRMATIONS`. Cutoff is at least two blocks before the chosen
height. The resolver fetches the canonical header while holding the round lock,
derives the outcome, locks pending bets, and commits all changes atomically. Running
it twice or concurrently does not pay twice. Payouts round down to atomic units.

Run `python bet_resolver.py` periodically with a supervised scheduler. Optional
`python sync_blocks.py` refreshes all unresolved eligible round hashes; neither
script relies on a last-50-block window. Sync no longer populates unused historical
rounds merely for statistics. A failure rolls back the active round and exits;
monitor exit codes and restart only after correcting persistent errors.

## Withdrawal state and recovery

Both user and admin forms carry a stable request ID. Repeating that request is
idempotent, and changing its amount/address is rejected. A new request reserves
principal in the database before asking the wallet to construct a transaction.

1. **reserved:** balance already debited; wallet may prepare with
   `do_not_relay=true` and `get_tx_metadata=true`. A definite prepare rejection
   releases the reservation once and marks it failed. A timeout stays reserved.
2. **prepared:** signed metadata, hash and actual fee committed **before relay**.
   Retry must use only that same metadata/hash. A crash or ambiguous relay result
   never releases funds or constructs a replacement transaction.
3. **submitted:** wallet acknowledged relay or its outgoing/pending history proves
   the saved hash was sent. Ledger record, house adjustment and status commit
   together. This means submitted, not final blockchain confirmation.
4. **failed:** prepare was rejected before any relay; principal was restored.
   Retry of this same ID remains failed. A genuinely new request needs a new ID.

The web request attempts immediate processing. Run **`python withdrawals.py`**
periodically (for example every 30 seconds under a supervised scheduler) to recover
after crashes or timeouts; it processes up to 100 pending records per invocation.
An advisory MySQL lock prevents simultaneous withdrawal processors on the same
server. A prepared unresolved transaction blocks new preparations, avoiding
conflicting use of wallet inputs. Keep web and workers on the same primary database
and same wallet; do not point one at a replica or a different wallet.

Monitor:

```sql
SELECT id, request_id, user_id, kind, amount, state, tx_hash, last_error, updated_at
FROM withdrawals WHERE state IN ('reserved','prepared') ORDER BY created_at;
```

An old prepared record needs operator investigation against the saved hash and
wallet/daemon history. Do not manually refund it, delete it, replace its metadata,
or submit a new transfer just because a timeout occurred. Do not roll back code to
the old direct-transfer version while reservations exist. Back up the withdrawal
table with the database and protect tx_metadata like spending material. Do not
display it in logs, UI or support exports.

Fees are recorded on the withdrawal and charged to the house, preserving the
requested destination amount. This does **not** fix the broader F15 house-ledger
model or guarantee solvency. Existing unsafe admin-reserve, deposit and sweep
behavior still requires the separately audited work.

Monero protocol basis: [transfer](https://docs.getmonero.org/rpc-library/wallet-rpc/#transfer)
supports construction without relay and metadata return;
[relay_tx](https://docs.getmonero.org/rpc-library/wallet-rpc/#relay_tx) relays that
saved transaction. Re-relaying identical signed metadata cannot create a different
payment. No live wallet or real funds were used in automated validation.

## Tests

Validation on 22 September 2026: **39 tests passed, zero skipped**, including 14
integration tests on disposable MySQL 8.4. The baseline and migration applied
successfully there. Wallet and daemon responses were mocked; live stagenet
end-to-end testing remains a deployment acceptance step.

From this directory run `python -B -m unittest discover -s tests -v`. Integration
tests skip by default. To run against a migrated **disposable** MySQL database,
set `CASINO_INTEGRATION_TESTS=1` plus `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`,
and a `DB_NAME` containing `test` or `qa`. Do not run these on a deployment database.
Tests seed and remove their fixtures; wallet/daemon RPC is mocked, while transaction
locking, precision, constraints and concurrent requests use actual MySQL.

Integration cases cover simultaneous bets/withdrawals, duplicate request IDs,
admin reservations, prepare durability before broadcast, prepare rejection/timeout,
lost relay responses, reuse of identical metadata, commit failure after broadcast,
and concurrent/future/already-settled round handling. HTTP tests verify that the
retired signing key cannot access the actual money routes and that pending retries
use the same request ID without exposing signed metadata.
