"""
Generate sample Parquet / ORC / Avro / Iceberg tables with Spark.

Writes two versions of an ``orders`` table so you can try drift detection end
to end:

  v1  — baseline schema
  v2  — drifted schema: ``discount`` added (SAFE), ``quantity`` int→bigint
        (SAFE widening), ``customer.email`` removed (BREAKING), ``status``
        removed (BREAKING)

Layout (default --out ./sample_data):

  sample_data/
    v1/orders_parquet/   (hive-partitioned by order_date)
    v1/orders_orc/
    v1/orders_avro/
    v2/...
    warehouse/db/orders/  (Iceberg table, schema evolved in place v1 → v2)

Requirements (NOT Shelfard dependencies — only needed to generate fixtures):

  pip install pyspark==4.0.1
  Java 17 or 21 (Spark 4.0 does not support newer JDKs). If JAVA_HOME is
  unset or points at another version, the script finds 17/21 via
  /usr/libexec/java_home on macOS.

The spark-avro and iceberg-spark-runtime jars are pulled from Maven Central on
first run.

Usage:
  python scripts/spark_fixtures.py [--out ./sample_data]

Then, for example:
  shelfard file snapshot sample_data/v1/orders_parquet --name orders_pq
  shelfard file check    sample_data/v2/orders_parquet --name orders_pq
  shelfard iceberg snapshot sample_data/warehouse/db/orders --name orders_ice
"""

import argparse
import shutil
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

SPARK_VERSION = "4.0.1"
ICEBERG_VERSION = "1.10.0"
PACKAGES = ",".join([
    f"org.apache.spark:spark-avro_2.13:{SPARK_VERSION}",
    f"org.apache.iceberg:iceberg-spark-runtime-4.0_2.13:{ICEBERG_VERSION}",
])


SUPPORTED_JAVA = (21, 17)


def _java_major(home: str):
    """Major version of the JDK at *home*, read from its ``release`` file."""
    import re

    try:
        text = (Path(home) / "release").read_text()
    except OSError:
        return None
    m = re.search(r'JAVA_VERSION="(?:1\.)?(\d+)', text)
    return int(m.group(1)) if m else None


def _find_java_home():
    """Locate a JDK Spark 4.0 supports (17 or 21); None if none is found."""
    import subprocess

    for major in SUPPORTED_JAVA:
        try:
            out = subprocess.run(
                ["/usr/libexec/java_home", "-v", str(major)],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            continue
        if out and _java_major(out) == major:
            return out
    return None


def _ensure_java():
    """Point JAVA_HOME at a supported JDK; exit with a hint if there is none.

    Spark 4.0 fails on JDK 23+ with ``getSubject is not supported`` /
    ``JAVA_GATEWAY_EXITED``, so a newer default ``java`` must not be used.
    """
    import os
    import sys

    current = os.environ.get("JAVA_HOME")
    if current and _java_major(current) in SUPPORTED_JAVA:
        return
    found = _find_java_home()
    if found:
        if current:
            print(f"JAVA_HOME={current} is not Java 17/21; using {found}")
        os.environ["JAVA_HOME"] = found
        return
    sys.exit(
        "Spark 4.0 needs Java 17 or 21, and none was found.\n"
        "Install one (e.g. Amazon Corretto 21) and set JAVA_HOME to it."
    )


def _spark(warehouse: Path):
    import os
    import sys

    from pyspark.sql import SparkSession

    _ensure_java()

    # Python workers must run the same interpreter as the driver.
    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
    os.environ.setdefault("PYSPARK_DRIVER_PYTHON", sys.executable)

    return (
        SparkSession.builder.appName("shelfard-fixtures")
        .master("local[1]")
        .config("spark.jars.packages", PACKAGES)
        .config("spark.sql.extensions",
                "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
        .config("spark.sql.catalog.local", "org.apache.iceberg.spark.SparkCatalog")
        .config("spark.sql.catalog.local.type", "hadoop")
        .config("spark.sql.catalog.local.warehouse", str(warehouse))
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.ui.enabled", "false")
        .getOrCreate()
    )


def _v1_frame(spark):
    from pyspark.sql.types import (
        ArrayType, DateType, DecimalType, IntegerType, LongType, StringType,
        StructField, StructType, TimestampType,
    )

    schema = StructType([
        StructField("order_id", LongType(), nullable=False),
        StructField("customer", StructType([
            StructField("id", LongType(), nullable=False),
            StructField("email", StringType()),
        ]), nullable=False),
        StructField("quantity", IntegerType()),
        StructField("amount", DecimalType(10, 2)),
        StructField("status", StringType()),
        StructField("tags", ArrayType(StringType())),
        StructField("created_at", TimestampType()),
        StructField("order_date", DateType()),
    ])
    rows = [
        (1, (10, "a@example.com"), 2, Decimal("19.99"), "shipped", ["gift"],
         datetime(2026, 11, 1, 10, 0), date(2026, 11, 1)),
        (2, (11, None), 1, Decimal("5.00"), "pending", [],
         datetime(2026, 11, 2, 12, 30), date(2026, 11, 2)),
    ]
    return spark.createDataFrame(rows, schema)


def _v2_frame(spark):
    from pyspark.sql.types import (
        ArrayType, DateType, DecimalType, DoubleType, LongType, StringType,
        StructField, StructType, TimestampType,
    )

    schema = StructType([
        StructField("order_id", LongType(), nullable=False),
        StructField("customer", StructType([
            StructField("id", LongType(), nullable=False),
        ]), nullable=False),
        StructField("quantity", LongType()),
        StructField("amount", DecimalType(10, 2)),
        StructField("tags", ArrayType(StringType())),
        StructField("created_at", TimestampType()),
        StructField("order_date", DateType()),
        StructField("discount", DoubleType()),
    ])
    rows = [
        (3, (12,), 4, Decimal("42.00"), ["bulk"],
         datetime(2026, 11, 3, 9, 15), date(2026, 11, 3), 0.1),
    ]
    return spark.createDataFrame(rows, schema)


def _write_files(df, base: Path) -> None:
    df.write.mode("overwrite").partitionBy("order_date").parquet(str(base / "orders_parquet"))
    df.write.mode("overwrite").orc(str(base / "orders_orc"))
    df.write.mode("overwrite").format("avro").save(str(base / "orders_avro"))


def _write_iceberg(spark, v1, v2) -> None:
    spark.sql("CREATE NAMESPACE IF NOT EXISTS local.db")
    spark.sql("DROP TABLE IF EXISTS local.db.orders")
    v1.writeTo("local.db.orders").partitionedBy(
        __import__("pyspark.sql.functions", fromlist=["days"]).days("created_at")
    ).create()
    # Evolve the schema in place, the way a real pipeline would.
    spark.sql("ALTER TABLE local.db.orders ADD COLUMN discount double")
    spark.sql("ALTER TABLE local.db.orders ALTER COLUMN quantity TYPE bigint")
    spark.sql("ALTER TABLE local.db.orders DROP COLUMN customer.email")
    spark.sql("ALTER TABLE local.db.orders DROP COLUMN status")
    v2.writeTo("local.db.orders").append()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="sample_data", help="Output directory (default: ./sample_data)")
    args = ap.parse_args()

    out = Path(args.out).resolve()
    if out.exists():
        shutil.rmtree(out)
    warehouse = out / "warehouse"

    spark = _spark(warehouse)
    spark.sparkContext.setLogLevel("WARN")
    try:
        v1, v2 = _v1_frame(spark), _v2_frame(spark)
        _write_files(v1, out / "v1")
        _write_files(v2, out / "v2")
        _write_iceberg(spark, v1, v2)
    finally:
        spark.stop()

    print(f"Sample tables written to {out}")


if __name__ == "__main__":
    main()
