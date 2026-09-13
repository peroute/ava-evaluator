-- ---------------------------------------------------------------------------
-- Runs ONCE, when the Postgres container starts with an empty data volume.
--
-- The official image executes every file in /docker-entrypoint-initdb.d/ on
-- first boot only. Editing this file later has no effect on an existing volume;
-- to re-run it, `docker compose down -v` (which deletes all data).
--
-- Tables are NOT created here -- that is Alembic's job (`python -m ava_eval
-- migrate`), so the schema has one versioned source of truth.
-- ---------------------------------------------------------------------------

-- The dev database (POSTGRES_DB) is created by the image itself.
CREATE EXTENSION IF NOT EXISTS vector;

-- A separate database for pytest. Tests empty every table before each test, so
-- they must never point at the database holding real pipeline results.
CREATE DATABASE ava_eval_test;

\connect ava_eval_test
CREATE EXTENSION IF NOT EXISTS vector;
