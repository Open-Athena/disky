-- A prefix's objects a real run left in place for want of a generation: its deletes stay recorded in the band, and with
-- any skipped the prefix stays staged (`unstageDeleted`, `deletedItems`).
ALTER TABLE deletion_bands ADD COLUMN skipped_objects INTEGER NOT NULL DEFAULT 0;
