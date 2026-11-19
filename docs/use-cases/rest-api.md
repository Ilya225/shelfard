# Use case: catch a breaking change in a third-party API

*Your billing service reads `email` and `plan.tier` from a partner's `/customers` endpoint. The partner ships changes without notice.*

```bash
export PARTNER_TOKEN=...                       # secret: stays in the environment, never stored

# 1. Capture the current response shape and store a reusable probe
shelfard rest snapshot https://api.partner.com/v2/customers/42 --name partner_customers \
  --header 'Authorization=Bearer $PARTNER_TOKEN' --create-probe

# 2. Record what your service actually reads
shelfard subscribe partner_customers --consumer billing --columns email,plan.tier

# 3. Later (by hand, cron or CI): has anything changed?
shelfard probe run partner_customers
```

```
✗ Schema drift detected for 'partner_customers'
  2 change(s) detected: 1 breaking, 1 safe.

  [BREAKING · consumer constrained (1)] column_removed  'plan.tier'
  [SAFE · no consumers]                 column_added    'plan.tier_id'

  Consumers: 1 of 1 affected — 1 BREAKING
```

You learn that `billing` breaks *before* it fails in production. New fields in a JSON payload are always SAFE, and a `null` in one response doesn't erase a known type, so a sample that happens to differ doesn't raise false alarms.

## See also

- [REST command reference](../../README.md#cli--rest-endpoint-drift-detection)
- [Consumers and projections](../../README.md#cli--consumers-and-projections)
- [Probes](../../README.md#cli--probe-stored-drift-check-configs)
- [All use cases](README.md)
