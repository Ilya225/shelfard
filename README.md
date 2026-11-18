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

Add `--create-checker` to register a checker config in the same command — no separate `checker register` step needed. `$VAR_NAME` references in the URL and headers are extracted automatically into the `env` list:

```bash
shelfard rest snapshot https://api.example.com/users/1 --name users \
  --header 'Authorization=Bearer $BEARER_TOKEN' \
  --create-checker
```

```
✓ Snapshot saved: 'users' (version 1, 6 top-level columns)
✓ Checker registered for 'users'  (rest · 1 env var)
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

When every change is SAFE, the check explains what a new baseline would record and offers to save it. Add `--update` (works on every `check` command and on `checker run`) to register it as the next version:

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

`--update` never applies WARNING or BREAKING drift — review those and re-snapshot to accept them. The update is refused if someone re-snapshotted since the check ran.

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

Add `--create-checker` to register a checker in the same step. `$VAR_NAME` references in the DSN are extracted automatically:

```bash
shelfard postgres snapshot \
  --dsn 'postgresql://user:$PG_PASS@host/db' \
  --table orders --name orders \
  --create-checker
```

```
Reading PostgreSQL 'orders' …
✓ Snapshot saved: 'orders' (version 1, 7 top-level columns)
✓ Checker registered for 'orders'  (postgres · 1 env var)
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
shelfard file snapshot '{{lake}}/orders' --name orders --create-checker
shelfard checker run orders
```

---

## CLI — Iceberg tables

`shelfard iceberg` reads a table's current schema straight from its table
metadata (`metadata/*.metadata.json`) — no catalog connection, JVM or extra
dependency.

```bash
shelfard iceberg list     warehouse/                         # → db.orders, db.users, ...
shelfard iceberg snapshot warehouse/db/orders --name orders  # --create-checker works too
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

Display the full column layout of a registered schema, with type, nullability, and nested STRUCT fields. If a checker is registered for the schema, its type and registration time are shown:

```bash
shelfard show <name>
```

```
$ shelfard show users

Schema: users  (source: rest_api · version: 2026-02-28T12:00:00 · 5 columns)
Checker: rest  (registered 2026-03-01T10:00:00)

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
Consumer subscriptions (3):

  analytics            users          all columns                    2026-02-28T12:00:00
  email_svc            users          email, created_at              2026-02-28T14:00:00
  reporting            orders         all columns                    2026-02-28T16:00:00
```

---

## CLI — Consumer subscriptions

Register which schemas (and columns) a consumer depends on directly from the terminal.

### Full subscription

```bash
shelfard subscribe users --consumer analytics
```

```
✓ Subscribed 'analytics' to 'users' — all 5 columns captured.
```

### Projection

```bash
shelfard subscribe users --consumer email_svc --columns email,created_at
```

```
✓ Subscribed 'email_svc' to 'users' — 2 columns: email, created_at
```

The subscription records a snapshot of the relevant columns at the time of the command. Run `shelfard list subscriptions` to see all registered consumers.

---

## CLI — Checker (stored drift-check configs)

Register a check configuration once, then run it any time — from the CLI, from code, or via the MCP server — without repeating the URL, DSN, or auth details. Two checker types are supported: `rest` (default) and `postgres`.

The easiest way is `--create-checker` on a snapshot command (see above) — it builds the checker from the same arguments automatically. Use `checker register` when you need to register or update a checker independently.

### REST checker

```bash
shelfard checker register users --url https://api.example.com/users/1
✓ Checker registered for 'users'  (rest · 0 env vars)
```

With authentication (env var names stored, values resolved at run time):

```bash
shelfard checker register users \
  --url 'https://api.example.com/users/1' \
  --header 'Authorization=$BEARER_TOKEN' \
  --env BEARER_TOKEN
```

### PostgreSQL checker — table mode

```bash
shelfard checker register orders \
  --type postgres \
  --dsn 'postgresql://user:$PG_PASS@host/db' \
  --table orders \
  --env PG_PASS
✓ Checker registered for 'orders'  (postgres · 1 env var)
```

### PostgreSQL checker — query mode

Track the schema of a derived result set. Nullability is re-sampled from live data on every run:

```bash
shelfard checker register order_summary \
  --type postgres \
  --dsn 'postgresql://user:$PG_PASS@host/db' \
  --query 'SELECT o.id, o.total, u.email FROM orders o JOIN users u ON o.user_id = u.id' \
  --env PG_PASS
```

### File and Iceberg checkers

```bash
shelfard checker register events --type file --path '{{lake}}/events' --format avro
shelfard checker register orders --type iceberg --path '{{lake}}/warehouse/db/orders'
```

### Run a checker

```bash
shelfard checker run <name>
```

```
$ shelfard checker run orders
Checking PostgreSQL 'orders' …
✓ No drift detected for 'orders'  (last snapshot: 2026-03-01T10:00:00)
```

Drift output follows the same format as `shelfard postgres check`. Exit code `1` on drift makes this suitable for CI.

### Show and list checkers

```bash
shelfard checker show orders
```

```
Checker: orders
  type:  postgres
  dsn:   postgresql://user:$PG_PASS@host/db
  table: orders
  env:   PG_PASS
```

```bash
shelfard checker list
```

```
Checkers (3):

  users          rest     https://api.example.com/users/1          BEARER_TOKEN   2026-03-01T...
  orders         postgres postgresql://user:$PG_PASS@host/db       PG_PASS        2026-03-01T...
  order_summary  postgres postgresql://user:$PG_PASS@host/db       PG_PASS        2026-03-01T...
```

---

## CLI — Template variables

Store repeated non-sensitive config values (hosts, base URLs, ports) once and reference them as `{{var_name}}` in checker configs and snapshot commands. This is distinct from `$ENV_VAR` — template variables are stored in the registry and are not secrets.

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

# Snapshot + checker in one step (--create-checker extracts $VAR refs automatically)
shelfard rest snapshot "{{todo_host}}/api/v1" --name todo_api --create-checker
shelfard postgres snapshot --dsn "postgresql://user:pass@{{db_host}}/mydb" --table orders --name orders --create-checker
```

Template variables are resolved **before** `$ENV_VAR` substitution, so they can be combined:

```bash
shelfard var set api_base https://api.example.com
shelfard rest snapshot "{{api_base}}/users/1" --name users \
  --header "Authorization=$BEARER_TOKEN" \
  --create-checker
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

The agent starts in **read-only** mode: it can inspect schemas, subscriptions, checkers, and template variables, and run live checks on existing checkers. To let it register checkers and modify template variables, pass `--allow-writes`:

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

**Environment passed to the tools.** The agent's MCP subprocess receives `SHELFARD_*` env vars, names listed in `SHELFARD_MCP_ENV_ALLOWLIST`, and the env vars declared by checkers **you registered from the CLI** (so `live_check_schema` works for a checker using `$BEARER_TOKEN`). LLM API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, …) are never passed to it.

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

The agent has access to the Shelfard MCP read tools — `get_schemas`, `get_schema` (includes checker info), `get_subscriptions`, `get_subscription`, `get_checker_config`, `live_check_schema`, `get_template_var`, `list_template_vars` — plus, with `--allow-writes`, `register_checker`, `register_postgres_checker`, `set_template_var`, `delete_template_var`, and `accept_safe_schema_update`.

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
| `get_schema(schema_name)` | Retrieve a specific schema with full column detail; includes checker info if registered |
| `get_subscriptions` | List all consumer subscriptions |
| `get_subscription(consumer_name, table_name)` | Retrieve a specific consumer subscription |
| ✎ `register_checker(schema_name, url, env, headers)` | Register a **new** REST checker config for a schema (see [Security](#security)) |
| ✎ `register_postgres_checker(schema_name, dsn, table, db_schema, env)` | Register a **new** PostgreSQL checker (table mode only — no custom SQL) |
| `get_checker_config(schema_name)` | Retrieve the stored checker config (url, env vars, headers) |
| `live_check_schema(schema_name)` | Run the registered checker against the live endpoint and return the drift result (with `suggested_update` when every change is SAFE) |
| ✎ `accept_safe_schema_update(schema_name)` | Re-run the checker and, only if every change is SAFE, save the suggested schema as the next baseline version |
| ✎ `set_template_var(name, value)` | Store a named template variable for use as `{{name}}` in checker configs and snapshot commands |
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

- **Write tools are off by default.** `register_checker`, `register_postgres_checker`, `set_template_var`, `delete_template_var`, and `accept_safe_schema_update` are only exposed with `--allow-writes`. In `shelfard agent`, each write call also needs your interactive approval.
- **Env-var allowlist.** Checkers registered via MCP may only reference env vars prefixed with `SHELFARD_` or listed in `SHELFARD_MCP_ENV_ALLOWLIST` (comma-separated). `$ANTHROPIC_API_KEY`, `$AWS_SECRET_ACCESS_KEY`, etc. are rejected. Every `$VAR` in the URL/headers/DSN must be declared in `env`.
- **Internal-address (SSRF) guard.** Checkers registered via MCP must target public hosts — loopback, private, link-local (e.g. `169.254.169.254` cloud metadata), and reserved ranges are rejected. This is checked at registration **and again every time the checker runs**:
  - REST: the address of every TCP connection is verified on the connected socket, including each redirect hop, so DNS rebinding (a name that resolves somewhere else later) is caught.
  - PostgreSQL: the host is re-resolved and validated, and the connection is pinned to that IP via libpq `hostaddr`. Multi-host and Unix-socket DSNs are rejected. Only table mode is allowed (no custom SQL).
  - Set `SHELFARD_MCP_ALLOW_PRIVATE_HOSTS=1` for local development.
- **No tampering with existing checkers.** MCP cannot overwrite an existing checker, and cannot change or delete a template variable that a registered checker references.
- **Path-traversal guard.** Schema, table, consumer, and checker names must match `[A-Za-z0-9_][A-Za-z0-9_.-]*` (no `/`, no `..`) — enforced in the registry for every entry point, CLI included.

Checkers you register yourself with the CLI are trusted and not subject to the MCP allowlist or host restrictions. Checkers store how they were registered (`registered_via: "cli" | "mcp"`).

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
| `WARNING` | Column reordered, default value changed |
| `BREAKING` | Column removed, type narrowed, `NOT NULL` column added without a default |

---

## Consumer subscriptions (Python API)

Register consumers and the columns they depend on. When a source schema drifts, Shelfard tells you exactly which consumers are impacted.

```python
from shelfard import LocalFileRegistry

r = LocalFileRegistry()

# Full subscription — consumer depends on the entire schema
r.subscribe_consumer("reporting_service", "users")

# Projection — consumer only reads these two columns
r.subscribe_consumer("email_service", "users", columns=["email", "created_at"])

# After detecting drift, find out who is affected
from shelfard import compare_schemas, get_registered_schema
from shelfard.models import SchemaDiff

diff_result = compare_schemas(old_schema, new_schema)
diff = SchemaDiff(**diff_result.data["diff"])  # reconstruct from dict if needed

impact = r.get_consumers_affected_by_diff("users", diff)
for entry in impact.data["affected"]:
    print(entry["consumer"], "→", [c["column_name"] for c in entry["impacted_changes"]])
```

Consumer snapshots are stored under `schemas/consumers/<consumer>/<table>.json` and versioned the same way as source schemas.

---

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
