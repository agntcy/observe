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

Any Opentelemetry compatible backend can be used, but for this guide, we will use ClickhouseDB as the backend database.

### Opentelemetry collector

The OpenTelemetry Collector offers a vendor-agnostic implementation of how to receive, process and export telemetry data. It removes the need to run, operate, and maintain multiple agents/collectors.

### Clickhouse DB

ClickhouseDB is used as a backend database to store and query the collected telemetry data efficiently, enabling you to analyze and visualize observability information for your multi-agentic applications.

### Grafana (optional)

Grafana can be used to visualize the telemetry data collected by the OpenTelemetry Collector and stored in ClickhouseDB.

To get started with development, start a Clickhouse DB and an OTel collector container locally using docker-compose like so:

```
cd deploy/
docker compose up -d
```

Running both locally allows you to test, monitor, and debug your observability setup in a development environment before deploying to production.

Ensure the contents of `otel-collector.yaml` is correct.

Check the logs of the collector to ensure it is running correctly:

```
docker logs -f otel-collector
```

Viewing data in Clickhouse DB can be done using the Clickhouse client. The collector is configured to export telemetry data to Clickhouse.

The clickhouse exporter creates various tables in the Clickhouse DB, including `otel_traces`, which is used to store trace data.

For more info, refer to the [OpenTelemetry Clickhouse Exporter documentation](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/exporter/clickhouseexporter/README.md)

```bash
docker exec -it clickhouse-server clickhouse-client

select * from otel_traces LIMIT 10;
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

### Grafana

To configure Grafana to visualize the telemetry data, follow these steps:

1. Spin up Grafana locally using Docker:

```bash
docker run -d -p 3000:3000 --name=grafana grafana/grafana
```
2. Access Grafana by navigating to `http://localhost:3000` in your web browser.
   - Default username: `admin`
   - Default password: `admin`

3. Add a new data source:
   - Choose "ClickHouse" as the data source type.
   - Set the URL to `http://0.0.0.0:8123`.
   - Configure the authentication settings if necessary.
   - Save and test the connection to ensure it works correctly.

Refer to the [Grafana ClickHouse plugin documentation](https://grafana.com/grafana/plugins/grafana-clickhouse-datasource/) for more details on configuring ClickHouse as a data source.


## Contributing

Contributions are welcome! Please follow these steps to contribute:

1. Fork the repository.
2. Create a new branch (`git checkout -b feature-branch`).
3. Commit your changes (`git commit -am 'Add new feature'`).
4. Push to the branch (`git push origin feature-branch`).
5. Create a new Pull Request.
