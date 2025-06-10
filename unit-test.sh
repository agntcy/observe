uv venv
source env/bin/activate
uv sync
python3 -m pytest -s tests/
