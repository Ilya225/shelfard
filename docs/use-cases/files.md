# Use case: watch a data lake output between pipeline runs

*A Spark job writes `orders` as Parquet (or ORC/Avro) every night; a dashboard and an ML feature job read it. You want to know when an upstream code change alters the output schema.*

```bash
shelfard var set lake /mnt/lake                       # non-secret path prefix, reusable

# Find sources and take a baseline (only file metadata is read, never row data)
shelfard file list /mnt/lake/                         # format auto-detected
shelfard file snapshot '{{lake}}/orders' --name orders --create-probe
shelfard subscribe orders --consumer features --columns customer_id,amount

# After each pipeline run
shelfard probe run orders
```

Typical findings: `customer_id` narrowed from `int64` to `int32` (BREAKING for `features`), `amount` written as `float` instead of `double` (WARNING: same normalized type, different physical type), a new nullable column (SAFE), a changed Hive partition column (WARNING). Iceberg tables work the same way with `shelfard iceberg snapshot <table-dir> --name ... --create-probe`, which reads the table metadata directly with no catalog or JVM. You can also snapshot an older `vN.metadata.json` as the baseline to see what changed between two table commits.

## See also

- [Parquet, ORC and Avro reference](../../README.md#cli--parquet-orc-and-avro-files)
- [Iceberg reference](../../README.md#cli--iceberg-tables)
- [Template variables](../../README.md#cli--template-variables)
- [All use cases](README.md)
