venv:
	uv venv

install-dev:
	uv pip install -e ../../

run-server:
	source .venv/bin/activate && uv run --active python app/server.py

run-client:
	source .venv/bin/activate && uv run --active python client/client_agent.py