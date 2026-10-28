-- Apply once to the supplied baseline or existing deployment, with ALL writers stopped.
-- Back up first. MySQL DDL commits implicitly: record successful application externally.
-- Existing negative balances/duplicate house labels must be investigated, not discarded.
ALTER TABLE blocks MODIFY last_two_digits INT NULL;
UPDATE blocks SET last_two_digits = NULL
 WHERE block_hash IN ('pending', '') AND resolved_at IS NULL;
ALTER TABLE wallets MODIFY virtual_balance DECIMAL(20,12) DEFAULT 0;
ALTER TABLE wallets ADD CONSTRAINT wallet_available_nonnegative CHECK (virtual_balance >= 0);
ALTER TABLE bets MODIFY payout DECIMAL(20,12) NULL;
ALTER TABLE admin_wallets ADD UNIQUE KEY unique_house_label (label);

CREATE TABLE withdrawals (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  request_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  user_id INT NOT NULL,
  kind ENUM('user','admin') NOT NULL,
  wallet_id INT NULL,
  admin_wallet_id INT NOT NULL,
  address VARCHAR(106) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  amount DECIMAL(20,12) NOT NULL,
  fee DECIMAL(20,12) NULL,
  state ENUM('reserved','prepared','submitted','failed') NOT NULL DEFAULT 'reserved',
  tx_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
  tx_metadata MEDIUMTEXT CHARACTER SET ascii COLLATE ascii_bin NULL,
  last_error VARCHAR(100) NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY withdrawal_request (user_id, kind, request_id),
  UNIQUE KEY withdrawal_tx_hash (tx_hash),
  KEY withdrawal_queue (state, id),
  CONSTRAINT withdrawal_user FOREIGN KEY (user_id) REFERENCES users(id),
  CONSTRAINT withdrawal_wallet FOREIGN KEY (wallet_id) REFERENCES wallets(id),
  CONSTRAINT withdrawal_house FOREIGN KEY (admin_wallet_id) REFERENCES admin_wallets(id),
  CONSTRAINT withdrawal_positive CHECK (amount > 0),
  CONSTRAINT withdrawal_fee CHECK (fee IS NULL OR fee >= 0),
  CONSTRAINT withdrawal_owner CHECK
    ((kind = 'user' AND wallet_id IS NOT NULL) OR (kind = 'admin' AND wallet_id IS NULL))
) ENGINE=InnoDB;
