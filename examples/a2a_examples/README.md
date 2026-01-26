# Running the A2A examples

You need to set the `OTLP_HTTP_ENDPOINT` variable to point to an otel collector.
One can deploy one using the docker compose file provided in `deploy/` at the root folder of this repo.

You can set it in a `.env` file:
```
$ cat <<< EOF > .env
OTLP_HTTP_ENDPOINT="http://localhost:4318"
EOF
```

## Plain A2A example

Start the server:

`uv run --env-file .env a2aserver`

In another terminal, run the client:

`uv run --env-file .env a2aclient`

## A2A over SLIM example

You need to have an instance of the SLIM gateway running locally to have this working:

```
$ cd examples/remote_agent_slim/
$ docker compose up
```

Start the server:

`uv run --env-file .env slima2aserver`

In another terminal, run the client:

`uv run --env-file .env slima2aclient`
