SHELL := /bin/zsh
.EXPORT_ALL_VARIABLES:

ifneq ("$(wildcard .env)","")
include .env
endif

export AIRFLOW_HOME ?= $(CURDIR)/airflow_home
export AIRFLOW__CORE__DAGS_FOLDER ?= $(CURDIR)/dags
export AIRFLOW__CORE__LOAD_EXAMPLES ?= False

AIRFLOW_PYTHON := $(firstword $(wildcard $(CURDIR)/.venv-airflow/bin/python3*))
ifeq ($(AIRFLOW_PYTHON),)
AIRFLOW_CMD := airflow
AIRFLOW_LIVE_VALIDATE_PYTHON := python3
else
AIRFLOW_CMD := $(AIRFLOW_PYTHON) -m airflow
AIRFLOW_LIVE_VALIDATE_PYTHON := $(AIRFLOW_PYTHON)
endif

FULL_SEASON_REPLAY_DAYS ?= 365

.PHONY: airflow-init airflow-sync airflow-create-user airflow-webserver airflow-scheduler airflow-trigger airflow-backfill-season airflow-pause airflow-unpause airflow-list airflow-parse airflow-live-validate

airflow-init:
	mkdir -p "$(AIRFLOW_HOME)"
	$(AIRFLOW_CMD) db migrate
	$(AIRFLOW_CMD) dags reserialize
	$(AIRFLOW_CMD) dags list
	$(AIRFLOW_CMD) tasks list nba_analytics_pipeline

airflow-create-user:
	$(AIRFLOW_CMD) users create \
		--username admin \
		--firstname Local \
		--lastname Admin \
		--role Admin \
		--email admin@example.com \
		--password admin

airflow-webserver:
	$(AIRFLOW_CMD) webserver --port 8080

airflow-scheduler:
	$(AIRFLOW_CMD) scheduler

airflow-sync:
	$(AIRFLOW_CMD) dags reserialize
	$(AIRFLOW_CMD) dags list
	$(AIRFLOW_CMD) tasks list nba_analytics_pipeline

airflow-trigger: airflow-init
	$(AIRFLOW_CMD) dags trigger nba_analytics_pipeline

airflow-backfill-season: airflow-init
	NBA_REPLAY_DAYS=$(FULL_SEASON_REPLAY_DAYS) NBA_BRONZE_BOOTSTRAP_MODE=force $(AIRFLOW_CMD) dags trigger nba_analytics_pipeline

airflow-live-validate:
	$(AIRFLOW_LIVE_VALIDATE_PYTHON) scripts/airflow_live_validate.py

airflow-pause:
	$(AIRFLOW_CMD) dags pause --yes nba_analytics_pipeline

airflow-unpause:
	$(AIRFLOW_CMD) dags unpause --yes nba_analytics_pipeline

airflow-list:
	$(AIRFLOW_CMD) dags list

airflow-parse:
	$(AIRFLOW_CMD) dags list-import-errors
	$(AIRFLOW_CMD) tasks list nba_analytics_pipeline
