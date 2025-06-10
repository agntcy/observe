# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from enum import Enum

OBSERVE_SPAN_KIND = "ioa_observe.span.kind"
OBSERVE_WORKFLOW_NAME = "ioa_observe.workflow.name"
OBSERVE_ENTITY_NAME = "ioa_observe.entity.name"
OBSERVE_ENTITY_PATH = "ioa_observe.entity.path"
OBSERVE_ENTITY_VERSION = "ioa_observe.entity.version"
OBSERVE_ENTITY_INPUT = "ioa_observe.entity.input"
OBSERVE_ENTITY_OUTPUT = "ioa_observe.entity.output"
OBSERVE_ASSOCIATION_PROPERTIES = "ioa_observe.association.properties"


OBSERVE_PROMPT_MANAGED = "ioa_observe.prompt.managed"
OBSERVE_PROMPT_KEY = "ioa_observe.prompt.key"
OBSERVE_PROMPT_VERSION = "ioa_observe.prompt.version"
OBSERVE_PROMPT_VERSION_NAME = "ioa_observe.prompt.version_name"
OBSERVE_PROMPT_VERSION_HASH = "ioa_observe.prompt.version_hash"
OBSERVE_PROMPT_TEMPLATE = "ioa_observe.prompt.template"
OBSERVE_PROMPT_TEMPLATE_VARIABLES = "ioa_observe.prompt.template_variables"


class ObserveSpanKindValues(Enum):
    WORKFLOW = "workflow"
    TASK = "task"
    AGENT = "agent"
    TOOL = "tool"
    UNKNOWN = "unknown"
