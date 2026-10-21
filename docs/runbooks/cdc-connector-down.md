# Runbook: the CDC connector is not RUNNING

## Symptom

`python -m cdc.register` exits non-zero, or `python -m cdc.consumer` reports
`no change events were consumed`.

## First check: is it actually the connector?

```bash
curl -s localhost:8083/connectors/sentinel-product-price/status | jq '.connector.state, .tasks[].state'
```

`POST /connectors` returning 201 proves only that the config was accepted. The connector
can fail a second later, and the failure appears here, not in the create response.

## Cause 1 — `wal_level` is not `logical`

The trace contains `logical decoding requires wal_level >= logical`.

```bash
docker compose exec postgres psql -U sentinel -d sentinel -tAc "show wal_level;"
```

`replica` is the Postgres default and makes logical decoding impossible. It is set in
`docker-compose.yml` under the `postgres` service. Changing it requires recreating the
container, and a container created before the change keeps the old setting.

## Cause 2 — the publication or the slot is missing

```bash
docker compose exec postgres psql -U sentinel -d sentinel -tAc "select pubname from pg_publication;"
docker compose exec postgres psql -U sentinel -d sentinel -tAc "select slot_name, active from pg_replication_slots;"
```

`sentinel_cdc` is created by `cdc/seed.py`. A slot left `active` by a killed connector
blocks a new one; delete it only after confirming nothing is reading:

```bash
docker compose exec postgres psql -U sentinel -d sentinel -c "select pg_drop_replication_slot('sentinel_cdc_slot');"
```

## Cause 3 — the topic was never created

```bash
docker compose exec kafka kafka-topics --bootstrap-server 127.0.0.1:9092 --list | grep sentinel
```

If the topic is absent, the connector never snapshotted. Re-registering
(`python -m cdc.register`) recreates it.

## Cause 4 — the host cannot resolve the broker

The consumer fails with `Failed to resolve 'kafka:9092'`.

Kafka advertises a different address per listener. `kafka:9092` is the INTERNAL listener and
only resolves inside the compose network; the host must use the EXTERNAL listener on
`localhost:9094`. If this appears after an edit to `docker-compose.yml`, check that
`KAFKA_ADVERTISED_LISTENERS` still contains `EXTERNAL://localhost:9094` and that the port
mapping is `9094:9094`.

## Recovery

```bash
python -m cdc.register      # deletes and recreates the connector
python -m cdc.seed --mutate # rebuilds the source table with a known change set
python -m cdc.consumer --reset
python -m cdc.parity        # must print PASS with verified > 0
```

`--reset` drops and recreates the Iceberg tables. It exists because a column type change
cannot be applied in place, and because a demo needs a clean slate. It is not a recovery
step for production: dropping the table destroys the history.

## What not to do

Do not "fix" a failing parity check by widening the timestamp tolerance or by removing a
column from `COMPARED_COLUMNS`. A parity check that passes because it stopped looking is
the failure this component exists to prevent. If the two paths disagree, one of them is
wrong, and the check is doing its job.
