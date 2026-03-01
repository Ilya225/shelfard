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

## CLI — Interactive schema assistant

Start a conversational agent that can query your schema registry using Claude:

```bash
export ANTHROPIC_API_KEY=<your-key>
shelfard agent
```

```
Shelfard Agent  (type 'exit' to quit)

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

The agent has access to two registry tools: listing all schemas and reading a specific one. It can answer questions, summarise schema shapes, and suggest next steps.

---

## How it works

1. **Snapshot** — fetches the endpoint, infers a typed schema from the JSON response (nested objects become `STRUCT` columns), and saves it to a local file-based registry under `schemas/`.
2. **Check** — fetches the endpoint again, diffs the new schema against the saved baseline using deterministic rules, and classifies every change as `SAFE`, `WARNING`, or `BREAKING`.

Change classification is fully deterministic — no LLM involved for clear-cut cases:

| Severity | Examples |
|----------|---------|
| `SAFE` | Nullable column added, type widened (`int` → `bigint`, `varchar(50)` → `varchar(200)`) |
| `WARNING` | Column reordered, default value changed |
| `BREAKING` | Column removed, type narrowed, `NOT NULL` column added without a default |

---

## License

MIT © 2026 illia
