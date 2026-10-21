# ADR 0006: JDK 17 for Spark, with a pandas fallback

- **Status:** Accepted
- **Date:** 2026-10-21
- **Deciders:** data-platform

## Context

Spark 3.5 supports JDK 8, 11 and 17. The development machine had JDK 25, and
`SparkSession.getOrCreate()` failed with
`UnsupportedOperationException: getSubject is not supported` — Hadoop's UserGroupInformation
calls `Subject.getSubject`, which JDK 24 removed.

## Decision

`transform/spark/session.py` selects a supported JDK (8/11/17) from `JAVA_HOME` or the
usual install roots, and reports clearly when it cannot. A pandas implementation
(`transform/spark/local_fallback.py`) produces the same tables for environments with no
usable JDK, and the Airflow DAG chooses between them.

## Consequences

The Spark path runs on a machine whose default JDK is too new, and CI needs no JDK at
all because it takes the fallback.

Both engines are verified to agree: 707 silver rows either way, with the same per-table
counts. That agreement is the property that makes the fallback safe rather than a second
implementation with its own bugs.
