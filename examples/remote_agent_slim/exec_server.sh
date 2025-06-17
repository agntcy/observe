#!/bin/bash

OTLP_HTTP_ENDPOINT="http://localhost:4318"
OPENAI_API_KEY=""
LLM_BASE_URL=""

if [[ $# -lt 1 ]]
then
  echo "Missing UUID!"
  exit -1
fi

EXECUTION_ID="$1" \
OTLP_HTTP_ENDPOINT="http://localhost:4318" \
OPENAI_API_KEY=${OPENAI_API_KEY} \
LLM_BASE_URL=${LLM_BASE_URL} \
python3 app/main.py
