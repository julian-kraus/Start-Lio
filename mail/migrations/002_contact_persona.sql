-- A test contact may play a supplier persona in a scenario. Set by a human with `mail set-persona`.
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS persona_id text;
