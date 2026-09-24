-- People (M5): per-contact outbound permission replaces the interaction_allowed flag.
-- may_contact: never | ask | auto (architecture 7.4). The owner is 'auto' by identity, not by row.
-- Needs SQLite >= 3.35 (DROP COLUMN); both migration runners check the version before any file
-- that drops a column. One transaction: a failure part way leaves the store at 005 for a rerun.
BEGIN;
ALTER TABLE contacts ADD COLUMN may_contact TEXT NOT NULL DEFAULT 'ask'
  CHECK (may_contact IN ('never','ask','auto'));
UPDATE contacts SET may_contact = 'never' WHERE interaction_allowed = 0;
ALTER TABLE contacts ADD COLUMN cadence_minutes INTEGER;                   -- owner-set; NULL = none
ALTER TABLE contacts ADD COLUMN digest TEXT;                               -- the per-contact digest
ALTER TABLE contacts ADD COLUMN digest_sources TEXT NOT NULL DEFAULT '[]'; -- JSON array of source refs
ALTER TABLE contacts DROP COLUMN interaction_allowed;
CREATE INDEX IF NOT EXISTS idx_contacts_may_contact
  ON contacts(may_contact) WHERE deleted_at IS NULL;
COMMIT;
