"""Does PySpark actually start on this machine?

Run this before blaming a job. A JDK that Spark does not support fails in a way that looks
like a Spark bug (IllegalAccessError / UnsupportedClassVersionError) rather than a JDK
mismatch.

    python -m transform.spark.doctor
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


def jdk_version() -> tuple[str, int | None]:
    java = os.environ.get("JAVA_HOME")
    exe = os.path.join(java, "bin", "java") if java else shutil.which("java")
    if not exe:
        return "not found", None

    try:
        out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"error: {exc}", None

    text = (out.stderr or "") + (out.stdout or "")
    for token in text.replace('"', " ").split():
        if token[:2].isdigit() and "." in token:
            major = int(token.split(".")[0])
            if major == 1:  # 1.8 style
                parts = token.split(".")
                major = int(parts[1]) if len(parts) > 1 else 8
            return token, major
    return "unknown", None


def spark_supported(java_major: int | None) -> bool:
    """Spark 3.5 supports JDK 8, 11 and 17. Newer JDKs are untested by the project."""
    return java_major in {8, 11, 17}


def main() -> int:
    print("PySpark / JDK check")
    print("-" * 50)

    raw, major = jdk_version()
    print(f"  java            {raw}  (major={major})")

    if major is None:
        print("  result          FAIL: no JDK found. Set JAVA_HOME to a JDK 17 install.")
        return 1

    if not spark_supported(major):
        print(
            f"  result          WARN: JDK {major} is outside Spark 3.5's supported set "
            "{8, 11, 17}."
        )
        print("                  Spark may start with --add-opens flags; see ADR 0006.")
        print("                  The pandas fallback (transform.spark.local_fallback)")
        print("                  produces the same tables and does not need a JDK.")

    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)

    try:
        from pyspark.sql import SparkSession
    except ImportError:
        print("  result          FAIL: pyspark not installed (pip install -r requirements-extra.txt)")
        return 1

    builder = (
        SparkSession.builder.appName("sentinel-doctor")
        .master("local[2]")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.shuffle.partitions", "2")
        .config("spark.driver.host", "127.0.0.1")
    )

    # These opens are what Spark itself sets in spark-class when it detects a newer JDK.
    # Declaring them here means a newer JDK is attempted rather than assumed broken.
    for package in (
        "java.base/java.lang",
        "java.base/java.lang.invoke",
        "java.base/java.lang.reflect",
        "java.base/java.io",
        "java.base/java.net",
        "java.base/java.nio",
        "java.base/java.util",
        "java.base/java.util.concurrent",
        "java.base/sun.nio.ch",
        "java.base/sun.security.action",
        "java.base/sun.util.calendar",
    ):
        builder = builder.config(f"spark.driver.extraJavaOptions", f"--add-opens={package}=ALL-UNNAMED")

    try:
        spark = builder.getOrCreate()
    except Exception as exc:  # noqa: BLE001 - the whole point is to report the failure
        print(f"  result          FAIL: SparkSession did not start: {type(exc).__name__}: {exc}")
        return 1

    try:
        spark.sql("SELECT 1").collect()
        version = spark.version
        print(f"  spark           {version}")
        print("  result          PASS: Spark started and executed a query")

        # Iceberg availability is a separate question from Spark starting.
        try:
            spark.conf.set("spark.sql.catalog.local", "org.apache.iceberg.spark.SparkCatalog")
            print("  iceberg         catalog class found in the Spark jars or packages")
        except Exception:
            print("  iceberg         not on the classpath (see requirements-extra.txt)")
        return 0
    finally:
        spark.stop()


if __name__ == "__main__":
    raise SystemExit(main())
