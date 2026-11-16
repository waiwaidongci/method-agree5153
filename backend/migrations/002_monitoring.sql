-- Monitoring capability: frozen baseline, daily batches, immutable revisions,
-- versioned replay evaluations. Schema is created from SQLAlchemy metadata in
-- the demo environment; this file records the v2 natural keys.
-- Natural keys:
--   monitoring_profiles.name (unique), monitoring_profiles.baseline_run_id (unique)
--   monitoring_batches(monitor_id, batch_key)
--   monitoring_batch_revisions(batch_id, revision)
--   monitoring_evaluations(monitor_id, version)
SELECT 'method-agree schema v2';
