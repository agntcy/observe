#!/bin/bash

OTLP_HTTP_ENDPOINT="http://localhost:4318"

if [[ $# -lt 1 ]]
then
  echo "Missing UUID!"
  exit -1
fi

EXECUTION_ID="$1" \
OTLP_HTTP_ENDPOINT="http://localhost:4318" \
python3 client/slim.py
