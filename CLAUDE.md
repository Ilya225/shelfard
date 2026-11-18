# Shelfard — Schema Drift Detection

## Project Purpose

Shelfard is a schema drift detection tool for REST APIs, databases and data lake files (Parquet, ORC, Avro, Iceberg). It captures versioned snapshots of data source shapes, classifies every change as SAFE, WARNING, or BREAKING, tracks which downstream consumers are impacted, and stores check configurations for repeatable on-demand or CI-driven runs.

The system is built in layers: **Acquisition** (vendor readers normalize raw schemas), **Registry** (versioned, pluggable storage), **Checkers** (stored run configs with env-var-resolved auth), and **MCP/Agent** (a standalone MCP server and a conversational assistant that expose all tools to LLM clients). Deterministic drift classification is kept strictly separate from LLM reasoning — the agent handles interpretation and orchestration, not severity decisions.

---

## Architecture

Layered design — each layer is deterministic and agent-ready:

- **Layer 1 — Acquisition** (`shelfard/tools/`): Vendor-specific tools co-locate the reader and checker for each data source. Each vendor package (e.g. `tools/rest/`, `tools/postgres/`) contains a `reader.py` (implements `SchemaReader` ABC) and a `checker.py` (implements `Checker` ABC). Shared SQL utilities live in `tools/sql/base.py`. Document parsers live in `shelfard/parsers/`.
- **Layer 1 — File formats** (`shelfard/tools/files/`): `FileReader(path, schema_name, file_format="auto")` reads metadata only. `arrow.py` handles Parquet/ORC via `pyarrow` (optional `[files]` extra, lazy import; directories are read as Hive-partitioned datasets). `avro.py` parses the container header / `.avsc` with the stdlib. Detection: extension → magic bytes. Iceberg tables/metadata files are **refused** (their `data/` holds Parquet) with a hint to use the iceberg reader. `FileChecker` resolves `{{vars}}` then `$VARS` in the path. CLI: `shelfard file snapshot|check|list`, `checker register --type file`.
- **Layer 1 — Iceberg** (`shelfard/tools/iceberg/`): table format, kept separate from `files/` so Iceberg-specific features (snapshots, schema history, catalogs) have room to grow. `metadata.py` (stdlib) resolves and parses table metadata (`version-hint.text`, else highest version; gzip; format v1–v3; partition spec → `partition_keys` as `transform(col)`, sort order → `clustering_keys`). `IcebergReader(path, schema_name)` accepts a table dir, its `metadata/` dir or a specific `*.metadata.json`; `list_tables()` walks a Hadoop-catalog warehouse and returns dotted identifiers (`db.orders`). `IcebergChecker` / `IcebergCheckerConfig` (`checker_type = "iceberg"`). CLI: `shelfard iceberg snapshot|check|list`, `checker register --type iceberg`.
- Files and Iceberg: local paths only (remote URIs rejected); no MCP write tools (deliberate: LLM-chosen local paths). `scripts/spark_fixtures.py` builds real Spark-written sample tables (needs pyspark + Java 17/21; not a dependency).
- **Layer 1 — Registry** (`shelfard/registry/`): Pluggable registry with a `SchemaRegistry` ABC and a `LocalFileRegistry` implementation. Tracks both source schema versions and consumer subscriptions (full or projected). Stubs exist for S3, GCS, and SQL backends.
- **Layer 2 — Comparison** (`shelfard/schema_comparison.py`): Pure deterministic diffing, produces self-documenting `SchemaDiff`
- **Layer 2 — Checkers** (`shelfard/tools/*/checker.py`): Stored run configurations for drift checks. Four checker types are supported: `RestCheckerConfig` (URL + headers with `$VAR` placeholders and/or `{{var}}` template vars) and `PostgresCheckerConfig` (DSN with `$VAR` placeholders and/or `{{var}}` template vars + table or custom SQL query). Resolution order at `Checker.run()` time: `{{template_vars}}` from registry first, then `$ENV_VARS` from `os.environ`. `FileCheckerConfig` (`path` with `{{var}}`/`$VAR`, `file_format`; `checker_type = "file"`) and `IcebergCheckerConfig` (`path`; `checker_type = "iceberg"`) complete the set. `LocalFileRegistry.run_checker()` dispatches to the correct `Checker` subclass via the stored `checker_type` field.
- **Layer 2 — Template Variables** (`shelfard/registry/`): Persistent key-value store for non-sensitive config values (hosts, base URLs, ports). Stored in `schemas/vars.json`. Referenced as `{{var_name}}` in checker configs and snapshot CLI commands. Managed via `shelfard var set/get/list/unset` or the MCP tools `set_template_var`, `get_template_var`, `list_template_vars`, `delete_template_var`. Distinct from `$ENV_VAR` (secrets, not stored).
- **Layer 3 — MCP Server** (`shelfard/mcp_server.py`): Standalone FastMCP server built by `build_server(allow_writes)`, stdio transport. Read tools (always): `get_schema` (includes checker info if registered), `get_schemas`, `get_subscriptions`, `get_subscription`, `get_checker_config`, `live_check_schema`, `get_schema_odcs`, `get_template_var`, `list_template_vars`. Write tools (only with `--allow-writes` / `SHELFARD_MCP_ALLOW_WRITES=1`): `register_checker`, `register_postgres_checker` (table mode only), `set_template_var`, `delete_template_var`, `accept_safe_schema_update`. Any MCP client (Claude Desktop, Cursor, etc.) can connect directly. See **MCP security model** below.
- **Layer 3 — Agent** (`shelfard/agent.py`): Interactive LangChain 1.x assistant; spawns the MCP server as a subprocess via `MultiServerMCPClient` and gets its tools from there. Supports Claude, OpenAI, and local Ollama models (`langchain-ollama`, optional extra); model resolved via `--model` flag (`anthropic`, `openai`, `ollama`, `ollama:<model>`, or a model ID) , then `SHELFARD_DEFAULT_MODEL`, then env-var auto-detection (`ANTHROPIC_API_KEY` → `OPENAI_API_KEY` → `OLLAMA_MODEL`). Defaults: `claude-sonnet-5-5`, `gpt-5`, `qwen2.5:7b`. `build_agent()` adds `SummarizationMiddleware` (bounded memory; `/reset` starts a new thread) and, with `--allow-writes`, `HumanInTheLoopMiddleware` so every write tool call needs interactive y/N approval (`run_turn()` / `review_tool_calls()` handle the interrupts). The subprocess env receives `SHELFARD_*` vars, names in `SHELFARD_MCP_ENV_ALLOWLIST`, and env names declared by CLI-registered checkers; LLM API keys (`_NEVER_FORWARD`) are never forwarded.
- **ODCS adapter** (`shelfard/odcs.py`): converts `TableSchema` ⇄ ODCS v3.1.0 documents (`to_odcs`, `from_odcs` → `(tables, ignored_fields)`). Lossless for Shelfard-generated docs (doc-level custom property `shelfard.generator`): native ODCS fields where possible, the rest in `customProperties` named `shelfard.*` (`colType` only when `logicalType` is ambiguous, `precision`/`scale`, `defaultValue`, leftover `constraints`, object-level `source`/`capturedAt`/`partitionKeys`/`clusteringKeys`). Foreign docs: `physicalType` is not mapped (free-form), constraints only where stated, DECIMAL precision parsed from `physicalType`. `logicalTypeOptions` must only hold keys the JSON Schema allows per `logicalType` (`format` = bit width for integer/number, date pattern for temporal, string format for string). `export_odcs` derives semver from the history (`semver_history`: BREAKING → major, WARNING → minor, SAFE/unchanged → patch); `import_odcs` registers each `schema[]` object; `update_contract` merges an all-SAFE suggested schema into `schema[].properties` only, keeping all other sections, unknown fields and property order (new properties inserted after their predecessor in the source). CLI: `shelfard odcs export|import|check`; MCP: read-only `get_schema_odcs` (no import tool by design). YAML/validation via optional extra `[odcs]` (PyYAML, jsonschema); official schema vendored in `tests/fixtures/`.
- **Future layers** (planned): Autonomous remediation suggestions, background drift monitoring, consumer-aware alerting


Importing: `from shelfard import ColumnSchema, get_sqlite_schema, compare_schemas, get_all_schemas, LocalFileRegistry, subscribe_consumer, ...`

### Adding a new registry backend
1. Create `shelfard/registry/<backend>.py` with a class extending `SchemaRegistry`
2. Implement all 16 abstract methods (source schemas, consumer subscriptions, impact analysis, checkers, template vars); `resolve_template` is a concrete base-class method and does not need to be overridden
3. Re-export from `shelfard/registry/__init__.py` and `shelfard/__init__.py`

### Adding a new vendor tool
1. Create `shelfard/tools/<vendor>/` package with `reader.py` and optionally `checker.py`
2. In `reader.py`: define `_TYPE_MAP`, `_normalize_type()`, and a class implementing `SchemaReader`. `get_schema(self)` takes **no arguments** — the target is stored in `__init__`.
3. For SQL databases: reuse `shelfard/tools/sql/base.py` helpers (`introspect_table_via_information_schema` for table mode, `sample_query` + `build_columns_from_query_result` for query mode). See `tools/postgres/reader.py` for the reference pattern.
4. Add module-level wrapper functions (`get_<vendor>_schema`, `list_<vendor>_tables`) in `reader.py`
5. Create `__init__.py` re-exporting from `reader.py` (and `checker.py` if present)
6. Re-export from `shelfard/tools/__init__.py` and `shelfard/__init__.py`

### Adding a new parser
1. Create `shelfard/parsers/<format>_reader.py` (does NOT implement `SchemaReader` — parsers are document-based, not live sources)
2. Re-export from `shelfard/parsers/__init__.py` and `shelfard/__init__.py`

### type_normalizer.py responsibilities
Contains only vendor-agnostic logic — nothing in this file knows about raw SQL type strings:
- `TYPE_WIDENING_RULES` — which `ColumnType` → `ColumnType` transitions are safe
- `is_safe_widening(from_type, to_type)` — used by `schema_comparison.py`
- `extract_length(raw_type)` — parses `varchar(255)` → `255`, used by `tools/sqlite/`

Each vendor's raw-type-to-`ColumnType` mapping lives exclusively in its own reader file.

---

## Tech Stack

- **Language**: Python 3.12 (conda env: `shelfard`)
- **Supported sources**: SQLite, REST API endpoints, PostgreSQL (table/view introspection + custom SQL queries), Parquet, ORC, Avro, Iceberg (local files/tables); Snowflake, BigQuery (type maps only, readers pending)

## Core Data Models

- **`ColumnType`** (enum): 14 canonical types — `INTEGER`, `BIGINT`, `FLOAT`, `DECIMAL`, `VARCHAR`, `TEXT`, `BOOLEAN`, `DATE`, `TIMESTAMP`, `TIME`, `JSON`, `ARRAY`, `STRUCT`, `UNKNOWN`
- **`ColumnSchema`**: Column metadata — type, nullability, length, precision, default, description, and optionally `fields: list[ColumnSchema]` for `STRUCT` columns (recursive) and `items: ColumnSchema` for `ARRAY` element types. `physical_type` is the raw vendor type (`None` = not recorded); `constraints` is a `ColumnConstraints` or `None`.
- **`ColumnConstraints`**: mirrors ODCS — `primary_key`, `primary_key_position`, `unique`, `format`, `pattern`, `minimum`/`maximum`/`exclusive_*`, `multiple_of`, `min_length`, `min_items`/`max_items`/`unique_items`. Every attribute is tri-state: `None` = unknown (not observable by the reader / not stated by a contract) and never compared; readers set what they can observe explicitly (e.g. Postgres table mode and SQLite set `primary_key`/`unique` True/False on every column; Iceberg sets `primary_key` from `identifier-field-ids`; uuid columns get `format: "uuid"`). `from_dict` returns `None` when nothing is known.
- **`TableSchema`**: Full table — columns, partition keys, clustering keys, source tracking. The root-level schema is conceptually the top-level STRUCT.
- **`ConsumerSubscription`**: A named consumer's dependency on a source schema. `subscribed_columns=None` means a full snapshot; a list means a projection. Stores the `TableSchema` snapshot at subscription time plus the source schema version it was derived from.
- **`RestCheckerConfig`**: Stored configuration for a REST drift check — `schema_name`, `url`, `headers` (list of dicts, values may contain `$VAR` placeholders), `env` (list of required env var names, never values). `checker_type = "rest"`. Serialized as a single (non-versioned) JSON file at `schemas/checkers/<schema_name>.json`.
- **`PostgresCheckerConfig`**: Stored configuration for a PostgreSQL drift check — `schema_name`, `dsn` (may contain `$VAR` placeholders), `env`, `table` (table mode) or `query` (query mode), `db_schema` (default `"public"`), `sample_size` (default 100). `checker_type = "postgres"`. Same non-versioned file storage as `RestCheckerConfig`.
- **`SchemaDiff`**: Comparison result — list of changes, severity per change, human-readable summaries. Nested STRUCT field changes use dot-notation column names (e.g. `"address.zip"`).

### STRUCT type
`ColumnType.STRUCT` represents a nested object with its own typed sub-fields. It is the recursive building block:
- `ColumnSchema.fields` holds the sub-schema (itself a `list[ColumnSchema]`, each of which can also be STRUCT)
- `json_file_reader` and `RestEndpointReader` infer STRUCT automatically for any nested dict in a JSON payload
- `schema_comparison` recurses into STRUCT fields; changes are reported with qualified names
- `ColumnSchema.from_dict(col)` reconstructs nested schemas from serialized dicts (used by registry, parsers, and comparison)
- `ColumnType.JSON` is kept for opaque/untyped JSON blobs (e.g. vendor columns declared as JSON without a known structure)

---

## Change Severity Rules

| Severity | Examples |
|---|---|
| **SAFE** | Nullable column added, nullability relaxed, type widened (int→bigint, varchar(50)→varchar(200), varchar(n)→unbounded), UNKNOWN→concrete type (`type_resolved`), any field added to a REST/JSON payload |
| **WARNING** | Column reordered (not for REST/JSON), default value changed, DECIMAL precision/scale/integer digits decreased, partition or clustering keys changed, physical type changed with the same normalized type (`physical_type_changed`; skipped for UNKNOWN/STRUCT/ARRAY), any constraint attribute changed (`constraint_changed`) |
| **BREAKING** | Column removed, NOT NULL column added without default (databases/files), type narrowed (incl. unbounded→varchar(n)), array item type changed, dangerous type conversion; unlisted type pairs default to BREAKING (incl. TIME↔TIMESTAMP) |

Each column's type, physical type, constraints, nullability and default are checked **independently** — one column can produce several changes. When every change is SAFE, `SchemaDiff.suggested_update` holds `explanation` (one line per change) and `schema` (the next baseline). Apply it with `SchemaRegistry.accept_safe_update(table, diff, baseline_version)` (concrete base method; refuses non-SAFE diffs and stale baselines), CLI `--update` on any `check`/`checker run`, or MCP write tool `accept_safe_schema_update` (re-runs the checker server-side; takes no schema from the LLM).

**Sampled sources** (`SAMPLED_SOURCES = {"rest_api", "json_file"}` in `schema_comparison.py`): schemas inferred from example payloads. A null value is UNKNOWN (type not observed) — X→UNKNOWN keeps X and only relaxes nullability; "no nulls seen" never tightens to NOT NULL; added fields are SAFE; key order is ignored. The suggested schema merges baseline knowledge into the new sample (`_merge_column`). ARRAY columns carry `ColumnSchema.items` (element schema; inferred from the first non-null element, UNKNOWN for empty/all-null lists); item changes are named `col[]` / `col[].field` and compared only when both sides recorded items.

---

## Key Conventions

- External dependencies must be justified and declared in `pyproject.toml` `dependencies`; prefer stdlib otherwise
- All public functions return `ToolResult` for LLM agent compatibility
- Severity classification is deterministic — do not involve LLM for clear-cut cases
- `SchemaRegistry` is an ABC — swap backends by instantiating a different class; module-level functions delegate to a `LocalFileRegistry` default instance
- `LocalFileRegistry` storage: `schemas/sources/<table>.json` (versioned list), `schemas/consumers/<consumer>/<table>.json` (versioned list), `schemas/checkers/<schema>.json` (single dict, always overwritten), `schemas/vars.json` (flat dict, always overwritten)
- Checker env vars: resolved from `os.environ` at `Checker.run()` time; never stored as values. Use `$VAR_NAME` in url/header values (REST) or dsn/query (PostgreSQL) or path (file/iceberg); list the name in `env`. `LocalFileRegistry.run_checker()` dispatches to `RestChecker`, `PostgresChecker`, `FileChecker` or `IcebergChecker` based on `checker_type`.
- Template variables (`{{var_name}}`): non-sensitive config values (hosts, base URLs) stored in `schemas/vars.json` via `set_var`/`get_var`/`list_vars`/`delete_var`. Resolved in checker URL/DSN/headers and snapshot CLI args **before** `$ENV_VAR` substitution. **Never allowed in SQL queries** (`security.query_template_error`; enforced by the registry, the CLI, and `PostgresChecker` at run time). Name format: `[a-zA-Z_][a-zA-Z0-9_]*`. Unknown `{{vars}}` left as-is (no error). `SchemaRegistry.resolve_template(template)` is a concrete base-class method — no need to override in new backends.
- `--create-checker` on snapshot commands: after a successful snapshot, auto-builds and registers a checker from the same connection args. `_extract_env_vars(*templates)` in `cli.py` scans URL/DSN/header values for `$VAR_NAME` patterns (regex `\$([A-Z_][A-Z0-9_]*)`) and populates `env` automatically — no `--env` flag needed. Stores the raw (pre-`{{var}}`-resolution) URL/DSN so the checker can resolve at run time. Checker registration failure is non-fatal (prints warning, exits 0).
- PostgreSQL query mode nullability contract: columns with zero NULL values across a `LIMIT sample_size` sample are marked `NOT NULL`; any NULL or empty result → nullable (conservative).
- `SchemaReader.get_schema()` takes no arguments — the target is fixed in the constructor
- `Checker.fetch()` reads the live schema without comparing (all built-in checkers implement it; `run()` = fetch + compare). Registry: `fetch_checker_schema(name)` and `get_schema_history(name)` are concrete base methods returning "unsupported" by default, implemented by `LocalFileRegistry` (they are not among the 16 abstract methods).
- Readers record `physical_type`: Postgres `data_type` (`udt_name` for ARRAY/USER-DEFINED; typname in query mode), SQLite declared type, Arrow type string (structs/lists only their kind, dictionary → value type), Avro logical or primitive/complex name, Iceberg type string, JSON type for REST/JSON payloads (none for null).
- `tests/odcs_tests.py` covers the ODCS adapter (round trip of every reader's output, JSON Schema validation, semver, foreign contracts, `odcs check --update`, MCP export).
- `tests/diff_tests.py` covers diff-engine edge cases (multi-change columns, sampled sources, array items, VARCHAR/DECIMAL bounds, partition keys) and the suggested-update flow (registry, CLI `--update`, MCP).
- `run_tests.py` contains ~25 basic tests (SQLite introspection, schema comparison, STRUCT drift); domain-specific tests live in `tests/` (`registry_tests.py`, `parsers_tests.py`, `rest_tests.py`, `postgresql_tests.py`, `vars_tests.py`, `files_tests.py` — generates Parquet/ORC with pyarrow and Avro fixtures with the stdlib; pyarrow tests are skipped if it isn't installed; `iceberg_tests.py` — hand-written table metadata); all files use a custom minimal test runner (no pytest)
- **Name validation**: every registry entry point that builds a file path validates names with `security.validate_name` (`[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}`, no `..`) and `_safe_path` checks the resolved path stays under `_root`. Template var names use `validate_var_name`. New backends should call these too.
- **MCP security model**: MCP tool inputs are LLM-controlled and therefore untrusted (the LLM reads third-party data such as remote API field names). Write tools are opt-in (`--allow-writes`). MCP `register_checker` enforces: env vars must be `SHELFARD_*` or in `SHELFARD_MCP_ENV_ALLOWLIST`; every `$VAR` referenced must be declared in `env`; URL must be http(s) and resolve to public IPs unless `SHELFARD_MCP_ALLOW_PRIVATE_HOSTS=1`; existing checkers cannot be overwritten. MCP `register_postgres_checker`: same env rules, table mode only, SQL-identifier table/schema, single public host (no multi-host/Unix socket). MCP-registered checkers are stored with `registered_via="mcp"` and guarded **at run time** too: `RestChecker` passes `guard_private_hosts=True` (socket peer check on every connection/redirect hop → defeats DNS rebinding); `PostgresChecker` re-validates the host and pins the DSN with `hostaddr=<ip>`. MCP `set_template_var`/`delete_template_var` refuse vars referenced by any registered checker. CLI-registered checkers (`registered_via="cli"`, the default) are trusted and not subject to these MCP-only checks. All REST fetches use `net.fetch` (cross-origin redirects drop caller headers).
- Timestamps use `datetime.now(timezone.utc).isoformat()` (aware, `+00:00` suffix); older registry entries may have naive timestamps — treat them as opaque strings, don't compare across the two formats.
- Open problems and planned work are tracked in `KNOWN_ISSUES.md`.
- Registry test isolation: patch `registry._default._root = Path(tmp)` inside a `tempfile.TemporaryDirectory()` block
- CLI uses argparse with `dest="command"` at the top level; all commands dispatch via `args.func(args)`; `_print_schema(schema_dict, indent)` recurses into STRUCT fields
