# SENTINEL — canonical task entry point (Unix / CI / anyone with make).
# Windows without make: use .\tasks.ps1 with the same command names.
# Both call the identical implementation in tools/tasks/runner.py.

PY ?= python
RUN := $(PY) -m tools.tasks.runner

.DEFAULT_GOAL := help
.PHONY: help doctor up down restart logs seed scrape quality test docs lineage \
        clickhouse-load metabase-import infra-plan infra-destroy k8s-up k8s-down clean all \
        cdc agent-eval ask edge-test controls warehouse-parity bench verify \
        bench-partitioned orchestration-parity experiments trace-parity portability bi-parity \
        edge-service-test

help:            ## show this help
	@$(RUN) help

doctor:          ## verify every tool and service the platform needs
	$(RUN) doctor

up:              ## start the local stack
	$(RUN) up

down:            ## stop the local stack (keeps volumes)
	$(RUN) down

restart:         ## restart the local stack
	$(RUN) restart

logs:            ## tail stack logs
	$(RUN) logs

seed:            ## create buckets and databases
	$(RUN) seed

scrape:          ## run the spiders against declared sources
	$(RUN) scrape

quality:         ## run all four quality levels
	$(RUN) quality

test:            ## run unit tests only (fast)
	$(RUN) test

docs:            ## regenerate dbt docs
	$(RUN) docs

clickhouse-load: ## load gold into ClickHouse
	$(RUN) clickhouse-load

metabase-import: ## create Metabase dashboards from JSON
	$(RUN) metabase-import

lineage:         ## emit and render the lineage graph
	$(RUN) lineage

infra-plan:      ## terraform plan for the dev environment
	$(RUN) infra-plan

infra-destroy:   ## terraform destroy for the dev environment
	$(RUN) infra-destroy

k8s-up:          ## deploy the stack to a local kind cluster
	$(RUN) k8s-up

k8s-down:        ## remove the local kind cluster
	$(RUN) k8s-down

clean:           ## remove generated data, caches and build output
	$(RUN) clean

all:             ## doctor -> up -> seed -> scrape -> quality
	$(RUN) all

cdc:             ## CDC end to end: seed source, register connector, consume, prove parity
	$(RUN) cdc

agent-eval:      ## 20 questions that must be answered, 10 that must be refused
	$(RUN) agent-eval

ask:             ## ask the agent: make ask Q="how many books are out of stock?"
	$(RUN) ask "$(Q)"

edge-test:       ## type-check and test the edge worker (no Cloudflare account needed)
	$(RUN) edge-test

controls:        ## evaluate the control register into an evidence pack
	$(RUN) controls

warehouse-parity: ## parse, transpile and (with credentials) execute across targets
	$(RUN) warehouse-parity

bench:           ## capacity benchmark at 10M rows
	$(RUN) bench

bench-partitioned: ## 100M rows, partitioned vs flat vs Iceberg, and what pruning buys
	$(RUN) bench-partitioned

orchestration-parity: ## the same pipeline through Airflow, Prefect and Dagster
	$(RUN) orchestration-parity

experiments:     ## A/B analysis against a known 5% effect and a known 0% effect
	$(RUN) experiments

trace-parity:    ## one run, three trace backends, read back from each
	$(RUN) trace-parity

portability:     ## the same agent through four frameworks over 30 cases
	$(RUN) portability

bi-parity:       ## one dashboard spec, four definitions, values compared where executed
	$(RUN) bi-parity

edge-service-test: ## type-check and test the TypeScript service
	$(RUN) edge-service-test

verify:          ## check every claim the README makes, in one command
	$(RUN) verify