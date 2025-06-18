# Observe-SDK

[![PyPI version](https://img.shields.io/pypi/v/ioa-observe-sdk.svg)](https://pypi.org/project/ioa-observe-sdk/)

IOA observability SDK for your multi-agentic application.

## Table of Contents

- [Installation](#installation)
- [Schema](#schema)
- [Dev](#dev)
- [Testing](#testing)
- [Getting Started](#getting-started)
- [Contributing](#contributing)

## Installation

To install the package via PyPI, simply run:

```bash
pip install ioa_observe_sdk
```

Alternatively, to download the SDK from git, you could also use the following command. Ensure you have `uv` installed in your environment.

```bash
uv add "git+https://github.com/agntcy/observe"
```

## Schema

The AGNTCY observability schema is an extension of the OTel LLM Semantic Conventions for Generative AI systems.
This schema is designed to provide comprehensive observability for Multi-Agent Systems (MAS).

Link: [AGNTCY Observability Schema](https://github.com/agntcy/observe/blob/main/schema/)

## Dev

To get started with development, start a Clickhouse DB and an OTel collector container locally using docker-compose like so:

```
cd deploy/
docker compose up -d
```

Ensure the contents of `otel-collector.yaml` is correct.

Check the logs of the collector to ensure it is running correctly:

```
docker logs -f otel-collector
```

Create a `.env` file with the following content:

```bash
OTLP_HTTP_ENDPOINT=http://localhost:4318
```

Install the dependencies and activate the virtual environment:

```bash
set -a
source .env
set +a

python3 -m venv .venv
source .venv/bin/activate
uv sync
```

## Testing

To run the unit tests, ensure you have the `OPENAI_API_KEY` set in your environment. You can run the tests using the following command:

```bash
OPENAI_API_KEY=<KEY> make test
```

## 🚀 Getting Started

For getting started with the SDK, please refer to the [Getting Started](https://github.com/agntcy/observe/blob/main/GETTING-STARTED.md)
 file. It contains detailed instructions on how to set up and use the SDK effectively.

## Contributing

Contributions are welcome! Please follow these steps to contribute:

1. Fork the repository.
2. Create a new branch (`git checkout -b feature-branch`).
3. Commit your changes (`git commit -am 'Add new feature'`).
4. Push to the branch (`git push origin feature-branch`).
5. Create a new Pull Request.
