# SENTINEL — canonical task entry point (Unix / CI / anyone with make).
# Windows without make: use .\tasks.ps1 with the same command names.
# Both call the identical implementation in tools/tasks/runner.py.

PY ?= python
RUN := $(PY) -m tools.tasks.runner

.DEFAULT_GOAL := help
.PHONY: help doctor up down restart logs seed scrape quality test docs lineage \
        clickhouse-load metabase-import infra-plan infra-destroy k8s-up k8s-down clean all

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