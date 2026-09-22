-- Baseline schema extracted from the supplied dump; contains no rows or DROP statements.
SET FOREIGN_KEY_CHECKS=0;

CREATE TABLE `admin_transactions` (
  `id` int NOT NULL AUTO_INCREMENT,
  `admin_wallet_id` int NOT NULL,
  `related_transaction_id` int DEFAULT NULL,
  `tx_hash` varchar(64) COLLATE utf8mb4_general_ci NOT NULL,
  `amount` decimal(20,12) DEFAULT '0.000000000000',
  `tx_type` enum('deposit','withdrawal','payout','profit') COLLATE utf8mb4_general_ci NOT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `tx_hash` (`tx_hash`),
  KEY `admin_wallet_id` (`admin_wallet_id`),
  KEY `related_transaction_id` (`related_transaction_id`),
  CONSTRAINT `admin_transactions_ibfk_1` FOREIGN KEY (`admin_wallet_id`) REFERENCES `admin_wallets` (`id`) ON DELETE CASCADE,
  CONSTRAINT `admin_transactions_ibfk_2` FOREIGN KEY (`related_transaction_id`) REFERENCES `transactions` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE `admin_wallets` (
  `id` int NOT NULL AUTO_INCREMENT,
  `wallet_address` varchar(100) COLLATE utf8mb4_general_ci NOT NULL,
  `balance` decimal(20,12) DEFAULT '0.000000000000',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `label` varchar(100) COLLATE utf8mb4_general_ci DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `wallet_address` (`wallet_address`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE `bets` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `block_id` int NOT NULL,
  `bet_amount` decimal(20,12) DEFAULT '0.000000000000',
  `chosen_number` int DEFAULT NULL,
  `bet_status` enum('pending','won','lost') COLLATE utf8mb4_general_ci NOT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `game_type` enum('digits','odd_even','high_low') COLLATE utf8mb4_general_ci NOT NULL,
  `bet_value` varchar(10) COLLATE utf8mb4_general_ci DEFAULT NULL,
  `payout` decimal(18,12) DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `user_id` (`user_id`),
  KEY `block_id` (`block_id`),
  CONSTRAINT `bets_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE,
  CONSTRAINT `bets_ibfk_2` FOREIGN KEY (`block_id`) REFERENCES `blocks` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=19 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE `blocks` (
  `id` int NOT NULL AUTO_INCREMENT,
  `block_height` int NOT NULL,
  `block_hash` varchar(64) COLLATE utf8mb4_general_ci NOT NULL,
  `last_two_digits` int NOT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `resolved_at` timestamp NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `block_height` (`block_height`),
  UNIQUE KEY `unique_block_height` (`block_height`),
  KEY `blocks_block_height_IDX` (`block_height`) USING BTREE
) ENGINE=InnoDB AUTO_INCREMENT=175 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE `transactions` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int DEFAULT NULL,
  `admin_wallet_id` int DEFAULT NULL,
  `tx_hash` varchar(64) COLLATE utf8mb4_general_ci NOT NULL,
  `amount` decimal(20,12) DEFAULT '0.000000000000',
  `tx_type` enum('deposit','withdrawal','win_payout','house_transfer','sweep') CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `tx_hash` (`tx_hash`),
  KEY `admin_wallet_id` (`admin_wallet_id`),
  KEY `fk_transactions_user` (`user_id`),
  CONSTRAINT `fk_transactions_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE,
  CONSTRAINT `transactions_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE,
  CONSTRAINT `transactions_ibfk_2` FOREIGN KEY (`admin_wallet_id`) REFERENCES `admin_wallets` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=61 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE `users` (
  `id` int NOT NULL AUTO_INCREMENT,
  `username` varchar(50) COLLATE utf8mb4_general_ci NOT NULL,
  `password_hash` varchar(255) COLLATE utf8mb4_general_ci NOT NULL,
  `wallet_id` int DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `is_admin` tinyint(1) DEFAULT '0',
  `admin_wallet_id` int DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `username` (`username`),
  UNIQUE KEY `wallet_address` (`wallet_id`)
) ENGINE=InnoDB AUTO_INCREMENT=36 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE `wallets` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `address` varchar(100) COLLATE utf8mb4_general_ci NOT NULL,
  `balance` decimal(20,12) DEFAULT '0.000000000000',
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `virtual_balance` decimal(16,12) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `address` (`address`),
  KEY `user_id` (`user_id`),
  CONSTRAINT `wallets_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=7 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

SET FOREIGN_KEY_CHECKS=1;
