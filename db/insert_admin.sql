-- Manually seeds the admin account on a database where `python seed.py`
-- either hasn't been run yet, or isn't reachable from this machine (e.g.
-- a hosted MySQL you're managing through a web SQL console instead).
--
-- Safe to re-run: if a row with this username already exists, this just
-- refreshes its password hash instead of failing on the UNIQUE constraint.
--
-- Run this against the SAME database your Render service's DB_NAME env
-- var points at, or the login will still 401 — the app queries whatever
-- database DB_HOST/DB_NAME/DB_USER/DB_PASSWORD resolve to, not this file.

INSERT INTO `admins` (`id`, `username`, `password_hash`, `created_at`)
VALUES (1, 'admin',
        'pbkdf2$200000$16dd4a251f42d761d8cceec897551d95$4ff9c504a97fb88a0c650d5eeea2812567d179f417378bc5952b5cbd796bffff',
        '2026-09-05 16:08:12')
ON DUPLICATE KEY UPDATE
  password_hash = VALUES(password_hash),
  created_at    = VALUES(created_at);
