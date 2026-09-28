-- Deposit observations and scan-run state. Apply with application writers stopped.
-- Existing transaction rows are deliberately not backfilled as credits: the scanner
-- must reconcile legacy deposits and place conflicts in review.

ALTER TABLE wallets
  ADD COLUMN deposit_hold BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE deposits (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  tx_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  account_index INT UNSIGNED NOT NULL,
  subaddress_index INT UNSIGNED NOT NULL,
  wallet_id INT NOT NULL,
  user_id INT NOT NULL,
  amount DECIMAL(20,12) NOT NULL,
  block_height BIGINT UNSIGNED NOT NULL,
  block_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
  confirmations INT UNSIGNED NOT NULL DEFAULT 0,
  state ENUM('observed','credited','orphaned','review') NOT NULL DEFAULT 'observed',
  transaction_id INT NULL,
  credited_at TIMESTAMP NULL DEFAULT NULL,
  last_seen_scan CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY deposit_output (tx_hash, account_index, subaddress_index),
  UNIQUE KEY deposit_transaction (transaction_id),
  KEY deposit_state (state),
  CONSTRAINT deposit_wallet FOREIGN KEY (wallet_id) REFERENCES wallets(id),
  CONSTRAINT deposit_user FOREIGN KEY (user_id) REFERENCES users(id),
  CONSTRAINT deposit_transaction_fk FOREIGN KEY (transaction_id) REFERENCES transactions(id),
  CONSTRAINT deposit_amount_positive CHECK (amount > 0)
) ENGINE=InnoDB;

CREATE TABLE deposit_scan_runs (
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
  state ENUM('running','complete','failed') NOT NULL,
  tip_height BIGINT UNSIGNED NOT NULL,
  error VARCHAR(100) NULL,
  started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TIMESTAMP NULL DEFAULT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;
