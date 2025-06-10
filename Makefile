# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

# Check Make version (we need at least GNU Make 3.82)
ifeq ($(filter undefine,$(value .FEATURES)),)
$(error Unsupported Make version. \
    Make $(MAKE_VERSION) detected; please use GNU Make 3.82 or above.)
endif

# Parameters
PYTHON_VERSION = 3.9

# Strict and safe set of defaults for Makefile
# see: https://tech.davis-hansson.com/p/make/
SHELL := bash
.ONESHELL:
.SHELLFLAGS := -euo pipefail -c
.DELETE_ON_ERROR:
MAKEFLAGS += --warn-undefined-variables
MAKEFLAGS += --no-builtin-rules
.RECIPEPREFIX = >

YELLOW := \033[1;33m
GREEN := \033[0;32m
RED := \033[0;31m
NOCOLOR := \033[0m

.DEFAULT_GOAL:=help

VENV_ACTIVATE = set +u; source .venv/bin/activate ; set -u;


## Internal targets ##

# Delete and re-create the virtual environment.
_clean-env:
> @printf "$(YELLOW)Removing the virtual environment$(NOCOLOR)\n"
> rm -rf .venv
> rm -f uv.lock
> uv venv --python $(PYTHON_VERSION)
.PHONY: _clean-env

## Main targets ##

check-env: # Ensure environment variables are set only when this target is explicitly invoked.
> @: $(if $(ARTIFACTORY_USER),, \
       $(error ARTIFACTORY_USER environment variable is not set. Please set this variable.))
> @: $(if $(ARTIFACTORY_PASSWORD),, \
       $(error ARTIFACTORY_PASSWORD environment variable not set. Please set this variable.))

help: # Show the help for each of the Makefile recipes.
> @grep -E '^[a-zA-Z0-9 -]+:.*#'  Makefile | \
  while read -r l; do \
    printf "$(GREEN)$$(echo $$l | cut -f 1 -d':')$(NOCOLOR):$$(echo $$l | cut -f 2- -d'#')\n"; \
  done
.PHONY: help

init: _clean-env update # Initialize the virtual environment.
> @printf "$(YELLOW)Initializing the environment$(NOCOLOR)\n"
> $(VENV_ACTIVATE)
> if [ -d ".git" ]; then \
    pre-commit install; \
  else \
    printf "$(YELLOW)Warning: git repository not found!$(NOCOLOR)\n"; \
    printf "Please don't forget to install pre-commit hooks manually.\n"; \
    printf "Once the git repo is created, (e.g. after git init), run the\n"; \
    printf "following command at the root folder of the repo:\n"; \
    printf "$(GREEN)pre-commit install$(NOCOLOR)\n"; \
  fi;
.PHONY: init

update: pyproject.toml # Update the conda environment after changes to dependencies.
> @printf "$(YELLOW)Updating the virtual environment$(NOCOLOR)\n"
> rm -rf uv.lock  # Removing the lock file make uv slower, but avoids certain corner cases.
> uv sync
.PHONY: update

clean: _clean-env update # Clean up the dist folder and the re-create the virtual environment.
> rm -rf dist/
.PHONY: clean

check: # Run all the pre-commit checks on the repo.
> @printf "$(YELLOW)Running code checkers$(NOCOLOR)\n"
> $(VENV_ACTIVATE)
> pre-commit run --all-files
.PHONY: check

build: update # Build the package. The Wheel file will be written in the dist folder.
> @printf "$(YELLOW)Running Python build$(NOCOLOR)\n"
> $(VENV_ACTIVATE)
> uv build  # use uv for faster builds
.PHONY: build

test: # Run all unit tests.
> @printf "$(YELLOW)Running Pytest$(NOCOLOR)\n"
> $(VENV_ACTIVATE)
> pytest -s tests/
.PHONY: test


install-dependencies: check-env
> echo "Installing dependencies..."
> uv pip install ioa_observe_sdk
