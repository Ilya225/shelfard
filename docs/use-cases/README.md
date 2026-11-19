# Shelfard use cases

Short scenarios showing how Shelfard fits into everyday work. The same loop applies to every source: **snapshot** a baseline → **subscribe** the consumers that depend on it → **check** (or `probe run`) whenever you want to know what changed.

| Use case | Source | Scenario |
|---|---|---|
| [Catch a breaking change in a third-party API](rest-api.md) | REST API | A partner changes an endpoint your service reads |
| [Gate migrations in CI](postgres.md) | PostgreSQL | Fail a pull request when a migration breaks a consumer |
| [Watch a data lake output](files.md) | Parquet / ORC / Avro / Iceberg | Detect schema changes between nightly pipeline runs |

> **Tip.** Every schema in the registry can be exported as an ODCS data contract (`shelfard odcs export orders --out orders.yaml`) and checked later with `shelfard odcs check orders.yaml --probe orders`. You can also point an MCP client or `shelfard agent` at the same registry to ask questions like "which consumers break if we drop `status`?"

The full command reference is in the [main README](../../README.md).
