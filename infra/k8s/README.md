# Kubernetes

The stack deployed to a local `kind` cluster.

```bash
make k8s-up      # create cluster, build + load the fetcher image, apply, wait for rollout
make k8s-down    # delete the cluster
```

## What runs

| Workload | Kind | Replicas | Notes |
|---|---|---|---|
| `postgres` | StatefulSet | 1 | Airflow metadata + dev warehouse. `volumeClaimTemplate`, because this is the one thing that must survive a pod restart. |
| `clickhouse` | Deployment | 1 | Analytical serving layer. `emptyDir`: the data is rebuildable from gold. |
| `go-fetcher` | Deployment | 2 | Two replicas on purpose — it is stateless and it is the path that needs throughput. |
| `metabase` | Deployment | 1 | Dashboards are imported by `make metabase-import`, not built by clicking. |

## Verified

```
$ kubectl -n sentinel get pods
clickhouse-58f64ddb74-gbqjp   1/1   Running   0   12m
go-fetcher-6c86fd8fbf-8bnfl   1/1   Running   0   12m
go-fetcher-6c86fd8fbf-hrmzv   1/1   Running   0   12m
metabase-84f7b49565-cm96w     1/1   Running   0   92s
postgres-0                    1/1   Running   0   12m

$ curl localhost:8088/healthz     -> {"service":"go-fetcher","status":"ok"}
$ curl localhost:8123/ping        -> Ok.
$ curl localhost:3000/api/health  -> {"status":"ok"}
```

## Two things this deployment taught, both now encoded in the manifests

**Metabase needs its own database.** It crash-looped with `CrashLoopBackOff` and a
connection error that never named the missing database. The compose stack gets the `metabase`
database from `tools/seed/sql` on first Postgres init; Kubernetes has no equivalent hook.
The fix is an `initContainer` that waits for Postgres and creates the database idempotently.

**The cluster and the compose stack cannot both run.** They publish the same host ports
(8088, 8123, 3000). `kind create cluster` fails with
`Bind for 0.0.0.0:8088 failed: port is already allocated`. Stop one before starting the
other:

```bash
docker compose down
make k8s-up
# ...
make k8s-down
docker compose up -d --wait
```

## Why plain manifests, not Helm

Four workloads and four Services do not need a templating layer. A reader can see the whole
object without rendering it first, and there is no values file to keep in sync with the
compose file. Helm earns its place when there are many environments with real variation; here
there are two, and both are read more often than they are changed. Recorded as ADR 0007.

## Validating without a cluster

`kubectl --dry-run=client` still contacts the API server for discovery, so it cannot validate
a manifest on a machine with no cluster. `tools/tasks/k8s_validate.py` checks the invariants
that actually break a deploy:

```bash
python -m tools.tasks.k8s_validate
# manifests: 5
# workloads: clickhouse, go-fetcher, metabase, postgres
# services:  clickhouse, go-fetcher, metabase, postgres
# PASS: manifests are structurally sound
```

It catches the silent failures: a selector that does not match its own pod template (the
Deployment is created and never schedules anything), a container with no readiness probe, a
workload with no Service.

## Secrets

`namespace.yaml` contains a `Secret` with local passwords. That is acceptable for a kind
cluster that exists for one demo and is deleted afterwards. A real cluster reads from an
external secret store; the manifest is written so that only the `secretKeyRef` names would
change.
