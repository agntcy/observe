# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import uuid

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.context import attach, detach, set_value
from opentelemetry.sdk.trace import TracerProvider

from ioa_observe.sdk.instrumentations.openai import instrument_openai_tool_presentations
from ioa_observe.sdk.tracing.runtime_event_emitter import (
    register_runtime_event_listener,
    unregister_runtime_event_listener,
)


@pytest.fixture
def capture(monkeypatch):
    from openai._base_client import BaseClient

    monkeypatch.setattr(BaseClient, "_build_request", BaseClient._build_request)
    monkeypatch.setenv("OBSERVE_TRACING_ENABLED", "true")
    monkeypatch.setenv("OBSERVE_TRACE_CONTENT", "true")
    monkeypatch.setenv("OBSERVE_REALTIME_OBSERVABILITY_ENABLED", "true")
    monkeypatch.setattr(
        "ioa_observe.sdk.tracing.runtime_event_emitter._emit_to_otel_logs",
        lambda *args: None,
    )
    instrument_openai_tool_presentations()
    instrument_openai_tool_presentations()
    events = []
    register_runtime_event_listener(events.append)
    provider = TracerProvider()
    yield events, provider.get_tracer(__name__)
    unregister_runtime_event_listener(events.append)
    provider.shutdown()


def tool():
    return {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Search documents",
            "parameters": {
                "type": "object",
                "properties": {"password": {"type": "string"}},
            },
        },
    }


@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.parametrize("endpoint", ["chat", "completions", "responses"])
@pytest.mark.parametrize("stream", [False, True])
def test_presented_definitions_match_serialized_body_before_send(
    capture, use_async, endpoint, stream
):
    import openai

    events, tracer = capture
    calls = []
    fields = (
        {"functions": [tool()["function"]], "function_call": "auto"}
        if endpoint == "completions"
        else {
            "tools": [
                tool()
                if endpoint == "chat"
                else {"type": "function", **tool()["function"]}
            ],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
        }
    )

    def respond(request):
        calls.append(request)
        [event] = [e for e in events if e["event.name"] == "llm.tools.presented"]
        assert event["event.name"] == "llm.tools.presented"
        assert event["capability.source"] == "llm-presented"
        assert event["capability.capture.state"] == "complete"
        body = json.loads(request.content)
        assert json.loads(event["capability.content"]) == {
            key: body[key] for key in fields
        }
        assert event["session.id"] == "session-one"
        assert event["llm.name"] == "model-one"
        if endpoint != "responses":
            assert event["llm.call.id"] == event["span.id"]
        else:
            assert "llm.call.id" not in event
        context = trace.get_current_span().get_span_context()
        assert event["span.id"] == f"{context.span_id:016x}"
        assert event["trace.id"] == f"{span.get_span_context().trace_id:032x}"
        assert "private prompt" not in event["capability.content"]
        assert "Authorization" not in event
        if stream:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content="data: [DONE]\n\n",
            )
        return httpx.Response(
            200,
            json={
                "id": uuid.uuid4().hex,
                "object": "response",
                "model": "model-one",
                "output": [],
                "choices": [
                    {
                        "index": 0,
                        "text": "ok",
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    def request_kwargs():
        kwargs = {
            "model": "model-one",
            "stream": stream,
            "extra_body": fields,
            **(
                {"messages": [{"role": "user", "content": "private prompt"}]}
                if endpoint == "chat"
                else {
                    "input" if endpoint == "responses" else "prompt": "private prompt"
                }
            ),
        }
        if endpoint != "completions":
            # extra_body must win over the original, unfiltered definition.
            kwargs["tools"] = []
        return kwargs

    def call():
        if use_async:

            async def async_call():
                async with openai.AsyncOpenAI(
                    api_key="unused-local-test",
                    base_url="https://model.test/v1",
                    http_client=httpx.AsyncClient(
                        transport=httpx.MockTransport(respond)
                    ),
                    max_retries=0,
                ) as client:
                    resource = (
                        client.chat.completions
                        if endpoint == "chat"
                        else getattr(client, endpoint)
                    )
                    result = await resource.create(**request_kwargs())
                    if stream:
                        if hasattr(result, "aclose"):
                            await result.aclose()
                        else:
                            await result.close()

            asyncio.run(async_call())
        else:
            with openai.OpenAI(
                api_key="unused-local-test",
                base_url="https://model.test/v1",
                http_client=httpx.Client(transport=httpx.MockTransport(respond)),
                max_retries=0,
            ) as client:
                resource = (
                    client.chat.completions
                    if endpoint == "chat"
                    else getattr(client, endpoint)
                )
                result = resource.create(**request_kwargs())
                if stream:
                    result.close()

    with tracer.start_as_current_span(
        "model-call", attributes={"session.id": "session-one"}
    ) as span:
        token = attach(set_value("session.id", "session-one"))
        try:
            call()
        finally:
            detach(token)
    assert len(calls) == 1
    [presented] = [e for e in events if e["event.name"] == "llm.tools.presented"]
    for event in events:
        if event["event.name"] == "llm.started" and endpoint != "responses":
            assert event["llm.call.id"] == presented["llm.call.id"]


@pytest.mark.parametrize(
    "fields,env,state,expected",
    [
        ({}, {}, None, None),
        ({"tools": []}, {}, "complete", {"tools": []}),
        ({"tools": [tool()]}, {"OBSERVE_TRACE_CONTENT": "false"}, "disabled", None),
        ({"tools": [tool()]}, {"OBSERVE_TRACING_ENABLED": "false"}, None, None),
        (
            {"tools": [tool()]},
            {"OBSERVE_REALTIME_OBSERVABILITY_ENABLED": "false"},
            None,
            None,
        ),
        (
            {"tools": [tool()]},
            {"OBSERVE_CAPABILITY_MAX_PAYLOAD_BYTES": "10"},
            "truncated",
            None,
        ),
        (
            {"tools": [{"type": "custom", "api_key": "sensitive-value"}]},
            {},
            "redacted",
            {"tools": [{"type": "custom", "api_key": "[REDACTED]"}]},
        ),
    ],
)
def test_capture_controls_and_sessionless_requests(
    capture, monkeypatch, fields, env, state, expected
):
    import openai

    events, _ = capture
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    token = attach(set_value("session.id", None))
    try:
        with openai.OpenAI(
            api_key="unused-local-test",
            http_client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json={"choices": []})
                )
            ),
        ) as client:
            client.chat.completions.create(model="m", messages=[], extra_body=fields)
    finally:
        detach(token)
    if state is None:
        assert events == []
    else:
        [event] = events
        assert "session.id" not in event
        assert event["capability.capture.state"] == state
        if expected is None:
            assert "capability.content" not in event
        else:
            assert json.loads(event["capability.content"]) == expected


def test_export_failure_does_not_change_request_or_response(
    capture, monkeypatch, caplog
):
    import openai

    def fail(*args, **kwargs):
        raise RuntimeError("export unavailable")

    monkeypatch.setattr(
        "ioa_observe.sdk.instrumentations.openai.record_capability_event", fail
    )
    with openai.OpenAI(
        api_key="unused-local-test",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"choices": []})
            )
        ),
    ) as client:
        result = client.chat.completions.create(model="m", messages=[], tools=[tool()])
    assert result.choices == []
    assert "Unable to capture OpenAI tool presentation" in caplog.text


def test_retries_are_separate_pre_send_observations(capture):
    import openai

    events, _ = capture
    requests = []

    def respond(request):
        requests.append(request)
        presentations = [
            event for event in events if event["event.name"] == "llm.tools.presented"
        ]
        assert len(presentations) == len(requests)
        if len(requests) == 1:
            return httpx.Response(
                429,
                headers={"retry-after-ms": "1"},
                json={"error": {"message": "try again"}},
            )
        return httpx.Response(200, json={"choices": []})

    with openai.OpenAI(
        api_key="unused-local-test",
        max_retries=1,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        result = client.chat.completions.create(model="m", messages=[], tools=[tool()])
    assert result.choices == []
    assert len(requests) == 2
    assert requests[0].content == requests[1].content


def test_nonrecording_context_does_not_drop_presentation(capture):
    import openai
    from openai._models import FinalRequestOptions

    events, _ = capture
    context = trace.SpanContext(
        trace_id=123, span_id=456, is_remote=False, trace_flags=trace.TraceFlags(0)
    )
    with openai.OpenAI(api_key="unused-local-test") as client:
        with trace.use_span(trace.NonRecordingSpan(context)):
            request = client._build_request(
                FinalRequestOptions.construct(
                    method="post",
                    url="/responses",
                    json_data={"model": "m", "tools": [tool()]},
                )
            )
    [event] = events
    assert json.loads(event["capability.content"]) == {"tools": [tool()]}
    assert json.loads(request.content)["tools"] == [tool()]
    assert event["span.id"] == f"{context.span_id:016x}"
    assert "llm.call.id" not in event


def test_legacy_sdk_does_not_break_other_instrumentation(monkeypatch, caplog):
    import openai

    monkeypatch.delattr(openai, "OpenAI")
    instrument_openai_tool_presentations()
    assert "requires openai >= 1.0" in caplog.text


def test_openai_initialization_installs_capture_even_when_already_instrumented(
    monkeypatch,
):
    from ioa_observe.sdk.tracing.tracing import init_openai_instrumentor

    installed = []
    monkeypatch.setattr(
        "ioa_observe.sdk.instrumentations.openai.instrument_openai_tool_presentations",
        lambda: installed.append(True),
    )
    assert init_openai_instrumentor(False, lambda *args: "") is True
    assert installed == [True]


def test_capability_is_readable_without_creating_execution_state(capture):
    from ioa_observe.materializer import (
        ClickHouseRuntimeEventSource,
        SessionStateMaterializer,
    )
    from ioa_observe.sdk.tracing.capability_capture import record_capability_event

    events, _ = capture
    record_capability_event(
        "llm.tools.presented", {"tools": []}, source="llm-presented"
    )
    source = ClickHouseRuntimeEventSource("http://unused", username="test", password="")
    assert "'llm.tools.presented'" in source._build_query(
        since_timestamp=None, service_name=None, limit=10
    )
    record = source._row_to_event({"LogAttributes": events[0]})
    assert record.session_id is None
    materializer = SessionStateMaterializer()
    assert materializer.apply_events([record]) == []
    assert materializer.list_snapshots() == []
