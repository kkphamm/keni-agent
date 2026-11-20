BEGIN;

CREATE TABLE IF NOT EXISTS public.action_runs (
    id uuid PRIMARY KEY,
    room_name text NOT NULL,
    action_type text NOT NULL CHECK (action_type IN ('spotify', 'vscode')),
    state text NOT NULL CHECK (state IN ('running', 'succeeded', 'failed')),
    started_at timestamptz NOT NULL,
    finished_at timestamptz,
    summary varchar(512),
    CHECK ((state = 'running') = (finished_at IS NULL))
);

CREATE INDEX IF NOT EXISTS action_runs_newest_idx ON public.action_runs (started_at DESC, id DESC);

COMMIT;
