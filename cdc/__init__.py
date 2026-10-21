"""Change data capture for SENTINEL.

The batch path answers "what does the warehouse contain after tonight's run".
This path answers "what changed, and when" — without asking the source database
for a full dump. The two must agree on the final state, and `cdc.parity` proves
that they do.

Layout:
    cdc/schema.sql      the operational table Debezium reads from
    cdc/seed.py         create the table and a deterministic baseline
    cdc/register.py     register the Debezium connector and wait for RUNNING
    cdc/consumer.py     Kafka -> Iceberg (current state + change log)
    cdc/parity.py       batch vs stream comparison, with a negative control
"""
