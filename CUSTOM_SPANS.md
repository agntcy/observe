# Custom Spans with the Observe SDK

This guide covers how to create custom spans when using the Observe SDK with frameworks that don't have built-in auto-instrumentation (e.g., LiteLLM, custom pipelines, or proprietary LLM providers).

## Table of Contents

1. [When Do You Need Custom Spans?](#when-do-you-need-custom-spans)
2. [Quick Start with Decorators](#quick-start-with-decorators)
3. [Manual Span Creation with `get_tracer()`](#manual-span-creation-with-get_tracer)
4. [Tracking LLM Calls with `track_llm_call()`](#tracking-llm-calls-with-track_llm_call)
5. [Association Properties (Custom Metadata)](#association-properties-custom-metadata)
6. [Span Attributes Reference](#span-attributes-reference)

---

## When Do You Need Custom Spans?

The Observe SDK auto-instruments the following frameworks:

| Framework | Auto-Instrumented |
|-----------|:-:|
| OpenAI | ✅ |
| Anthropic | ✅ |
| LangChain / LangGraph | ✅ |
| LlamaIndex | ✅ |
| Bedrock | ✅ |
| CrewAI | ✅ |
| Google Generative AI | ✅ |
| Groq | ✅ |
| Mistral | ✅ |
| Ollama | ✅ |
| Together | ✅ |
| Transformers | ✅ |
| Vertex AI | ✅ |
| SageMaker | ✅ |
| LiteLLM | ❌ |
| Custom / proprietary LLMs | ❌ |

If your LLM provider or framework is **not** in the auto-instrumented list, you need custom spans to get full observability. Note that if you use a non-instrumented library (e.g., LiteLLM) *through* an instrumented framework (e.g., LangChain), the instrumented framework will capture the traces at its layer — no custom spans needed.

---

## Quick Start with Decorators

The simplest way to create custom spans is using the SDK's decorators. These automatically create properly attributed spans around your functions.

```python
import os
from ioa_observe.sdk import Observe
from ioa_observe.sdk.decorators import agent, tool, task, workflow
from ioa_observe.sdk.tracing import session_start

Observe.init("my_service", api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT"))

@workflow(name="my_pipeline")
def run_pipeline(query: str):
    result = my_agent(query)
    return result

@agent(name="my_agent")
def my_agent(query: str):
    context = search_tool(query)
    # Call any LLM — even one without auto-instrumentation
    response = my_llm_call(query, context)
    return response

@tool(name="search")
def search_tool(query: str):
    return "some retrieved context"

session_start()
run_pipeline("What is observability?")
```

Available decorators:

| Decorator | Span Kind | Use Case |
|-----------|-----------|----------|
| `@workflow(name=...)` | `WORKFLOW` | Top-level orchestration |
| `@agent(name=...)` | `AGENT` | Individual agent logic |
| `@task(name=...)` | `TASK` | Sub-steps within an agent |
| `@tool(name=...)` | `TOOL` | Tool invocations |
| `@graph(name=...)` | `graph` | Multi-agent topology |

All decorators also accept optional `description`, `version`, and `method_name` parameters.

---

## Manual Span Creation with `get_tracer()`

For finer-grained control, use `get_tracer()` to obtain an OpenTelemetry `Tracer` and create spans directly.

```python
from ioa_observe.sdk import Observe
from ioa_observe.sdk.tracing import get_tracer, session_start
from ioa_observe.sdk.utils.const import (
    ObserveSpanKindValues,
    OBSERVE_ENTITY_INPUT,
    OBSERVE_ENTITY_OUTPUT,
    OBSERVE_SPAN_KIND,
)

Observe.init("my_service", api_endpoint="http://localhost:4318")
session_start()

with get_tracer() as tracer:
    with tracer.start_as_current_span(
        "my-custom-agent",
        kind=ObserveSpanKindValues.AGENT,
    ) as span:
        span.set_attribute(OBSERVE_SPAN_KIND, ObserveSpanKindValues.AGENT.value)
        span.set_attribute(OBSERVE_ENTITY_INPUT, "What is observability?")

        # Your custom logic here (e.g., LiteLLM call)
        result = call_my_llm("What is observability?")

        span.set_attribute(OBSERVE_ENTITY_OUTPUT, result)
```

### Nested Spans

Spans created inside a `start_as_current_span` block are automatically linked as children:

```python
with get_tracer() as tracer:
    with tracer.start_as_current_span(
        "my-workflow",
        kind=ObserveSpanKindValues.WORKFLOW,
    ) as workflow_span:
        workflow_span.set_attribute(OBSERVE_SPAN_KIND, ObserveSpanKindValues.WORKFLOW.value)
        workflow_span.set_attribute(OBSERVE_ENTITY_INPUT, "user query")

        # Child span — automatically linked to the workflow span
        with tracer.start_as_current_span(
            "my-tool",
            kind=ObserveSpanKindValues.TOOL,
        ) as tool_span:
            tool_span.set_attribute(OBSERVE_SPAN_KIND, ObserveSpanKindValues.TOOL.value)
            tool_span.set_attribute(OBSERVE_ENTITY_INPUT, "search query")
            tool_result = do_search("search query")
            tool_span.set_attribute(OBSERVE_ENTITY_OUTPUT, tool_result)

        workflow_span.set_attribute(OBSERVE_ENTITY_OUTPUT, "final result")
```

### Custom Attributes

You can attach any custom attributes to spans:

```python
with tracer.start_as_current_span("my-span") as span:
    span.set_attribute("custom.model_name", "llama-3")
    span.set_attribute("custom.temperature", 0.7)
    span.set_attribute("custom.retry_count", 2)
```

---

## Tracking LLM Calls with `track_llm_call()`

For LLM calls made through non-instrumented providers, `track_llm_call()` creates spans with proper LLM semantic conventions (model, prompts, completions, token usage).

```python
from ioa_observe.sdk.tracing.manual import track_llm_call, LLMMessage

# Example: Wrapping a LiteLLM call
import litellm

with track_llm_call(vendor="litellm", type="chat") as llm_span:
    # Report the request
    llm_span.report_request(
        model="gpt-4",
        messages=[
            LLMMessage(role="system", content="You are a helpful assistant."),
            LLMMessage(role="user", content="Tell me a joke"),
        ],
    )

    # Make the actual call
    response = litellm.completion(
        model="gpt-4",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Tell me a joke"},
        ],
    )

    # Report the response
    llm_span.report_response(
        model=response.model,
        completions=[choice.message.content for choice in response.choices],
    )
```

### `LLMSpan` Methods

| Method | Parameters | Description |
|--------|-----------|-------------|
| `report_request(model, messages)` | `model: str`, `messages: list[LLMMessage]` | Records the model name and prompt messages |
| `report_response(model, completions)` | `model: str`, `completions: list[str]` | Records the response model and completion texts |

### Combining with Decorators

You can use `track_llm_call()` inside decorated functions for a complete trace:

```python
@agent(name="my_agent")
def my_agent(query: str):
    with track_llm_call(vendor="litellm", type="chat") as llm_span:
        llm_span.report_request(
            model="gpt-4",
            messages=[LLMMessage(role="user", content=query)],
        )
        response = litellm.completion(
            model="gpt-4",
            messages=[{"role": "user", "content": query}],
        )
        llm_span.report_response(
            model=response.model,
            completions=[c.message.content for c in response.choices],
        )
    return response.choices[0].message.content
```

This produces a trace like:

```
my_agent (AGENT span)
  └── litellm.chat (LLM span with model/prompt/completion attributes)
```

---

## Association Properties (Custom Metadata)

Attach custom key-value metadata that propagates to all spans in the current context:

```python
from ioa_observe.sdk import Observe

# Set before or during execution — propagates to all child spans
Observe.set_association_properties({
    "user_id": "user_123",
    "session_id": "sess_abc",
    "environment": "production",
})
```

These appear as `ioa_observe.association.properties.<key>` attributes on every span in the current and child contexts.

---

## Span Attributes Reference

### Observe SDK Attributes

| Constant | Attribute Key | Description |
|----------|--------------|-------------|
| `OBSERVE_SPAN_KIND` | `ioa_observe.span.kind` | Span type: `workflow`, `task`, `agent`, `tool` |
| `OBSERVE_ENTITY_INPUT` | `ioa_observe.entity.input` | Input to the entity (plain text or JSON) |
| `OBSERVE_ENTITY_OUTPUT` | `ioa_observe.entity.output` | Output from the entity |
| `OBSERVE_ENTITY_NAME` | `ioa_observe.entity.name` | Name of the entity |
| `OBSERVE_ENTITY_VERSION` | `ioa_observe.entity.version` | Version of the entity |
| `OBSERVE_WORKFLOW_NAME` | `ioa_observe.workflow.name` | Name of the parent workflow |
| `OBSERVE_ASSOCIATION_PROPERTIES` | `ioa_observe.association.properties` | Prefix for custom metadata |

### Span Kind Values (`ObserveSpanKindValues`)

| Value | Usage |
|-------|-------|
| `ObserveSpanKindValues.WORKFLOW` | Top-level orchestration spans |
| `ObserveSpanKindValues.TASK` | Task/step spans |
| `ObserveSpanKindValues.AGENT` | Agent spans |
| `ObserveSpanKindValues.TOOL` | Tool invocation spans |

Import these from:

```python
from ioa_observe.sdk.utils.const import (
    ObserveSpanKindValues,
    OBSERVE_ENTITY_INPUT,
    OBSERVE_ENTITY_OUTPUT,
    OBSERVE_SPAN_KIND,
    OBSERVE_ENTITY_NAME,
    OBSERVE_ENTITY_VERSION,
    OBSERVE_WORKFLOW_NAME,
    OBSERVE_ASSOCIATION_PROPERTIES,
)
```

---

## Full Example: Custom Framework with LiteLLM

```python
import os
import litellm
from ioa_observe.sdk import Observe
from ioa_observe.sdk.decorators import agent, tool, workflow
from ioa_observe.sdk.tracing import session_start
from ioa_observe.sdk.tracing.manual import track_llm_call, LLMMessage

Observe.init("custom_pipeline", api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT"))


@tool(name="web_search")
def web_search(query: str) -> str:
    # Your search logic
    return f"Results for: {query}"


@agent(name="research_agent")
def research_agent(topic: str) -> str:
    context = web_search(topic)

    with track_llm_call(vendor="litellm", type="chat") as llm_span:
        messages = [
            LLMMessage(role="system", content="Summarize the research."),
            LLMMessage(role="user", content=f"Topic: {topic}\nContext: {context}"),
        ]
        llm_span.report_request(model="gpt-4", messages=messages)

        response = litellm.completion(
            model="gpt-4",
            messages=[{"role": m.role, "content": m.content} for m in messages],
        )

        llm_span.report_response(
            model=response.model,
            completions=[c.message.content for c in response.choices],
        )

    return response.choices[0].message.content


@workflow(name="research_pipeline")
def run_research(topic: str) -> str:
    Observe.set_association_properties({"topic": topic})
    return research_agent(topic)


session_start()
result = run_research("quantum computing")
print(result)
```

This produces a trace like:

```
research_pipeline (WORKFLOW)
  └── research_agent (AGENT)
        ├── web_search (TOOL)
        └── litellm.chat (LLM span — model, prompts, completions)
```
