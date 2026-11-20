BEGIN;

ALTER TABLE public.action_runs
    DROP CONSTRAINT IF EXISTS action_runs_action_type_check,
    ADD CONSTRAINT action_runs_action_type_check CHECK (action_type IN ('spotify', 'vscode', 'open_app'));

COMMIT;
