-- Run this ONCE in phpMyAdmin (SQL tab) with the movie_booking database selected.
-- Back up your database first (Export in phpMyAdmin).

USE movie_booking;

-- Lets the admin hide a movie from customers without deleting it (its bookings are kept).
ALTER TABLE movies
  ADD COLUMN is_archived TINYINT(1) NOT NULL DEFAULT 0;
