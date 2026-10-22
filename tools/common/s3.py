"""S3 helpers.

The application only ever talks to a boto3 client built here, so moving from the local
s3mock endpoint to real AWS S3 or a MinIO cluster is a change to `.env`, not to code.
"""

from __future__ import annotations

import io
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError

from tools.common.config import get_settings
from tools.common.logging_setup import get_logger

log = get_logger("s3")


def s3_client():
    """A path-style S3 client pointed at the configured endpoint."""
    settings = get_settings()
    client = boto3.client(
        "s3",
        endpoint_url=settings.s3.endpoint_url,
        aws_access_key_id=settings.s3.access_key_id,
        aws_secret_access_key=settings.s3.secret_access_key,
        region_name=settings.s3.region,
        config=BotoConfig(
            s3={"addressing_style": "path" if settings.s3.force_path_style else "virtual"},
            # Fail fast. A dead endpoint should surface in seconds, not hang a crawl for
            # minutes while boto retries with long timeouts.
            retries={"max_attempts": 2, "mode": "standard"},
            connect_timeout=5,
            read_timeout=20,
        ),
    )
    # Drop `Expect: 100-continue` on every request.
    #
    # boto3 adds that header to PUT/POST. s3mock 5.2.3 closes the connection instead of
    # answering it, so boto3 reports `ConnectionClosedError` while the object is in fact
    # written: a PUT that looks like a failure and leaves the data behind. GET is unaffected,
    # which is why this first appeared as a read error. curl does not send the header by
    # default, which is how it was isolated.
    #
    # The header is added late, so this has to run on `before-send`, not `before-call`: at
    # before-call the header dict is still empty. botocore passes `request` as a keyword here,
    # which is why the signature is `request=None` rather than positional.
    # Expect is not part of the SigV4 canonical request, so removing it is safe for real S3.
    def _drop_expect_header(request=None, **_kwargs):
        headers = getattr(request, "headers", None)
        if headers is not None:
            headers.pop("Expect", None)

    client.meta.events.register_last("before-send.s3.*", _drop_expect_header)
    return client


def ensure_buckets() -> list[str]:
    """Create any missing bucket. Idempotent: safe to run on every seed."""
    client = s3_client()
    created: list[str] = []
    for bucket in get_settings().s3.buckets:
        try:
            client.head_bucket(Bucket=bucket)
            log.info("bucket exists", extra={"event": "bucket_ok", "source": bucket})
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"404", "NoSuchBucket", "NotFound"}:
                client.create_bucket(Bucket=bucket)
                created.append(bucket)
                log.info("bucket created", extra={"event": "bucket_created", "source": bucket})
            else:
                raise
    return created


def list_buckets() -> list[str]:
    client = s3_client()
    return sorted(b["Name"] for b in client.list_buckets().get("Buckets", []))


def put_bytes(key: str, data: bytes, bucket: str | None = None, content_type: str | None = None) -> str:
    settings = get_settings()
    target = bucket or settings.s3.bucket_raw
    extra = {"ContentType": content_type} if content_type else {}
    s3_client().put_object(Bucket=target, Key=key, Body=data, **extra)
    return f"s3://{target}/{key}"


def put_text(key: str, text: str, bucket: str | None = None, content_type: str = "text/plain") -> str:
    return put_bytes(key, text.encode("utf-8"), bucket=bucket, content_type=content_type)


def get_bytes(key: str, bucket: str | None = None) -> bytes:
    settings = get_settings()
    target = bucket or settings.s3.bucket_raw
    body = s3_client().get_object(Bucket=target, Key=key)["Body"]
    return body.read()


def list_keys(bucket: str, prefix: str = "", limit: int = 1000) -> list[str]:
    paginator = s3_client().get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
            if len(keys) >= limit:
                return keys
    return keys


@contextmanager
def open_stream(key: str, bucket: str | None = None) -> Iterator[io.BytesIO]:
    """Read an object into memory. Small objects only; large reads use pyarrow dataset."""
    yield io.BytesIO(get_bytes(key, bucket))


def partition_path(*, source: str, table: str, ingested_at: datetime | None = None) -> str:
    """Hive-style partition path: raw/<source>/<table>/dt=YYYY-MM-DD/hour=HH/."""
    when = ingested_at or datetime.now(timezone.utc)
    return f"{source}/{table}/dt={when:%Y-%m-%d}/hour={when:%H}"
