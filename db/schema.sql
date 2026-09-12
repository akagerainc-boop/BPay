-- BPay backend schema (MySQL / MariaDB, XAMPP-compatible).
--
-- The heart of this schema is `ussd_templates`: the admin defines, per
-- network and per transaction type, the exact USSD string to dial and
-- where the user's own input is substituted into it. The Flutter app
-- holds no hardcoded carrier codes — it renders whatever is defined here.

-- CREATE DATABASE IF NOT EXISTS bpay
--   CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
-- USE bpay;

-- ---------------------------------------------------------------- admins
CREATE TABLE IF NOT EXISTS admins (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  username      VARCHAR(64)  NOT NULL UNIQUE,
  password_hash VARCHAR(255) NOT NULL,
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- -------------------------------------------------------- ussd templates
-- `template` uses {placeholders} the app substitutes at dial time:
--   {recipient} — phone number the user typed / picked
--   {amount}    — amount in RWF, digits only
--   {account}   — account / meter / merchant reference for a service
--   {code}      — merchant or till code
-- Anything not listed is left untouched, so a template can be a plain
-- menu code like *182# with no placeholders at all.
CREATE TABLE IF NOT EXISTS ussd_templates (
  id                INT AUTO_INCREMENT PRIMARY KEY,

  -- Which SIM the user is paying FROM.
  sim_network       ENUM('mtn','airtel')                      NOT NULL,

  -- What the user is doing.
  transaction_type  ENUM(
                      'phone_transfer',
                      'merchant_payment',
                      'bill_payment',
                      'airtime',
                      'mokash_send',
                      'mokash_withdraw',
                      'check_balance'
                    )                                          NOT NULL,

  -- For phone transfers, which network the RECIPIENT is on. 'any' means
  -- this template covers every recipient network.
  recipient_network ENUM('mtn','airtel','any') NOT NULL DEFAULT 'any',

  template          VARCHAR(255) NOT NULL,

  -- TRUE when the template carries recipient+amount inline, so the user
  -- only has to authenticate. FALSE for menu entry points where they must
  -- still navigate the carrier's own menu.
  completes_payment TINYINT(1)   NOT NULL DEFAULT 1,

  -- Shown to the user when completes_payment = 0.
  guidance          TEXT         NULL,

  active            TINYINT(1)   NOT NULL DEFAULT 1,
  updated_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,

  UNIQUE KEY uniq_route (sim_network, transaction_type, recipient_network)
) ENGINE=InnoDB;

-- -------------------------------------------------------------- services
-- Buyable services (electricity, water, TV…) shown in the app's service
-- grid. `icon` is a Flutter icon name the app maps to a real IconData.
CREATE TABLE IF NOT EXISTS services (
  id           INT AUTO_INCREMENT PRIMARY KEY,
  name         VARCHAR(120) NOT NULL,
  description  VARCHAR(255) NULL,
  category     VARCHAR(64)  NOT NULL DEFAULT 'other',

  -- Flutter icon name, e.g. 'bolt_rounded'. Unknown names fall back to a
  -- default icon in the app rather than crashing.
  icon         VARCHAR(64)  NOT NULL DEFAULT 'receipt_long_rounded',

  -- USSD to dial for this service, with the same {placeholders}. MTN and
  -- Airtel almost never share a code for the same service, so each network
  -- gets its own column; a NULL one means the admin hasn't set that
  -- network up yet and the app won't offer the service on it.
  ussd_template_mtn    VARCHAR(255) NULL,
  ussd_template_airtel VARCHAR(255) NULL,

  -- Label for the account field the user fills in ("Meter number"…).
  account_label VARCHAR(64) NOT NULL DEFAULT 'Account number',

  -- Higher shows first; used for the "frequently used" ordering.
  sort_order   INT          NOT NULL DEFAULT 0,
  active       TINYINT(1)   NOT NULL DEFAULT 1,
  updated_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                            ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- ---------------------------------------------------------- transactions
-- Client-reported records. Per the app's own rule, a client claim is
-- never treated as financial truth: `verification` records how strongly
-- the result is evidenced, and stays below 'provider_verified' until a
-- provider-side mechanism confirms it.
CREATE TABLE IF NOT EXISTS transactions (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  bpay_id       VARCHAR(64)  NOT NULL UNIQUE,
  device_id     VARCHAR(128) NULL,
  user_phone    VARCHAR(20)  NULL,
  sim_network   VARCHAR(16)  NULL,
  type          VARCHAR(32)  NOT NULL,
  destination   VARCHAR(120) NULL,
  destination_name VARCHAR(120) NULL,
  amount        INT          NOT NULL DEFAULT 0,
  status        VARCHAR(32)  NOT NULL,
  verification  VARCHAR(32)  NOT NULL DEFAULT 'none',
  carrier_ref   VARCHAR(64)  NULL,
  message       TEXT         NULL,
  -- Set only when this transaction was paid through someone else's
  -- payment link (see `payment_links`) — how the link owner's usage count
  -- (and eventually their per-use fee) gets attributed.
  payment_link_code VARCHAR(16) NULL,
  created_at    DATETIME     NOT NULL,
  received_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,

  INDEX idx_created (created_at),
  INDEX idx_status (status),
  INDEX idx_link_code (payment_link_code)
) ENGINE=InnoDB;

-- --------------------------------------------------------------- devices
CREATE TABLE IF NOT EXISTS devices (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  device_id   VARCHAR(128) NOT NULL UNIQUE,
  phone       VARCHAR(20)  NULL,
  sim_network VARCHAR(16)  NULL,
  app_version VARCHAR(32)  NULL,
  last_seen   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                           ON UPDATE CURRENT_TIMESTAMP,
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- -------------------------------------------------------------- fee_rules
-- A service fee BPay discloses to the user once they've made enough
-- transactions on a network within a rolling calendar window. One row per
-- network — the admin edits it in place rather than creating new ones.
-- This is a *disclosure* the app shows before dialling, not a charge BPay
-- collects itself: BPay has no payment rail of its own, only the USSD code
-- already defined in `ussd_templates`/`services`.
CREATE TABLE IF NOT EXISTS fee_rules (
  id             INT AUTO_INCREMENT PRIMARY KEY,
  network        ENUM('mtn','airtel') NOT NULL UNIQUE,
  fee_amount     INT NOT NULL DEFAULT 0,
  trigger_count  INT NOT NULL DEFAULT 5,
  trigger_window ENUM('day','week','month','year') NOT NULL DEFAULT 'month',
  active         TINYINT(1) NOT NULL DEFAULT 0,
  updated_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                          ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- -------------------------------------------------------- device_tokens
-- One row per installed app instance that has a push token, keyed by a
-- device id it generates for itself. Registered the moment Firebase hands
-- the app a token, and again whenever that token is refreshed. Separate
-- from `devices` above, which nothing currently writes to.
CREATE TABLE IF NOT EXISTS device_tokens (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  device_id   VARCHAR(128) NOT NULL UNIQUE,
  fcm_token   VARCHAR(255) NOT NULL,
  platform    VARCHAR(16)  NOT NULL DEFAULT 'android',
  updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                       ON UPDATE CURRENT_TIMESTAMP,
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- --------------------------------------------------------- announcements
-- What the admin broadcasts via Firebase push: a title, a message, an
-- optional photo and logo (served from /uploads). `sent_count` is how many
-- device tokens the push was actually handed to at the time — not proof
-- any device displayed it.
CREATE TABLE IF NOT EXISTS announcements (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  title       VARCHAR(160) NOT NULL,
  message     TEXT         NOT NULL,
  photo_url   VARCHAR(500) NULL,
  logo_url    VARCHAR(500) NULL,
  sent_count  INT          NOT NULL DEFAULT 0,
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- --------------------------------------------------------- provider_keys
-- Real credentials for the MTN MoMo / Airtel Money Collections APIs — a
-- "Request to Pay" that charges a user's own mobile-money wallet and pays
-- it into the company's account on file with that provider. Server-side
-- only, never sent to the Flutter app. Column meaning differs by network:
--   MTN:    subscription_key (Ocp-Apim-Subscription-Key), api_user
--           (API user UUID), api_key (that user's API key/secret)
--   Airtel: client_id, client_secret
-- Both also use environment/target_environment/base_url, since sandbox
-- vs. production differs by what each provider actually provisions for
-- the merchant account behind these credentials.
CREATE TABLE IF NOT EXISTS provider_keys (
  id                 INT AUTO_INCREMENT PRIMARY KEY,
  network            ENUM('mtn','airtel') NOT NULL UNIQUE,
  environment        ENUM('sandbox','production') NOT NULL DEFAULT 'sandbox',
  base_url           VARCHAR(255) NULL,
  target_environment VARCHAR(64)  NULL,
  subscription_key   VARCHAR(255) NULL,
  api_user           VARCHAR(255) NULL,
  api_key            VARCHAR(512) NULL,
  client_id          VARCHAR(255) NULL,
  client_secret      VARCHAR(512) NULL,
  updated_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                              ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- ---------------------------------------------------------- payment_links
-- A shareable BPay link a user generates for others to pay them: tap it,
-- and BPay opens with the recipient/merchant and amount already filled in
-- and the dial already started (Android App Links / iOS Universal Links —
-- see `payment_link_settings` for the domain and cert config that makes
-- the OS hand the link straight to the app instead of a browser).
CREATE TABLE IF NOT EXISTS payment_links (
  id                INT AUTO_INCREMENT PRIMARY KEY,
  code              VARCHAR(16)  NOT NULL UNIQUE,
  device_id         VARCHAR(128) NOT NULL,
  owner_phone       VARCHAR(20)  NULL,
  -- The owner's OWN mobile-money network — who the usage fee below is
  -- actually charged against. Distinct from `network`, which classifies
  -- the destination for routing the payer's own dial.
  owner_network     ENUM('mtn','airtel') NULL,
  destination       VARCHAR(120) NOT NULL,
  destination_type  ENUM('phone','merchant') NOT NULL,
  network           ENUM('mtn','airtel','unknown') NOT NULL DEFAULT 'unknown',
  amount            INT          NOT NULL,
  status            ENUM('active','paused','deleted') NOT NULL DEFAULT 'active',

  -- How many completed transactions have come in through this link, and
  -- how many fee thresholds worth of those have already been billed —
  -- the difference is what `fee_threshold` in payment_link_settings uses
  -- to know a new charge is due, without ever double-billing the same use.
  use_count         INT          NOT NULL DEFAULT 0,
  fee_charges_done  INT          NOT NULL DEFAULT 0,

  created_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,

  INDEX idx_device (device_id),
  INDEX idx_status (status)
) ENGINE=InnoDB;

-- ------------------------------------------------- payment_link_settings
-- Single global row the header admin controls: which domain the links
-- point at, where to send someone who taps one without BPay installed,
-- the signing certificate fingerprint the domain's assetlinks.json must
-- publish, and the usage-based fee a link owner is charged after every
-- `fee_threshold` payments received through their link.
CREATE TABLE IF NOT EXISTS payment_link_settings (
  id                 INT PRIMARY KEY DEFAULT 1,
  app_domain         VARCHAR(255) NULL,
  play_store_url     VARCHAR(500) NULL,
  sha256_fingerprint VARCHAR(255) NULL,
  fee_amount         INT NOT NULL DEFAULT 0,
  fee_threshold      INT NOT NULL DEFAULT 5,
  active             TINYINT(1) NOT NULL DEFAULT 0,
  updated_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                              ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- ------------------------------------------------------- fee_collections
-- One row per Request-to-Pay actually attempted against a real provider —
-- this is the company's real ledger of fee money requested from users,
-- separate from `transactions` (which is the user's own USSD payments).
CREATE TABLE IF NOT EXISTS fee_collections (
  id                  INT AUTO_INCREMENT PRIMARY KEY,
  network             ENUM('mtn','airtel') NOT NULL,
  phone               VARCHAR(20)  NOT NULL,
  amount              INT          NOT NULL,
  external_id         VARCHAR(64)  NOT NULL UNIQUE,
  provider_reference  VARCHAR(128) NULL,
  status              ENUM('pending','successful','failed') NOT NULL DEFAULT 'pending',
  reason              VARCHAR(255) NULL,
  device_id           VARCHAR(128) NULL,
  payment_link_code   VARCHAR(16)  NULL,
  created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                               ON UPDATE CURRENT_TIMESTAMP,

  INDEX idx_status (status),
  INDEX idx_fee_link_code (payment_link_code)
) ENGINE=InnoDB;
