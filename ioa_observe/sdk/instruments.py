# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from enum import Enum


class Instruments(Enum):
    ANTHROPIC = "anthropic"
    BEDROCK = "bedrock"
    CREW = "crew"
    GOOGLE_GENERATIVEAI = "google_generativeai"
    GROQ = "groq"
    LANGCHAIN = "langchain"
    LLAMA_INDEX = "llama_index"
    MISTRAL = "mistral"
    OLLAMA = "ollama"
    OPENAI = "openai"
    REQUESTS = "requests"
    SAGEMAKER = "sagemaker"
    TOGETHER = "together"
    TRANSFORMERS = "transformers"
    URLLIB3 = "urllib3"
    VERTEXAI = "vertexai"
