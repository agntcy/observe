# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from typing import Optional, Union, TypeVar, Callable, Awaitable

from typing_extensions import ParamSpec

from ioa_observe.sdk.decorators.base import (
    entity_class,
    entity_method,
)
from ioa_observe.sdk.utils.const import ObserveSpanKindValues


P = ParamSpec("P")
R = TypeVar("R")
F = TypeVar("F", bound=Callable[P, Union[R, Awaitable[R]]])

from contextlib import ContextDecorator

class DecoratorContextManager(ContextDecorator):
    def __init__(self, decorator):
        self.decorator = decorator
        self._wrapped = None

    def __call__(self, func):
        return self.decorator(func)

    def __enter__(self):
        if hasattr(self.decorator, "__enter__"):
            return self.decorator.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if hasattr(self.decorator, "__exit__"):
            return self.decorator.__exit__(exc_type, exc_val, exc_tb)
        return False


def task(
    name: Optional[str] = None,
    version: Optional[int] = None,
    method_name: Optional[str] = None,
    tlp_span_kind: Optional[ObserveSpanKindValues] = ObserveSpanKindValues.TASK,
) -> Callable[[F], F]:
    if method_name is None:
        return entity_method(name=name, version=version, tlp_span_kind=tlp_span_kind)
    else:
        return entity_class(
            name=name,
            version=version,
            method_name=method_name,
            tlp_span_kind=tlp_span_kind,
        )


def workflow(
    name: Optional[str] = None,
    version: Optional[int] = None,
    method_name: Optional[str] = None,
    tlp_span_kind: Optional[
        Union[ObserveSpanKindValues, str]
    ] = ObserveSpanKindValues.WORKFLOW,
) -> Callable[[F], F]:
    if method_name is None:
        return entity_method(name=name, version=version, tlp_span_kind=tlp_span_kind)
    else:
        return entity_class(
            name=name,
            version=version,
            method_name=method_name,
            tlp_span_kind=tlp_span_kind,
        )


def graph(
    name: Optional[str] = None,
    version: Optional[int] = None,
    method_name: Optional[str] = None,
) -> Callable[[F], F]:
    return workflow(
        name=name,
        version=version,
        method_name=method_name,
        tlp_span_kind="graph",
    )

def agent(
    name: Optional[str] = None,
    version: Optional[int] = None,
    method_name: Optional[str] = None,
) -> Callable[[F], F]:
    decorator = workflow(
        name=name,
        version=version,
        method_name=method_name,
        tlp_span_kind=ObserveSpanKindValues.AGENT,
    )
    return DecoratorContextManager(decorator)

def tool(
    name: Optional[str] = None,
    version: Optional[int] = None,
    method_name: Optional[str] = None,
) -> Callable[[F], F]:
    decorator = task(
        name=name,
        version=version,
        method_name=method_name,
        tlp_span_kind=ObserveSpanKindValues.TOOL,
    )
    return DecoratorContextManager(decorator)
