"""Local staging: pull the raw zone down, push the curated zones back up.

Why staging instead of reading s3a:// directly: Spark's S3 connector needs hadoop-aws and
aws-java-sdk jars on the classpath, which is a real deployment step but a poor fit for a
laptop. Staging keeps the transform logic identical in both environments and makes the job
runnable and testable without a full Hadoop client.
"""

from __future__ import annotations

from pathlib import Path

from tools.common.config import get_settings
from tools.common.logging_setup import get_logger
from tools.common.s3 import list_keys, s3_client

log = get_logger("spark.staging")


def staging_root() -> Path:
    root = get_settings().root / "data" / "staging"
    root.mkdir(parents=True, exist_ok=True)
    return root


def download_prefix(bucket: str, prefix: str) -> list[Path]:
    """Download every object under `prefix` into the staging tree, preserving keys.

    Returns the local paths, so the caller knows exactly what it is about to process
    instead of globbing and hoping.
    """
    client = s3_client()
    keys = list_keys(bucket, prefix=prefix)
    written: list[Path] = []
    root = staging_root()

    for key in keys:
        if key.endswith("/"):
            continue
        dest = root / bucket / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.stat().st_size > 0:
            written.append(dest)
            continue
        client.download_file(bucket, key, str(dest))
        written.append(dest)

    log.info(
        "staged raw objects",
        extra={"event": "stage_download", "source": bucket, "rows": len(written)},
    )
    return written


def upload_directory(local_dir: Path, bucket: str, prefix: str, *, patterns: tuple[str, ...] = ("*.parquet",)) -> int:
    """Upload files under `local_dir` to `bucket/prefix`, preserving relative paths."""
    client = s3_client()
    uploaded = 0
    if not local_dir.exists():
        return 0

    for pattern in patterns:
        for path in sorted(local_dir.rglob(pattern)):
            rel = path.relative_to(local_dir).as_posix()
            key = f"{prefix.rstrip('/')}/{rel}" if prefix else rel
            client.upload_file(str(path), bucket, key, ExtraArgs={"ContentType": "application/octet-stream"})
            uploaded += 1

    log.info(
        "uploaded curated objects",
        extra={"event": "stage_upload", "source": bucket, "rows": uploaded},
    )
    return uploaded


def local_zone(name: str) -> Path:
    """A local directory for one medallion zone (raw/bronze/silver)."""
    path = staging_root() / "zones" / name
    path.mkdir(parents=True, exist_ok=True)
    return path
