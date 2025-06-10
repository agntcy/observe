# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import inspect
import string
import time
from functools import wraps
from json import JSONEncoder
from typing import Optional

from opentelemetry.metrics import get_meter
from opentelemetry import trace

from ioa_observe.sdk import TracerWrapper
from ioa_observe.sdk.decorators.base import (
    _setup_span,
    _is_async_method,
    _ahandle_generator,
    _handle_span_input,
    _cleanup_span,
)
from ioa_observe.sdk.tracing import get_tracer
# from opentelemetry import context as context_api


class SLIMConnector:
    """
    SLIM Connector for connecting to remote organizations and namespaces.
    """

    def __init__(
        self, remote_org: string, remote_namespace: string, shared_space: string
    ):
        self.remote_org = remote_org
        self.remote_namespace = remote_namespace
        self.shared_space = shared_space
        self.meter = get_meter("observe.metrics")

    def register(self, agent_name: str):
        """
        Register the slim client connector with the SDK.
        """
        with get_tracer() as tracer:
            span = tracer.start_span("slim_connector_register")
            # ctx = trace.set_span_in_context(span)
            # ctx_token = context_api.attach(ctx)
            with trace.get_tracer(__name__).start_span(
                "slim_connector_register_event", context=trace.set_span_in_context(span)
            ) as slim_span:
                slim_span.add_event(
                    agent_name,
                    {
                        "agent_name": agent_name,
                        "remote_org": self.remote_org,
                        "remote_namespace": self.remote_namespace,
                        "shared_space": self.shared_space,
                    },
                )
            TracerWrapper().active_connections_counter.add(1, {"agent": agent_name})

    def disconnect(self, agent_name: str):
        """
        Disconnect from the slim connector.
        """
        # Implement disconnection logic here
        TracerWrapper().active_connections_counter.add(-1, {"agent": agent_name})


def process_slim_msg(name: Optional[str] = None):
    def decorate(fn):
        is_async = _is_async_method(fn)
        entity_name = name or fn.__name__
        if is_async:
            if inspect.isasyncgenfunction(fn):

                @wraps(fn)
                async def async_gen_wrap(*args, **kwargs):
                    if not TracerWrapper.verify_initialized():
                        async for item in fn(*args, **kwargs):
                            yield item
                        return
                    span, ctx, ctx_token = _setup_span(entity_name)
                    _handle_span_input(span, args, kwargs, cls=JSONEncoder)

                    start_time = time.time()

                    try:
                        async for item in _ahandle_generator(
                            span, ctx_token, fn(*args, **kwargs)
                        ):
                            # Measure throughput and processing time per item
                            item_process_time = time.time() - start_time
                            TracerWrapper().processing_time.record(
                                item_process_time, {"agent": entity_name}
                            )
                            TracerWrapper().throughput_counter.add(
                                1, {"agent": entity_name}
                            )

                            # Reset timer for next item
                            start_time = time.time()
                            # Count each yielded item as a published message
                            TracerWrapper().messages_received_counter.add(
                                1, {"agent": entity_name}
                            )
                            TracerWrapper().processed_messages_counter.add(
                                1, {"agent": entity_name}
                            )
                            yield item
                    finally:
                        _cleanup_span(span, ctx_token)
                        # Decrement active connections when done
                        # TracerWrapper().active_connections_counter.add(-1, {"agent": entity_name})

                return async_gen_wrap
            else:

                @wraps(fn)
                async def async_wrap(*args, **kwargs):
                    if not TracerWrapper.verify_initialized():
                        return await fn(*args, **kwargs)

                    # span, ctx, ctx_token = _setup_span(entity_name)
                    # _handle_span_input(span, args, kwargs, cls=JSONEncoder)
                    start_time = time.time()

                    try:
                        res = await fn(*args, **kwargs)

                        # Measure processing time
                        process_time = time.time() - start_time
                        TracerWrapper().processing_time.record(
                            process_time, {"agent": entity_name}
                        )
                        TracerWrapper().throughput_counter.add(
                            1, {"agent": entity_name}
                        )

                        # span will be ended in the generator
                        # if isinstance(res, types.GeneratorType):
                        #     return _handle_generator(span, res)
                        #
                        # _handle_span_output(span, "slim", res, cls=JSONEncoder)
                        # Count published message on success
                        TracerWrapper().messages_received_counter.add(
                            1, {"agent": entity_name}
                        )
                        TracerWrapper().processed_messages_counter.add(
                            1, {"agent": entity_name}
                        )
                    except Exception as e:
                        # span.record_exception(e)
                        # span.set_status(
                        #     trace.Status(trace.StatusCode.ERROR, str(e))
                        # )
                        raise e
                    # finally:
                    #     # _cleanup_span(span, ctx_token)
                    #     # Decrement active connections when done
                    #     # TracerWrapper().active_connections_counter.add(-1, {"agent": entity_name})
                    return res

                return async_wrap

        else:

            @wraps(fn)
            def sync_wrap(*args, **kwargs):
                if not TracerWrapper.verify_initialized():
                    return fn(*args, **kwargs)

                # span, ctx, ctx_token = _setup_span(entity_name)
                # _handle_span_input(span, args, kwargs, cls=JSONEncoder)

                start_time = time.time()

                try:
                    res = fn(*args, **kwargs)

                    # Measure processing time
                    process_time = time.time() - start_time
                    TracerWrapper().processing_time.record(
                        process_time, {"agent": entity_name}
                    )
                    TracerWrapper().throughput_counter.add(1, {"agent": entity_name})
                    # span will be ended in the generator
                    # if isinstance(res, types.GeneratorType):
                    #     return _handle_generator(span, res)
                    # _handle_span_output(span, "slim", res, cls=JSONEncoder)
                    # Count published message on success
                    TracerWrapper().messages_received_counter.add(
                        1, {"agent": entity_name}
                    )
                    TracerWrapper().processed_messages_counter.add(
                        1, {"agent": entity_name}
                    )
                except Exception as e:
                    # span.record_exception(e)
                    # span.set_status(
                    #     trace.Status(trace.StatusCode.ERROR, str(e))
                    # )
                    raise e
                # finally:
                #     # _cleanup_span(span, ctx_token)
                #     # Decrement active connections when done
                #     # TracerWrapper().active_connections_counter.add(-1, {"agent": entity_name})
                return res

            return sync_wrap

    return decorate
