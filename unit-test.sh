# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

uv venv
source env/bin/activate
uv sync
python3 -m pytest -s tests/
