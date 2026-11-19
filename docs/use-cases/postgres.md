# Use case: gate PostgreSQL migrations in CI

*Your team owns the `orders` table; analytics and a reporting job read from it. You want a pull request to fail when a migration breaks one of them.*

```bash
# Once: baseline + probe (the DSN keeps $PG_PASS as a placeholder)
shelfard postgres snapshot --dsn 'postgresql://ci:$PG_PASS@db.internal/shop' \
  --table orders --name orders --create-probe
shelfard subscribe orders --consumer analytics --columns order_id,total,created_at
shelfard subscribe orders --consumer reporting --columns order_id,status

# In CI, after applying the migrations to a staging database:
shelfard probe run orders --details      # exit 0 = no drift, 1 = drift, 2 = error
```

A minimal GitHub Actions step (commit the `schemas/` registry so CI sees the baseline):

```yaml
- name: Schema drift check
  env:
    PG_PASS: ${{ secrets.PG_PASS }}
  run: shelfard probe run orders --details
```

When the drift is intentional, accept it as the new baseline:

```bash
shelfard probe run orders --update                       # only if every change is SAFE
shelfard probe run orders --update --allow-unaffected    # also WARNING/BREAKING changes no consumer reads
```

Need the shape of a join or a view instead of a table? Track the query directly with `--query "SELECT ..."` instead of `--table`.

## See also

- [PostgreSQL command reference](../../README.md#cli--postgresql-drift-detection)
- [Exit codes](../../README.md#exit-codes)
- [Consumers and projections](../../README.md#cli--consumers-and-projections)
- [All use cases](README.md)
