-- Run this ONCE in phpMyAdmin (SQL tab) with the movie_booking database selected.
-- It is safe to run even if your bookings table already has a created_at column.
-- It is needed for the "Pending for more than 24 hours" list on the dashboard.

USE movie_booking;

SET @has_col := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'bookings' AND COLUMN_NAME = 'created_at'
);
SET @ddl := IF(@has_col = 0,
  'ALTER TABLE bookings ADD COLUMN created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP',
  'SELECT ''created_at already exists'' AS info');
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
