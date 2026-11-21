-- Run this ONCE in phpMyAdmin (SQL tab) or MySQL, with the movie_booking database selected.
-- Back up your database first (Export in phpMyAdmin).

USE movie_booking;

-- 1) Lets the admin disable an account without deleting it
ALTER TABLE users
  ADD COLUMN is_active TINYINT(1) NOT NULL DEFAULT 1;

-- 2) Allows the new booking statuses: 'cancelled' (booking) and 'refunded' (payment).
--    Existing values (pending / confirmed / paid / rejected) are kept as they are.
ALTER TABLE bookings
  MODIFY booking_status VARCHAR(20) NOT NULL DEFAULT 'pending',
  MODIFY payment_status VARCHAR(20) NOT NULL DEFAULT 'pending';

-- 3) Audit log: who did what, and when
CREATE TABLE IF NOT EXISTS admin_audit_log (
  id          INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  admin_id    INT NULL,
  admin_email VARCHAR(100) NULL,
  action      VARCHAR(50)  NOT NULL,
  target_type VARCHAR(30)  NULL,
  target_id   INT NULL,
  details     VARCHAR(500) NULL,
  ip_address  VARCHAR(45)  NULL,
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_audit_created (created_at),
  INDEX idx_audit_admin (admin_id)
);
