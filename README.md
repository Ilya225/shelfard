# Shelfard

Schema drift detection for REST APIs and databases.

Capture the shape of a data source once, then re-check it any time to surface unexpected changes before they break downstream pipelines or consumers.

---

## Install

### pip

```bash
pip install git+https://github.com/Ilya225/shelfard.git
```

Or, once published to PyPI:

```bash
pip install shelfard
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
               email_address (varchar) was added. Nullable — existing queries unaffected.
```

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
| `0`  | Success — no drift detected (check) or snapshot saved (snapshot) |
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

## CLI — Inspect the registry

### Show a schema

Display the full column layout of a registered schema, with type, nullability, and nested STRUCT fields:

```bash
shelfard show <name>
```

```
$ shelfard show users

Schema: users  (source: rest_api · version: 2026-02-28T12:00:00 · 5 columns)

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
```

### Explicit model selection

```bash
# Provider shorthand — uses the default model for that provider
shelfard agent --model anthropic      # claude-sonnet-4-6
shelfard agent --model openai         # gpt-4o

# Specific model ID
shelfard agent --model claude-opus-4-6
shelfard agent --model gpt-4o-mini
```

The `--model` flag requires the corresponding API key to be set (`ANTHROPIC_API_KEY` for Claude models, `OPENAI_API_KEY` for OpenAI models).

### Example session

```
Shelfard Agent  [claude-sonnet-4-6]  (type 'exit' to quit)

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

The agent has access to four registry tools via the Shelfard MCP server: `get_schemas`, `get_schema`, `get_subscriptions`, `get_subscription`. It can answer questions, summarise schema shapes, and suggest next steps.

---

## MCP server

Shelfard exposes its registry as a standalone **Model Context Protocol server**, so any MCP-compatible client can query schemas and subscriptions directly — without going through the interactive agent.

### Start the server

```bash
shelfard mcp
```

The server runs over **stdio** (the MCP standard for local tools). It exposes four tools:

| Tool | Description |
|---|---|
| `get_schemas` | List all registered schemas with summary info |
| `get_schema(schema_name)` | Retrieve a specific schema with full column detail |
| `get_subscriptions` | List all consumer subscriptions |
| `get_subscription(consumer_name, table_name)` | Retrieve a specific consumer subscription |

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

1. **Snapshot** — fetches the endpoint, infers a typed schema from the JSON response (nested objects become `STRUCT` columns), and saves it to the registry under `schemas/sources/`.
2. **Check** — fetches the endpoint again, diffs the new schema against the saved baseline using deterministic rules, and classifies every change as `SAFE`, `WARNING`, or `BREAKING`.

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
