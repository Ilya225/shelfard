# Shelfard

Schema drift detection for REST APIs and databases.

Capture the shape of a data source once, then re-check it any time to surface unexpected changes before they break downstream pipelines or consumers.

---

## Install

```bash
git clone git@github.com:Ilya225/shelfard.git
cd shelfard
pip install -r requirements.txt
```

Python 3.12+ required.

---

## CLI — REST endpoint drift detection

### 1. Snapshot — capture a baseline

Fetch an endpoint and store its schema in the local registry:

```bash
python3 shelfard.py rest snapshot <url>
```

```
$ python3 shelfard.py rest snapshot https://api.example.com/users/1
Fetching https://api.example.com/users/1 …
✓ Snapshot saved: 'api_example_com_users_1' (version 1, 6 top-level columns)
```

The schema name is derived automatically from the URL. Override it with `--name`:

```bash
python3 shelfard.py rest snapshot https://api.example.com/users/1 --name users
```

### 2. Check — detect drift

Fetch the endpoint again and compare against the saved snapshot:

```bash
python3 shelfard.py rest check <url>
```

**No drift:**
```
$ python3 shelfard.py rest check https://api.example.com/users/1 --name users
Fetching https://api.example.com/users/1 …
✓ No drift detected for 'users'  (last snapshot: 2026-02-28T12:00:00)
```

**Drift detected:**
```
$ python3 shelfard.py rest check https://api.example.com/users/1 --name users
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
python3 shelfard.py rest snapshot <url> --bearer <token>

# Custom headers (repeatable)
python3 shelfard.py rest snapshot <url> --header X-Api-Key=abc --header X-Tenant=acme
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
python3 shelfard.py --help
python3 shelfard.py rest --help
python3 shelfard.py rest snapshot --help
python3 shelfard.py rest check --help
```

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
