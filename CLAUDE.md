# Shelfard — Schema Drift Detection

## Project Purpose

Shelfard is a schema drift detection tool for REST APIs and databases. It captures versioned snapshots of data source shapes, classifies every change as SAFE, WARNING, or BREAKING, tracks which downstream consumers are impacted, and stores check configurations for repeatable on-demand or CI-driven runs.

The system is built in layers: **Acquisition** (vendor readers normalize raw schemas), **Registry** (versioned, pluggable storage), **Checkers** (stored run configs with env-var-resolved auth), and **MCP/Agent** (a standalone MCP server and a conversational assistant that expose all tools to LLM clients). Deterministic drift classification is kept strictly separate from LLM reasoning — the agent handles interpretation and orchestration, not severity decisions.

---

## Architecture

Layered design — each layer is deterministic and agent-ready:

- **Layer 1 — Acquisition** (`shelfard/tools/`): Vendor-specific tools co-locate the reader and checker for each data source. Each vendor package (e.g. `tools/rest/`, `tools/postgres/`) contains a `reader.py` (implements `SchemaReader` ABC) and a `checker.py` (implements `Checker` ABC). Shared SQL utilities live in `tools/sql/base.py`. Document parsers live in `shelfard/parsers/`.
- **Layer 1 — Registry** (`shelfard/registry/`): Pluggable registry with a `SchemaRegistry` ABC and a `LocalFileRegistry` implementation. Tracks both source schema versions and consumer subscriptions (full or projected). Stubs exist for S3, GCS, and SQL backends.
- **Layer 2 — Comparison** (`shelfard/schema_comparison.py`): Pure deterministic diffing, produces self-documenting `SchemaDiff`
- **Layer 2 — Checkers** (`shelfard/tools/*/checker.py`): Stored run configurations for drift checks. Two checker types are supported: `RestCheckerConfig` (URL + headers with `$VAR` placeholders and/or `{{var}}` template vars) and `PostgresCheckerConfig` (DSN with `$VAR` placeholders and/or `{{var}}` template vars + table or custom SQL query). Resolution order at `Checker.run()` time: `{{template_vars}}` from registry first, then `$ENV_VARS` from `os.environ`. `LocalFileRegistry.run_checker()` dispatches to the correct `Checker` subclass via the stored `checker_type` field.
- **Layer 2 — Template Variables** (`shelfard/registry/`): Persistent key-value store for non-sensitive config values (hosts, base URLs, ports). Stored in `schemas/vars.json`. Referenced as `{{var_name}}` in checker configs and snapshot CLI commands. Managed via `shelfard var set/get/list/unset` or the MCP tools `set_template_var`, `get_template_var`, `list_template_vars`, `delete_template_var`. Distinct from `$ENV_VAR` (secrets, not stored).
- **Layer 3 — MCP Server** (`shelfard/mcp_server.py`): Standalone FastMCP server built by `build_server(allow_writes)`, stdio transport. Read tools (always): `get_schema` (includes checker info if registered), `get_schemas`, `get_subscriptions`, `get_subscription`, `get_checker_config`, `live_check_schema`, `get_template_var`, `list_template_vars`. Write tools (only with `--allow-writes` / `SHELFARD_MCP_ALLOW_WRITES=1`): `register_checker`, `register_postgres_checker` (table mode only), `set_template_var`, `delete_template_var`. Any MCP client (Claude Desktop, Cursor, etc.) can connect directly. See **MCP security model** below.
- **Layer 3 — Agent** (`shelfard/agent.py`): Interactive LangChain 1.x assistant; spawns the MCP server as a subprocess via `MultiServerMCPClient` and gets its tools from there. Supports Claude, OpenAI, and local Ollama models (`langchain-ollama`, optional extra); model resolved via `--model` flag (`anthropic`, `openai`, `ollama`, `ollama:<model>`, or a model ID) , then `SHELFARD_DEFAULT_MODEL`, then env-var auto-detection (`ANTHROPIC_API_KEY` → `OPENAI_API_KEY` → `OLLAMA_MODEL`). Defaults: `claude-sonnet-5-5`, `gpt-5`, `qwen2.5:7b`. `build_agent()` adds `SummarizationMiddleware` (bounded memory; `/reset` starts a new thread) and, with `--allow-writes`, `HumanInTheLoopMiddleware` so every write tool call needs interactive y/N approval (`run_turn()` / `review_tool_calls()` handle the interrupts). The subprocess env receives `SHELFARD_*` vars, names in `SHELFARD_MCP_ENV_ALLOWLIST`, and env names declared by CLI-registered checkers; LLM API keys (`_NEVER_FORWARD`) are never forwarded.
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
- **Supported sources**: SQLite, REST API endpoints, PostgreSQL (table/view introspection + custom SQL queries); Snowflake, BigQuery (type maps only, readers pending)

## Core Data Models

- **`ColumnType`** (enum): 13 canonical types — `INTEGER`, `BIGINT`, `FLOAT`, `DECIMAL`, `VARCHAR`, `TEXT`, `BOOLEAN`, `DATE`, `TIMESTAMP`, `JSON`, `ARRAY`, `STRUCT`, `UNKNOWN`
- **`ColumnSchema`**: Column metadata — type, nullability, length, precision, default, description, and optionally `fields: list[ColumnSchema]` for `STRUCT` columns (recursive)
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
| **SAFE** | Nullable column added, nullability relaxed, type widened (int→bigint, varchar(50)→varchar(200)) |
| **WARNING** | Column reordered, default value changed, DECIMAL precision decreased |
| **BREAKING** | Column removed, NOT NULL column added without default, type narrowed, dangerous type conversion |

---

## Key Conventions

- External dependencies must be justified and declared in `pyproject.toml` `dependencies`; prefer stdlib otherwise
- All public functions return `ToolResult` for LLM agent compatibility
- Severity classification is deterministic — do not involve LLM for clear-cut cases
- `SchemaRegistry` is an ABC — swap backends by instantiating a different class; module-level functions delegate to a `LocalFileRegistry` default instance
- `LocalFileRegistry` storage: `schemas/sources/<table>.json` (versioned list), `schemas/consumers/<consumer>/<table>.json` (versioned list), `schemas/checkers/<schema>.json` (single dict, always overwritten), `schemas/vars.json` (flat dict, always overwritten)
- Checker env vars: resolved from `os.environ` at `Checker.run()` time; never stored as values. Use `$VAR_NAME` in url/header values (REST) or dsn/query (PostgreSQL); list the name in `env`. `LocalFileRegistry.run_checker()` dispatches to `RestChecker` or `PostgresChecker` based on `checker_type`.
- Template variables (`{{var_name}}`): non-sensitive config values (hosts, base URLs) stored in `schemas/vars.json` via `set_var`/`get_var`/`list_vars`/`delete_var`. Resolved in checker URL/DSN/headers and snapshot CLI args **before** `$ENV_VAR` substitution. **Never allowed in SQL queries** (`security.query_template_error`; enforced by the registry, the CLI, and `PostgresChecker` at run time). Name format: `[a-zA-Z_][a-zA-Z0-9_]*`. Unknown `{{vars}}` left as-is (no error). `SchemaRegistry.resolve_template(template)` is a concrete base-class method — no need to override in new backends.
- `--create-checker` on snapshot commands: after a successful snapshot, auto-builds and registers a checker from the same connection args. `_extract_env_vars(*templates)` in `cli.py` scans URL/DSN/header values for `$VAR_NAME` patterns (regex `\$([A-Z_][A-Z0-9_]*)`) and populates `env` automatically — no `--env` flag needed. Stores the raw (pre-`{{var}}`-resolution) URL/DSN so the checker can resolve at run time. Checker registration failure is non-fatal (prints warning, exits 0).
- PostgreSQL query mode nullability contract: columns with zero NULL values across a `LIMIT sample_size` sample are marked `NOT NULL`; any NULL or empty result → nullable (conservative).
- `SchemaReader.get_schema()` takes no arguments — the target is fixed in the constructor
- `run_tests.py` contains ~25 basic tests (SQLite introspection, schema comparison, STRUCT drift); domain-specific tests live in `tests/` (`registry_tests.py`, `parsers_tests.py`, `rest_tests.py`, `postgresql_tests.py`, `vars_tests.py`); all files use a custom minimal test runner (no pytest)
- **Name validation**: every registry entry point that builds a file path validates names with `security.validate_name` (`[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}`, no `..`) and `_safe_path` checks the resolved path stays under `_root`. Template var names use `validate_var_name`. New backends should call these too.
- **MCP security model**: MCP tool inputs are LLM-controlled and therefore untrusted (the LLM reads third-party data such as remote API field names). Write tools are opt-in (`--allow-writes`). MCP `register_checker` enforces: env vars must be `SHELFARD_*` or in `SHELFARD_MCP_ENV_ALLOWLIST`; every `$VAR` referenced must be declared in `env`; URL must be http(s) and resolve to public IPs unless `SHELFARD_MCP_ALLOW_PRIVATE_HOSTS=1`; existing checkers cannot be overwritten. MCP `register_postgres_checker`: same env rules, table mode only, SQL-identifier table/schema, single public host (no multi-host/Unix socket). MCP-registered checkers are stored with `registered_via="mcp"` and guarded **at run time** too: `RestChecker` passes `guard_private_hosts=True` (socket peer check on every connection/redirect hop → defeats DNS rebinding); `PostgresChecker` re-validates the host and pins the DSN with `hostaddr=<ip>`. MCP `set_template_var`/`delete_template_var` refuse vars referenced by any registered checker. CLI-registered checkers (`registered_via="cli"`, the default) are trusted and not subject to these MCP-only checks. All REST fetches use `net.fetch` (cross-origin redirects drop caller headers).
- Timestamps use `datetime.now(timezone.utc).isoformat()` (aware, `+00:00` suffix); older registry entries may have naive timestamps — treat them as opaque strings, don't compare across the two formats.
- Open problems and planned work are tracked in `KNOWN_ISSUES.md`.
- Registry test isolation: patch `registry._default._root = Path(tmp)` inside a `tempfile.TemporaryDirectory()` block
- CLI uses argparse with `dest="command"` at the top level; all commands dispatch via `args.func(args)`; `_print_schema(schema_dict, indent)` recurses into STRUCT fields
