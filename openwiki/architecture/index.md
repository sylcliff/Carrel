# Files

- [Configuration and secrets](configuration.md) - How Carrel loads configuration from data/config.yaml and .env, every CarrelYAML block and EnvSettings key, and how PATCH /schedule writes config back atomically.
- [Data model and schemas](data-model.md) - SQLModel tables, enums, JSONB shapes, relationships, indexes, and the paper state machine that Carrel persists in Postgres (and SQLite under tests).
- [Carrel architecture overview](overview.md) - High-level system map of Carrel — runtime components, process model, request lifecycle, and how the backend pipelines, database, filesystem, and external services fit together.
- [Scheduler and background jobs](scheduler-and-jobs.md) - APScheduler-based cron registry, the Job table as the progress surface, and how API BackgroundTasks and scheduled runs share one execution model.
