-- Tighten message_logs status/timestamp consistency. Migration 009 only required a
-- send time for non-failed events; this makes each status carry its matching event
-- timestamp, so a logged status cannot disagree with the times recorded for it.
--
--   sent      -> sent_at present.
--   delivered -> sent_at AND delivered_at present (delivery implies it was sent first).
--   failed    -> failed_at present; sent_at stays optional (a send can fail before any
--               send time exists, as 009 already allowed).
--
-- 009's anonymous CHECK (named message_logs_check by PostgreSQL) is replaced by a named
-- constraint. Allowed status values are unchanged (message_logs_status_check in 009).
ALTER TABLE message_logs DROP CONSTRAINT message_logs_check;

ALTER TABLE message_logs ADD CONSTRAINT message_logs_status_timestamps CHECK (
    (status = 'sent' AND sent_at IS NOT NULL)
    OR (status = 'delivered' AND sent_at IS NOT NULL AND delivered_at IS NOT NULL)
    OR (status = 'failed' AND failed_at IS NOT NULL)
);
