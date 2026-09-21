-- SPA-78 — sanitize secrets for a VIEW-ONLY public demo.
-- Runs on the SERVER after the data copy (scripts/copy-data-to-server.sh).
--
-- UPDATE-only by design: NO DELETE / DROP / TRUNCATE, so every row is preserved
-- (providers, experiments, runs, results, registry entries) and no FK cascade can
-- fire. The committee sees all results; to actually run an agent or judge they must
-- enter their own key + endpoint in Settings -> Providers & Models.
--
-- Run:
--   docker compose -f docker-compose.yml -f docker-compose.prod.yml \
--     exec -T postgres psql -U evalhive -d evalhive < scripts/sanitize-secrets.sql

BEGIN;

-- LLM provider API keys + working endpoint addresses -> placeholders (rows kept).
UPDATE providers
   SET api_key  = 'REPLACE-WITH-YOUR-OWN-KEY',
       endpoint = 'https://your-endpoint.example/v1';

-- Tool & MCP registry: clear stored secrets/tokens; keep the entries and their config.
UPDATE registry_entries
   SET secrets = '{}'::jsonb
 WHERE secrets IS NOT NULL
   AND secrets <> '{}'::jsonb;

COMMIT;

-- Verify (should show only the placeholder values):
--   SELECT name, api_key, endpoint FROM providers;
-- If any private URLs remain inside registry_entries.config, inspect and scrub the
-- specific JSON keys here against the actual copied rows:
--   SELECT id, name, config FROM registry_entries;
