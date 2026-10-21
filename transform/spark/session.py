"""SparkSession construction and JDK handling.

Spark 3.5 supports JDK 8, 11 and 17. On JDK 18+ Hadoop's UserGroupInformation calls
Subject.getSubject, which was removed, and the failure surfaces as
"UnsupportedOperationException: getSubject is not supported" from deep inside SparkContext.
This module finds a supported JDK and reports clearly when it cannot.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path

# Windows needs winutils.exe and HADOOP_HOME for file:// writes; without them Spark
# warns loudly and some operations fail. Kept in one place so it is set once.
HADOOP_WINUTILS_NOTE = (
    "Windows: set HADOOP_HOME to a directory containing bin/winutils.exe, or accept the "
    "native-io warning (local file writes still work)."
)

SUPPORTED_JDK_MAJORS = {8, 11, 17}

# Spark's own launcher sets these when it detects a newer JDK. Declaring them explicitly
# means a newer JDK is attempted rather than assumed broken.
ADD_OPENS = (
    "java.base/java.lang",
    "java.base/java.lang.invoke",
    "java.base/java.lang.reflect",
    "java.base/java.io",
    "java.base/java.net",
    "java.base/java.nio",
    "java.base/java.util",
    "java.base/java.util.concurrent",
    "java.base/java.util.concurrent.atomic",
    "java.base/sun.nio.ch",
    "java.base/sun.util.calendar",
)


def _ensure_hadoop_home() -> None:
    """Point HADOOP_HOME at a directory with winutils.exe on Windows when one exists.

    Without winutils, Hadoop's NativeIO$Windows.access0 fails with UnsatisfiedLinkError the
    moment Spark touches the local filesystem — a confusing error for a missing binary.
    """
    if platform.system() != "Windows":
        return
    if os.environ.get("HADOOP_HOME"):
        return

    for candidate in (Path(r"C:\hadoop"), Path(os.environ.get("USERPROFILE", ".")) / "hadoop"):
        if (candidate / "bin" / "winutils.exe").exists():
            os.environ["HADOOP_HOME"] = str(candidate)
            os.environ["PATH"] = str(candidate / "bin") + os.pathsep + os.environ.get("PATH", "")
            return
    print("warning: " + HADOOP_WINUTILS_NOTE)


def _jdk_major(java_exe: str) -> int | None:
    import subprocess

    try:
        out = subprocess.run([java_exe, "-version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (out.stderr or "") + (out.stdout or "")
    for token in text.replace('"', " ").split():
        if token[:2].isdigit() and "." in token:
            parts = token.split(".")
            return int(parts[1]) if parts[0] == "1" else int(parts[0])
    return None


def find_supported_jdk() -> tuple[str | None, int | None]:
    """Return (JAVA_HOME, major) for a JDK Spark supports, or (None, None)."""
    candidates: list[str] = []

    if os.environ.get("JAVA_HOME"):
        candidates.append(os.environ["JAVA_HOME"])

    # Common install roots, newest supported first.
    if platform.system() == "Windows":
        for root in (r"C:\Program Files\Eclipse Adoptium", r"C:\Program Files\Java"):
            base = Path(root)
            if base.is_dir():
                candidates.extend(str(p) for p in sorted(base.iterdir(), reverse=True))
    else:
        for pattern in ("/usr/lib/jvm/*", "/opt/java/*", "/Library/Java/JavaVirtualMachines/*"):
            import glob

            candidates.extend(sorted(glob.glob(pattern), reverse=True))

    for home in candidates:
        exe = Path(home) / "bin" / ("java.exe" if platform.system() == "Windows" else "java")
        if not exe.exists():
            continue
        major = _jdk_major(str(exe))
        if major in SUPPORTED_JDK_MAJORS:
            return home, major

    which = shutil.which("java")
    if which:
        major = _jdk_major(which)
        if major in SUPPORTED_JDK_MAJORS:
            return str(Path(which).parent.parent), major

    return None, None


def build_session(
    app_name: str = "sentinel",
    *,
    master: str | None = None,
    extra_conf: dict[str, str] | None = None,
):
    """Create a SparkSession, choosing a JDK Spark supports and failing with a reason."""
    java_home, major = find_supported_jdk()
    if java_home:
        os.environ["JAVA_HOME"] = java_home
        os.environ["PATH"] = str(Path(java_home) / "bin") + os.pathsep + os.environ.get("PATH", "")

    _ensure_hadoop_home()

    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
    os.environ.setdefault("PYSPARK_DRIVER_PYTHON", sys.executable)
    # Without this Spark binds to a hostname that may not resolve, and the worker hangs.
    os.environ.setdefault("SPARK_LOCAL_IP", "127.0.0.1")

    try:
        from pyspark.sql import SparkSession
    except ImportError as exc:  # pragma: no cover - exercised by the DAG fallback path
        raise RuntimeError(
            "pyspark is not installed. Either install requirements-extra.txt or use "
            "transform.spark.local_fallback, which produces the same tables with pandas."
        ) from exc

    builder = (
        SparkSession.builder.appName(app_name)
        .master(master or os.environ.get("SPARK_MASTER", "local[4]"))
        .config("spark.ui.enabled", "false")
        .config("spark.sql.shuffle.partitions", os.environ.get("SPARK_SHUFFLE_PARTITIONS", "8"))
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.driver.host", "127.0.0.1")
        .config("spark.driver.bindAddress", "127.0.0.1")
        .config("spark.driver.extraJavaOptions", " ".join(f"--add-opens={p}=ALL-UNNAMED" for p in ADD_OPENS))
        .config("spark.executor.extraJavaOptions", " ".join(f"--add-opens={p}=ALL-UNNAMED" for p in ADD_OPENS))
    )

    for key, value in (extra_conf or {}).items():
        builder = builder.config(key, value)

    session = builder.getOrCreate()
    session.sparkContext.setLogLevel(os.environ.get("SPARK_LOG_LEVEL", "WARN"))

    if major and major not in SUPPORTED_JDK_MAJORS:
        print(
            f"warning: running on JDK {major}, outside Spark's supported set "
            f"{sorted(SUPPORTED_JDK_MAJORS)}; behaviour is not guaranteed"
        )
    return session
