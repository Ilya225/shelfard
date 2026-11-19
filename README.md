# Shelfard

Detect schema drift in REST APIs, databases and data lake files (Parquet, ORC, Avro, Iceberg). Know exactly what changes and who breaks.

Shelfard snapshots the structure of your data sources, classifies every change as SAFE, WARNING, or BREAKING, and tracks which downstream consumers are affected. Store check configurations once and run them from a CLI, a CI pipeline, an MCP-compatible client, or a conversational agent.

---

## Install

### pip

```bash
pip install git+https://github.com/Ilya225/shelfard.git
```

Or, once published to PyPI:

```bash
pip install shelfard

# With PostgreSQL support (psycopg2-binary)
pip install shelfard[postgres]

# With Parquet / ORC support (pyarrow). Avro and Iceberg need no extra.
pip install shelfard[files]

# ODCS contracts as YAML + JSON Schema validation (JSON contracts need no extra)
pip install shelfard[odcs]
```

### Homebrew

```bash
brew tap Ilya225/shelfard
brew install shelfard
```

Python 3.12+ required.

---

### if you just cloned the repo locally

```bash
conda activate shelfard
```


## CLI — REST endpoint drift detection

### 1. Snapshot — capture a baseline

Fetch an endpoint and store its schema in the local registry under an explicit name:

```bash
shelfard rest snapshot <url> --name <name>
```

```
$ shelfard rest snapshot https://api.example.com/users/1 --name users
Fetching https://api.example.com/users/1 …
✓ Snapshot saved: 'users' (version 1, 6 top-level columns)
```

Add `--create-probe` to register a probe config in the same command — no separate `probe register` step needed. `$VAR_NAME` references in the URL and headers are extracted automatically into the `env` list:

```bash
shelfard rest snapshot https://api.example.com/users/1 --name users \
  --header 'Authorization=Bearer $BEARER_TOKEN' \
  --create-probe
```

```
✓ Snapshot saved: 'users' (version 1, 6 top-level columns)
✓ Probe registered for 'users'  (rest · 1 env var)
```

### 2. Check — detect drift

Fetch the endpoint again and compare against the saved snapshot:

```bash
shelfard rest check <url> --name <name>
```

**No drift:**
```
$ shelfard rest check https://api.example.com/users/1 --name users
Fetching https://api.example.com/users/1 …
✓ No drift detected for 'users'  (last snapshot: 2026-02-28T12:00:00)
```

**Drift detected:**
```
$ shelfard rest check https://api.example.com/users/1 --name users
Fetching https://api.example.com/users/1 …
✗ Schema drift detected for 'users'
  2 change(s): 1 breaking, 1 safe.

  [BREAKING ] column_removed          'email'
               email (varchar) was removed. All consumers reading this column will fail.

  [SAFE     ] column_added            'email_address'
               email_address (varchar) was added. New field in the payload — existing readers are unaffected.
```

**Only safe changes — update suggested:**

When every change is SAFE, the check explains what a new baseline would record and offers to save it. Add `--update` (works on every `check` command and on `probe run`) to register it as the next version:

```
$ shelfard rest check https://api.example.com/users/1 --name users
✓ Safe schema changes for 'users'
  2 change(s) detected: 2 safe.

  [SAFE    ] column_added              'email'
             Column 'email' (varchar) was added. New field in the payload — existing readers are unaffected.

  [SAFE    ] type_resolved             'name'
             'name' had an unknown type (no value was observed when the baseline was captured — null or an
             empty array); it is now varchar. Suggest recording varchar as its type in the next baseline version.

  Suggested baseline update (version 1 → 2):
    • add 'email' (varchar)
    • set the type of 'name' to varchar (was unknown)
  Run the same command with --update to save it.

$ shelfard rest check https://api.example.com/users/1 --name users --update
  …
✓ Baseline updated: 'users' is now version 2
```

`--update` alone never applies WARNING or BREAKING drift. When those changes touch no consumer, `--update --allow-unaffected` accepts them (see [Consumers](#cli--consumers-and-projections)); otherwise review them and re-snapshot. The update is refused if someone re-snapshotted since the check ran.

**Physical types and constraints.** Besides the normalized type, every reader records the source's own type (`physical_type`: `timestamp with time zone`, `int64`, `decimal(10, 2)`, the JSON type for REST payloads). Where the source can tell, it also records constraints: primary keys and single-column UNIQUE for PostgreSQL tables and SQLite, Iceberg identifier fields, and `format: uuid`. A different physical type with the same normalized type (e.g. `timestamptz` → `timestamp`) is a WARNING, and so is any constraint change (primary key removed, unique added, …). Each is compared only when both versions know it, so baselines saved before these fields existed report no extra drift.

**REST / JSON payloads are samples.** A schema inferred from one response says what that response looked like, so for these sources: a `null` value doesn't erase a known type (the baseline keeps it and marks the field nullable); a field that happens to be non-null isn't treated as NOT NULL; new fields are SAFE; key order is ignored; and array element types are tracked (`tags[]`, `tags[].x`).

### Authentication

```bash
# Bearer token
shelfard rest snapshot <url> --name <name> --bearer <token>

# Custom headers (repeatable)
shelfard rest snapshot <url> --name <name> --header X-Api-Key=abc --header X-Tenant=acme
```

### Exit codes

| Code | Meaning |
|------|---------|
| `0`  | Success — no drift detected or `--update` applied (check) or snapshot saved (snapshot) |
| `1`  | Drift detected |
| `2`  | Error — network failure, missing snapshot, parse error |

Exit code `1` on drift makes `check` suitable for use in CI pipelines.

### Help

```bash
shelfard --help
shelfard rest --help
shelfard rest snapshot --help
shelfard rest check --help
```

---

---

## CLI — PostgreSQL drift detection

Snapshot and check the schema of a PostgreSQL table, view, or custom SQL query. Install the optional driver first:

```bash
pip install psycopg2-binary
# or: pip install shelfard[postgres]
```

### Table or view mode

```bash
shelfard postgres snapshot --dsn "postgresql://user:pass@host/db" --table orders --name orders
shelfard postgres check    --dsn "postgresql://user:pass@host/db" --table orders --name orders
```

```
Reading PostgreSQL 'orders' …
✓ Snapshot saved: 'orders' (version 1, 7 top-level columns)
```

Add `--create-probe` to register a probe in the same step. `$VAR_NAME` references in the DSN are extracted automatically:

```bash
shelfard postgres snapshot \
  --dsn 'postgresql://user:$PG_PASS@host/db' \
  --table orders --name orders \
  --create-probe
```

```
Reading PostgreSQL 'orders' …
✓ Snapshot saved: 'orders' (version 1, 7 top-level columns)
✓ Probe registered for 'orders'  (postgres · 1 env var)
```

### Custom SQL query mode

Use this when you want to track the shape of a join, aggregation, or any derived result set — not a single table:

```bash
shelfard postgres snapshot \
  --dsn "postgresql://user:pass@host/db" \
  --query "SELECT o.id, o.total, u.email FROM orders o JOIN users u ON o.user_id = u.id" \
  --name order_summary

shelfard postgres check \
  --dsn "postgresql://user:pass@host/db" \
  --query "SELECT o.id, o.total, u.email FROM orders o JOIN users u ON o.user_id = u.id" \
  --name order_summary
```

Nullability is inferred from sampled data: columns with zero NULL values across the first 100 rows are treated as NOT NULL by contract.

> **Note:** `{{template_vars}}` are not allowed inside `--query` (a stored variable could inject SQL). Write queries literally; use `{{var}}` in the DSN only.

### Credentials via environment variables

Put secrets in env vars rather than the connection string directly:

```bash
export PG_PASS=secret
shelfard postgres snapshot \
  --dsn "postgresql://user:$PG_PASS@host/db" \
  --table orders --name orders
```

### Non-public schema

```bash
shelfard postgres snapshot \
  --dsn "postgresql://user:pass@host/db" \
  --table reports --name reports --db-schema analytics
```

---

## CLI — Parquet, ORC and Avro files

`shelfard file` reads the schema of a local file or a directory of files (e.g. a
Spark output folder or a Hive-partitioned dataset). Only metadata is read —
Parquet/ORC footers, the Avro container header — never row data.

```bash
pip install shelfard[files]   # pyarrow, for Parquet / ORC; Avro needs nothing extra

shelfard file snapshot data/orders_parquet --name orders          # format auto-detected
shelfard file check    data/orders_parquet --name orders          # exit 1 on drift
shelfard file snapshot events.avro --name events
shelfard file snapshot schemas/event.avsc --name event_contract    # Avro schema file
shelfard file list     data/                                       # find sources in a folder
```

Format detection: file extension → magic bytes. Force it with `--format parquet|orc|avro`.
Iceberg tables are refused here (their data folders hold Parquet files) — use `shelfard iceberg`.

| Format | What is read | Notes |
|---|---|---|
| Parquet | File footer (pyarrow) | REQUIRED/OPTIONAL → nullability. Directories: Hive `key=value` folders become `partition_keys` (their types are inferred from folder names, so dates show up as TEXT). |
| ORC | File footer (pyarrow) | ORC has no nullability, so every column is nullable. |
| Avro | Writer schema in the container header, or an `.avsc` file | `["null", T]` → nullable T. Directories: first data file's schema. `default` and `doc` are kept. |

Paths may use `{{var}}` template variables and `$ENV_VARS`. Remote URIs
(`s3://`, `gs://`, ...) are not supported yet.

```bash
shelfard var set lake /mnt/lake
shelfard file snapshot '{{lake}}/orders' --name orders --create-probe
shelfard probe run orders
```

---

## CLI — Iceberg tables

`shelfard iceberg` reads a table's current schema straight from its table
metadata (`metadata/*.metadata.json`) — no catalog connection, JVM or extra
dependency.

```bash
shelfard iceberg list     warehouse/                         # → db.orders, db.users, ...
shelfard iceberg snapshot warehouse/db/orders --name orders  # --create-probe works too
shelfard iceberg check    warehouse/db/orders --name orders  # exit 1 on drift

# Baseline from an older commit, then compare against the current one
shelfard iceberg snapshot warehouse/db/orders/metadata/v1.metadata.json --name orders
shelfard iceberg check    warehouse/db/orders --name orders
```

- The current metadata file comes from `version-hint.text` (Hadoop catalog), otherwise the highest metadata version. Gzip-compressed metadata and format v1/v2/v3 are supported.
- `required` → nullability, `doc` → description, `write-default` → default.
- Partition spec → `partition_keys` (e.g. `day(created_at)`, `bucket[16](id)`); sort order → `clustering_keys`.
- Catalog lookups (REST, Glue, Hive Metastore) and `s3://` paths are not supported yet.

**Sample data.** `scripts/spark_fixtures.py` uses PySpark to write an `orders`
table as Parquet, ORC and Avro in a baseline and a drifted version, plus an
Iceberg table evolved with `ALTER TABLE`:

```bash
pip install pyspark==4.0.1       # needs Java 17 or 21
python scripts/spark_fixtures.py --out sample_data
shelfard file snapshot sample_data/v1/orders_parquet --name orders_pq
shelfard file check    sample_data/v2/orders_parquet --name orders_pq   # 2 BREAKING, 2 SAFE
shelfard iceberg snapshot sample_data/warehouse/db/orders/metadata/v1.metadata.json --name orders_ice
shelfard iceberg check    sample_data/warehouse/db/orders --name orders_ice
```

---

## CLI — Inspect the registry

### Show a schema

Display the full column layout of a registered schema, with type, nullability, and nested STRUCT fields. If a probe is registered for the schema, its type and registration time are shown:

```bash
shelfard show <name>
```

```
$ shelfard show users

Schema: users  (source: rest_api · version: 2026-02-28T12:00:00 · 5 columns)
Probe: rest  (registered 2026-03-01T10:00:00)

  id                       INTEGER      NOT NULL
  email                    VARCHAR      NOT NULL   (max 255)
  name                     TEXT         nullable
  address                  STRUCT       nullable
    .street                VARCHAR      nullable
    .city                  VARCHAR      nullable
  created_at               TIMESTAMP    NOT NULL
```

### List schemas

```bash
shelfard list schemas
```

```
Schemas (2):

  orders               5 cols   1 version    test           2026-02-28T18:00:00
  users                5 cols   2 versions   rest_api       2026-02-28T12:00:00
```

### List consumer subscriptions

```bash
shelfard list subscriptions
```

```
Consumer subscriptions (2):

  blog                 posts          all columns (userId, id, title, body) updated 2026-03-01T18:48:08  · up to date
  report               orders         order_id, quantity             updated 2026-11-18T20:34:01  · outdated (SAFE: quantity)
```

---

## CLI — Consumers and projections

A consumer subscribes to a **projection** of a schema: the columns it reads, with the shape it expects for them. Projections aren't versioned; they always refer to the schema's latest registered version. Every check reports the impact on each consumer.

```bash
# Column paths: top-level columns, nested fields, array items
shelfard subscribe orders --consumer report --columns order_id,quantity
shelfard subscribe orders --consumer crm --columns customer.email      # also: 'tags[]', 'tags[].sku'
shelfard subscribe orders --consumer audit                             # all top-level columns

shelfard consumers orders                              # who reads what; is each projection current?
shelfard unsubscribe orders --consumer crm
shelfard consumer refresh orders --consumer report     # adopt the current shape (consumer's own action)
```

**Consumer impact in every check.** Each change is labelled `no consumers` or `consumer constrained (N)`. Each consumer is judged by its own diff (expected shape vs. the new schema), so it gets its own severity: `int → bigint` on a column it reads is SAFE for it, and removing a column it doesn't read doesn't concern it. The check ends with the number of affected consumers out of the total. Only counts are shown, since a schema may have hundreds of consumers; add `--details` (any `check`, `probe run`, or `odcs check --name`) to list them, or use `shelfard consumers <table>`:

```
$ shelfard probe run orders
✗ Schema drift detected for 'orders'
  4 change(s) detected: 2 breaking, 2 safe.

  [BREAKING · no consumers]             column_removed            'status'
  [SAFE · no consumers]                 column_added              'discount'
  [BREAKING · consumer constrained (1)] column_removed            'customer.email'
  [SAFE · consumer constrained (1)]     type_widened              'quantity'

  Consumers: 2 of 2 affected — 1 BREAKING, 1 SAFE
  Run with --details to list them, or 'shelfard consumers orders'.
```

With `--details`, each constrained change gets a `Consumers: crm BREAKING` line and the summary lists every affected consumer with its severity and the columns involved.

**Breaking changes nobody reads can be accepted.** If every WARNING/BREAKING change has `no consumers`, the check suggests a baseline update (kind `unaffected`) — this also applies to schemas without any consumers ("0 of 0"). Because it accepts breaking changes, it needs an explicit flag:

```
$ shelfard probe run orders --update --allow-unaffected
  …
  Suggested baseline update (version 1 → 2):
    • remove 'status' — BREAKING, no consumer reads it
    • add 'discount' (float)
    • remove 'customer.email' — BREAKING, no consumer reads it
    • widen 'quantity' from integer to bigint
  Contains 2 WARNING/BREAKING change(s) that no consumer reads.
✓ Baseline updated: 'orders' is now version 2
  Consumer projections are unchanged; 1 now differs from the baseline (1 SAFE). Consumers can refresh with 'shelfard consumer refresh orders --consumer NAME'.
  Run with --details to list them, or 'shelfard consumers orders'.
```

`--update` alone still applies only all-SAFE updates. Right before saving, Shelfard checks the consumers again, so a consumer that subscribed to a removed column after the check blocks the update.

**Projections belong to their consumers.** Baseline updates never change them: consumers are often other teams. Instead, checks report `Outdated projections: 1 (1 SAFE)` (names with `--details`) until the consumer runs `shelfard consumer refresh`. Refreshing fails if a column they read no longer exists; they then re-subscribe with the columns they read now.

---

## CLI — Probe (stored drift-check configs)

Register a check configuration once, then run it any time — from the CLI, from code, or via the MCP server — without repeating the URL, DSN, or auth details. Two probe types are supported: `rest` (default) and `postgres`.

The easiest way is `--create-probe` on a snapshot command (see above) — it builds the probe from the same arguments automatically. Use `probe register` when you need to register or update a probe independently.

### REST probe

```bash
shelfard probe register users --url https://api.example.com/users/1
✓ Probe registered for 'users'  (rest · 0 env vars)
```

With authentication (env var names stored, values resolved at run time):

```bash
shelfard probe register users \
  --url 'https://api.example.com/users/1' \
  --header 'Authorization=$BEARER_TOKEN' \
  --env BEARER_TOKEN
```

### PostgreSQL probe — table mode

```bash
shelfard probe register orders \
  --type postgres \
  --dsn 'postgresql://user:$PG_PASS@host/db' \
  --table orders \
  --env PG_PASS
✓ Probe registered for 'orders'  (postgres · 1 env var)
```

### PostgreSQL probe — query mode

Track the schema of a derived result set. Nullability is re-sampled from live data on every run:

```bash
shelfard probe register order_summary \
  --type postgres \
  --dsn 'postgresql://user:$PG_PASS@host/db' \
  --query 'SELECT o.id, o.total, u.email FROM orders o JOIN users u ON o.user_id = u.id' \
  --env PG_PASS
```

### File and Iceberg probes

```bash
shelfard probe register events --type file --path '{{lake}}/events' --format avro
shelfard probe register orders --type iceberg --path '{{lake}}/warehouse/db/orders'
```

### Run a probe

```bash
shelfard probe run <name>
```

```
$ shelfard probe run orders
Checking PostgreSQL 'orders' …
✓ No drift detected for 'orders'  (last snapshot: 2026-03-01T10:00:00)
```

Drift output follows the same format as `shelfard postgres check`. Exit code `1` on drift makes this suitable for CI.

### Show and list probes

```bash
shelfard probe show orders
```

```
Probe: orders
  type:  postgres
  dsn:   postgresql://user:$PG_PASS@host/db
  table: orders
  env:   PG_PASS
```

```bash
shelfard probe list
```

```
Probes (3):

  users          rest     https://api.example.com/users/1          BEARER_TOKEN   2026-03-01T...
  orders         postgres postgresql://user:$PG_PASS@host/db       PG_PASS        2026-03-01T...
  order_summary  postgres postgresql://user:$PG_PASS@host/db       PG_PASS        2026-03-01T...
```

---

## CLI — ODCS data contracts

Shelfard can exchange schemas as [Open Data Contract Standard](https://bitol-io.github.io/open-data-contract-standard/v3.1.0/) (ODCS v3.1.0) documents. The registry keeps its own format; ODCS is used for export, import and for checking a source against a contract.

```bash
# Export a registered schema (latest or --version N) — JSON to stdout, or YAML/JSON by --out extension
shelfard odcs export orders --out orders.odcs.yaml

# Register every schema object of a contract (prints the ODCS fields Shelfard can't represent)
shelfard odcs import orders.odcs.yaml --name-prefix contract_

# Compare a live source (through a stored probe) or a registered schema with a contract
shelfard odcs check orders.odcs.yaml --probe orders
shelfard odcs check orders.odcs.yaml --name orders --object orders --update
```

- **Lossless for Shelfard's own contracts.** Types, nullability (`required`), max length, constraints (`primaryKey`, `unique`, `logicalTypeOptions`), physical types, nested `properties` / `items` and partition keys use native ODCS fields. What ODCS can't express (exact type such as TEXT vs VARCHAR or DECIMAL precision, defaults, clustering keys, Iceberg partition transforms) is kept in `customProperties` named `shelfard.*`.
- **Versions.** The contract `version` is semver derived from the registry history: version 1 is `1.0.0`, and each later version bumps by its diff against the previous one — BREAKING → major, WARNING → minor, SAFE → patch. The contract `id` is stable per schema name.
- **Contracts written by hand or by other tools** are read from their native fields. Their `physicalType` strings (`VARCHAR(2)`) are free-form and not compared, and only the constraints a contract states are checked — a `pattern` in the contract is never compared against a database that can't observe patterns.
- **`odcs check --update`** writes only all-SAFE changes, and only into `schema[].properties`. Everything else stays as-is: `team`, `quality`, `slaProperties`, classifications, examples, your own custom properties, and the contract's property order (new properties go where the source has them). It bumps the patch version. YAML comments are not preserved.
- Exit codes match the other `check` commands (0 no drift or update written, 1 drift, 2 error).

---

## CLI — Template variables

Store repeated non-sensitive config values (hosts, base URLs, ports) once and reference them as `{{var_name}}` in probe configs and snapshot commands. This is distinct from `$ENV_VAR` — template variables are stored in the registry and are not secrets.

### Set and use a variable

```bash
shelfard var set todo_host http://localhost:8080
```

```
✓ Set {{ todo_host }} = 'http://localhost:8080'
```

Now use it anywhere a URL or DSN would appear:

```bash
# Snapshot only
shelfard rest snapshot "{{todo_host}}/api/v1" --name todo_api

# Snapshot + probe in one step (--create-probe extracts $VAR refs automatically)
shelfard rest snapshot "{{todo_host}}/api/v1" --name todo_api --create-probe
shelfard postgres snapshot --dsn "postgresql://user:pass@{{db_host}}/mydb" --table orders --name orders --create-probe
```

Template variables are resolved **before** `$ENV_VAR` substitution, so they can be combined:

```bash
shelfard var set api_base https://api.example.com
shelfard rest snapshot "{{api_base}}/users/1" --name users \
  --header "Authorization=$BEARER_TOKEN" \
  --create-probe
```

### Manage variables

```bash
shelfard var get todo_host          # Show a single variable's value
shelfard var list                   # List all stored variables
shelfard var unset todo_host        # Delete a variable
```

```
Template variables (2):

  {{ api_base               }}  =  'https://api.example.com'
  {{ db_host                }}  =  'localhost'
```

Variables are stored in `schemas/vars.json` in plain text — do not use them for secrets.

---

## CLI — Interactive schema assistant

Start a conversational agent that can query your schema registry. The model is auto-detected from your environment, or specified explicitly with `--model`.

### Auto-detect from environment

```bash
# Claude (if ANTHROPIC_API_KEY is set)
export ANTHROPIC_API_KEY=<your-key>
shelfard agent

# OpenAI (if OPENAI_API_KEY is set)
export OPENAI_API_KEY=<your-key>
shelfard agent

# Local Ollama model (if OLLAMA_MODEL is set and no API key is present)
export OLLAMA_MODEL=qwen2.5:7b
shelfard agent
```

### Explicit model selection

```bash
# Provider shorthand — uses the default model for that provider
shelfard agent --model anthropic      # claude-sonnet-5-5
shelfard agent --model openai         # gpt-5

# Specific model ID
shelfard agent --model claude-opus-5-5
shelfard agent --model gpt-5-mini
```

Set `SHELFARD_DEFAULT_MODEL` (same syntax as `--model`, e.g. `ollama:qwen2.5:7b`) to choose a default without passing the flag every time. `--model` still wins.

The `--model` flag requires the corresponding API key to be set (`ANTHROPIC_API_KEY` for Claude models, `OPENAI_API_KEY` for OpenAI models).

### Local models with Ollama

Run the agent fully locally with [Ollama](https://ollama.com) — no API key needed.

```bash
pip install "shelfard[ollama]"        # installs langchain-ollama
ollama pull qwen2.5:7b

shelfard agent --model ollama                 # $OLLAMA_MODEL, or qwen2.5:7b
shelfard agent --model ollama:qwen2.5:7b      # specific model (tag after the first colon)
shelfard agent --model ollama:llama3.1:8b
```

| Env var | Default | Purpose |
|---|---|---|
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL |
| `OLLAMA_MODEL` | — | Default model for `--model ollama` and auto-detection |
| `OLLAMA_NUM_CTX` | `8192` | Context window; raise it for large schemas |

The model **must support tool calling** (e.g. `qwen2.5`, `qwen3`, `llama3.1`/`3.2`, `mistral-nemo`). Small models (<7B) call tools less reliably.

### Read-only by default

The agent starts in **read-only** mode: it can inspect schemas, subscriptions, probes, and template variables, and run live checks on existing probes. To let it register probes and modify template variables, pass `--allow-writes`:

```bash
shelfard agent --allow-writes
```

In write mode, **every write tool call stops for your approval** before it runs:

```
⚠  The agent wants to run a write tool: set_template_var
     name: api_host
     value: https://api.example.com
   Allow this action? [y/N]
```

Anything other than `y` rejects the call and tells the model not to retry. The [MCP security](#security) guards apply as well.

**Environment passed to the tools.** The agent's MCP subprocess receives `SHELFARD_*` env vars, names listed in `SHELFARD_MCP_ENV_ALLOWLIST`, and the env vars declared by probes **you registered from the CLI** (so `live_check_schema` works for a probe using `$BEARER_TOKEN`). LLM API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, …) are never passed to it.

**Long sessions.** Older turns are summarized automatically once the conversation grows large (scaled to `OLLAMA_NUM_CTX` for local models), keeping the most recent messages verbatim. Type `/reset` to start a fresh conversation.

### Example session

```
Shelfard Agent  [anthropic: claude-sonnet-5-5]  [read-only]
Type 'exit' to quit, '/reset' to start a new conversation.

You: what schemas do I have?
Agent: You have 2 schemas stored:
  • posts — 4 columns, rest_api (saved 2026-03-01T09:38:23)
  • todos — 4 columns, rest_api (saved 2026-02-28T21:46:13)

You: show me the posts schema
Agent: The posts schema has 4 columns:
  • userId   INTEGER  not null
  • id       INTEGER  not null
  • title    VARCHAR  not null
  • body     VARCHAR  not null

You: exit
```

The agent has access to the Shelfard MCP read tools — `get_schemas`, `get_schema` (includes probe info), `get_subscriptions`, `get_subscription`, `get_probe_config`, `live_check_schema`, `get_schema_odcs`, `get_template_var`, `list_template_vars` — plus, with `--allow-writes`, `register_probe`, `register_postgres_probe`, `set_template_var`, `delete_template_var`, and `accept_schema_update`.

---

## MCP server

Shelfard exposes its registry as a standalone **Model Context Protocol server**, so any MCP-compatible client can query schemas and subscriptions directly — without going through the interactive agent.

### Start the server

```bash
shelfard mcp                  # read-only tools (default)
shelfard mcp --allow-writes   # also expose write tools (or SHELFARD_MCP_ALLOW_WRITES=1)
```

The server runs over **stdio** (the MCP standard for local tools). It exposes eight read tools, plus four write tools (marked ✎) when started with `--allow-writes`:

| Tool | Description |
|---|---|
| `get_schemas` | List all registered schemas with summary info |
| `get_schema(schema_name)` | Retrieve a specific schema with full column detail; includes probe info if registered |
| `get_subscriptions` | List all consumer subscriptions |
| `get_subscription(consumer_name, table_name)` | Retrieve a specific consumer subscription |
| ✎ `register_probe(schema_name, url, env, headers)` | Register a **new** REST probe config for a schema (see [Security](#security)) |
| ✎ `register_postgres_probe(schema_name, dsn, table, db_schema, env)` | Register a **new** PostgreSQL probe (table mode only — no custom SQL) |
| `get_probe_config(schema_name)` | Retrieve the stored probe config (url, env vars, headers) |
| `live_check_schema(schema_name)` | Run the registered probe against the live endpoint and return the drift result (with `suggested_update` when every change is SAFE) |
| `get_schema_odcs(schema_name, version?)` | Export a registered schema as an ODCS v3.1.0 contract (semver from the history). Export only — contracts are imported and written from the CLI |
| ✎ `accept_schema_update(schema_name, allow_unaffected=False)` | Re-run the probe and save the suggested schema as the next baseline version — only if every change is SAFE, or (with `allow_unaffected`) if every WARNING/BREAKING change touches no consumer. Never changes consumer projections |
| ✎ `set_template_var(name, value)` | Store a named template variable for use as `{{name}}` in probe configs and snapshot commands |
| `get_template_var(name)` | Retrieve a stored template variable by name |
| `list_template_vars()` | List all stored template variables and their values |
| ✎ `delete_template_var(name)` | Delete a stored template variable by name |

### Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "shelfard": {
      "command": "shelfard",
      "args": ["mcp"]
    }
  }
}
```

Claude will then be able to call the Shelfard tools directly in any conversation.

### Cursor / other MCP clients

Point your MCP client at `shelfard mcp` with stdio transport. The server reads from the local registry (`./schemas/`) in the working directory where it is launched.

### Security

MCP tool inputs come from an LLM, and the LLM reads data that third parties control (field names returned by a REST API, for example). A prompt injection could otherwise trick it into sending your secrets to an attacker's URL, or writing files outside the registry. The server therefore treats every tool input as untrusted:

- **Write tools are off by default.** `register_probe`, `register_postgres_probe`, `set_template_var`, `delete_template_var`, and `accept_schema_update` are only exposed with `--allow-writes`. In `shelfard agent`, each write call also needs your interactive approval.
- **Env-var allowlist.** Probes registered via MCP may only reference env vars prefixed with `SHELFARD_` or listed in `SHELFARD_MCP_ENV_ALLOWLIST` (comma-separated). `$ANTHROPIC_API_KEY`, `$AWS_SECRET_ACCESS_KEY`, etc. are rejected. Every `$VAR` in the URL/headers/DSN must be declared in `env`.
- **Internal-address (SSRF) guard.** Probes registered via MCP must target public hosts — loopback, private, link-local (e.g. `169.254.169.254` cloud metadata), and reserved ranges are rejected. This is checked at registration **and again every time the probe runs**:
  - REST: the address of every TCP connection is verified on the connected socket, including each redirect hop, so DNS rebinding (a name that resolves somewhere else later) is caught.
  - PostgreSQL: the host is re-resolved and validated, and the connection is pinned to that IP via libpq `hostaddr`. Multi-host and Unix-socket DSNs are rejected. Only table mode is allowed (no custom SQL).
  - Set `SHELFARD_MCP_ALLOW_PRIVATE_HOSTS=1` for local development.
- **No tampering with existing probes.** MCP cannot overwrite an existing probe, and cannot change or delete a template variable that a registered probe references.
- **Path-traversal guard.** Schema, table, consumer, and probe names must match `[A-Za-z0-9_][A-Za-z0-9_.-]*` (no `/`, no `..`) — enforced in the registry for every entry point, CLI included.

Probes you register yourself with the CLI are trusted and not subject to the MCP allowlist or host restrictions. Probes store how they were registered (`registered_via: "cli" | "mcp"`).

**Applies to every REST fetch** (CLI or MCP): redirects are followed manually (max 5). On a redirect to a different origin, all custom headers are dropped, so secrets in headers like `X-API-Key` never reach another host. Redirects to non-HTTP(S) URLs are refused.

---

## Docker

Two images are provided for testing and exploration. Both are based on `continuumio/miniconda3` with Python 3.12.

### Smoke test image

Runs a fresh `pip install` and exercises every CLI command against [JSONPlaceholder](https://jsonplaceholder.typicode.com) on every `docker run`. Non-zero exit on any failure.

```bash
docker build -f docker/Dockerfile.test -t shelfard-test .
docker run --rm shelfard-test
```

### Playground image

Shelfard is pre-installed and the registry is pre-seeded with three schemas (`todos`, `users`, `posts`) and two consumer subscriptions. Drops into an interactive bash shell with a welcome message.

```bash
docker build -f docker/Dockerfile.playground -t shelfard-playground .
docker run --rm -it shelfard-playground
```

Mount a named volume to persist your registry across sessions:

```bash
docker run --rm -it -v shelfard-data:/shelfard/schemas shelfard-playground
```

---

## How it works

**For REST endpoints:**
1. **Snapshot** — fetches the endpoint, infers a typed schema from the JSON response (nested objects become `STRUCT` columns), and saves it to the registry under `schemas/sources/`.
2. **Check** — fetches the endpoint again, diffs the new schema against the saved baseline using deterministic rules, and classifies every change as `SAFE`, `WARNING`, or `BREAKING`.

**For PostgreSQL:**
1. **Snapshot (table mode)** — connects and reads column definitions from `information_schema.columns`, capturing exact types and NOT NULL constraints as declared in the catalog.
2. **Snapshot (query mode)** — executes the custom SQL query, resolves PostgreSQL type OIDs via `pg_catalog.pg_type`, and samples up to 100 rows to infer nullability. Columns with zero NULLs in the sample are marked NOT NULL by contract.
3. **Check** — re-reads the live schema using the same mode and diffs against the saved baseline.

Change classification is fully deterministic — no LLM involved for clear-cut cases:

| Severity | Examples |
|----------|---------|
| `SAFE` | Nullable column added, type widened (`int` → `bigint`, `varchar(50)` → `varchar(200)`) |
| `WARNING` | Column reordered, default value changed, physical type changed (same normalized type), constraint changed (primary key, unique, …) |
| `BREAKING` | Column removed, type narrowed, `NOT NULL` column added without a default |

Each change is also labelled with its consumer impact (`no consumers` / `consumer constrained`), and each consumer gets its own severity based on the columns it reads.

---

## Consumer projections (Python API)

```python
from shelfard import LocalFileRegistry

r = LocalFileRegistry()
r.subscribe_consumer("email_service", "users", columns=["email", "address.zip"])
r.subscribe_consumer("reporting_service", "users")          # all top-level columns

# Diff with consumer impact (what probes and check commands use)
result = r.compare_with_baseline("users", baseline_schema_dict, live_schema_dict)
diff = result.data["diff"]
for change in diff["changes"]:
    print(change["column_name"], change["severity"], change["consumer_impact"], change["consumers"])
print(diff["consumer_summary"])     # {"total": 2, "affected": 1, "by_severity": {...}, "outdated": [...]}

# Accept a suggested update ("unaffected" kind needs allow_unaffected=True)
r.accept_update("users", diff, baseline_version, allow_unaffected=True)

r.refresh_consumer("email_service", "users")   # consumer adopts the current shape
```

A projection is stored as one unversioned file at `schemas/consumers/<consumer>/<table>.json` (`columns`, `expected`, `subscribed_at`, `updated_at`).

---

## Using Shelfard from Python (handlers)

Every CLI command and MCP tool is a thin controller over a transport-agnostic
handler: build a payload (a `Command` that writes or a `Query` that reads),
dispatch it, and get a typed `Response` back. A REST/gRPC/UI layer can reuse the
same handlers.

```python
from shelfard.handlers import AcceptUpdate, CheckDrift, Dispatcher, ErrorCode, dispatch
from shelfard import LocalFileRegistry

resp = dispatch(CheckDrift("orders"))            # runs the registered probe; never writes
if not resp.ok:
    print(resp.error.code, resp.error.message)   # e.g. ErrorCode.NOT_FOUND
else:
    report = resp.result                         # DriftReport with a typed SchemaDiff
    for change in report.diff.changes:
        print(change.severity.value, change.column_name, change.consumer_impact)
    if report.diff.suggested_update:
        dispatch(AcceptUpdate("orders", report.diff, report.baseline_version))

# Work on another registry instead of ./schemas
d = Dispatcher(LocalFileRegistry("/path/to/registry"))
```

Error codes (`not_found`, `invalid`, `conflict`, `refused`, `failed`) are left to
each controller to map (the CLI exits with 2; an HTTP layer could use 404/400/409/422/500).

## Pluggable registry backends

The default backend stores schemas as local JSON files. Swap it out by instantiating a different `SchemaRegistry` implementation:

```python
from shelfard import LocalFileRegistry
from shelfard.registry import S3Registry, GCSRegistry, SQLRegistry  # stubs — coming soon

# Default: local filesystem
r = LocalFileRegistry()                          # defaults to ./schemas/
r = LocalFileRegistry("/data/shelfard")          # custom path

# Planned backends (raise NotImplementedError until implemented)
r = S3Registry("my-bucket", prefix="shelfard/")
r = GCSRegistry("my-bucket")
r = SQLRegistry("postgresql://user:pass@host/db")
```

All backends share the same interface (`SchemaRegistry` ABC), so switching is a one-line change.

---

## License

MIT © 2026 illia
