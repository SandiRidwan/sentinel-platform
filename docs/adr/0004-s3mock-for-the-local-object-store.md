# ADR 0004: s3mock for the local object store

- **Status:** Accepted
- **Date:** 2026-10-21
- **Deciders:** data-platform

## Context

The plan was MinIO, which is the usual local S3. In 2026 MinIO withdrew its public
Docker images: `docker pull minio/minio` returns `pull access denied`, and the Quay registry
returns 401. The stack could not start.

## Decision

Use `adobe/s3mock` — a real S3 API implementation published by Adobe for SDK testing.
The application code only knows `S3_ENDPOINT_URL`, so pointing it at real S3 or a MinIO
cluster is a configuration change, not a code change.

## Consequences

The local stack starts reliably with no account and no auth token.

Limitation, stated plainly: s3mock is a test double. It does not implement every S3 feature,
and it keeps state in the container layer (a root-owned volume made its store unreadable and
produced `ListBuckets -> InternalError`). It is not a system of record and does not need to
be: the local lake is rebuilt by re-running the pipeline.
