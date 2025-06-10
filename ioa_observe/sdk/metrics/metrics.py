# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from collections.abc import Sequence
from typing import Dict

from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter as HTTPExporter,
)
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
    OTLPMetricExporter as GRPCExporter,
)
from opentelemetry.sdk.metrics._internal.export import ConsoleMetricExporter
from opentelemetry.semconv_ai import Meters
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    PeriodicExportingMetricReader,
    MetricExporter,
)
from opentelemetry.sdk.metrics.view import View, ExplicitBucketHistogramAggregation
from opentelemetry.sdk.resources import Resource

from opentelemetry import metrics


class MetricsWrapper(object):
    resource_attributes: dict = {}
    endpoint: str = None
    # if it needs headers?
    headers: Dict[str, str] = {}
    __metrics_exporter: MetricExporter = None
    __metrics_provider: MeterProvider = None
    metrics_provider: MeterProvider = None

    def __new__(cls, exporter: MetricExporter = None) -> "MetricsWrapper":
        if not hasattr(cls, "instance"):
            obj = cls.instance = super(MetricsWrapper, cls).__new__(cls)
            if not MetricsWrapper.endpoint:
                return obj
            if exporter is None:
                exporter = ConsoleMetricExporter()
            # _meter_provider = MeterProvider()
            # meter_provider = _meter_provider
            # reader = PeriodicExportingMetricReader(exporter)
            # _meter = _meter_provider.get_meter("observe")

            obj.__metrics_exporter = (
                exporter
                if exporter
                else init_metrics_exporter(
                    MetricsWrapper.endpoint, MetricsWrapper.headers
                )
            )
            # obj.__metrics_exporter = (
            #     init_metrics_exporter(
            #         MetricsWrapper.endpoint, MetricsWrapper.headers
            #     )
            # )

            obj.__metrics_provider = init_metrics_provider(
                obj.__metrics_exporter, MetricsWrapper.resource_attributes
            )
            # metrics.set_meter_provider(obj.__metrics_provider)
            # _meter = obj.__metrics_provider.get_meter("observe")

        return cls.instance

    @staticmethod
    def set_static_params(
        resource_attributes: dict,
        endpoint: str,
        headers: Dict[str, str],
    ) -> None:
        MetricsWrapper.resource_attributes = resource_attributes
        MetricsWrapper.endpoint = endpoint
        MetricsWrapper.headers = headers


def init_metrics_exporter(endpoint: str, headers: Dict[str, str]) -> MetricExporter:
    if "http" in endpoint.lower() or "https" in endpoint.lower():
        return HTTPExporter(endpoint=f"{endpoint}/v1/metrics", headers=headers)
    else:
        return GRPCExporter(endpoint=endpoint, headers=headers, insecure=True)


def init_metrics_provider(
    exporter: MetricExporter, resource_attributes: dict = None
) -> MeterProvider:
    resource = (
        Resource.create(resource_attributes)
        if resource_attributes
        else Resource.create()
    )
    reader = PeriodicExportingMetricReader(exporter)
    provider = MeterProvider(
        metric_readers=[reader],
        resource=resource,
        views=metric_views(),
    )

    metrics.set_meter_provider(provider)
    return provider


def metric_views() -> Sequence[View]:
    return [
        View(
            instrument_name=Meters.LLM_TOKEN_USAGE,
            aggregation=ExplicitBucketHistogramAggregation(
                [
                    0.01,
                    0.02,
                    0.04,
                    0.08,
                    0.16,
                    0.32,
                    0.64,
                    1.28,
                    2.56,
                    5.12,
                    10.24,
                    20.48,
                    40.96,
                    81.92,
                ]
            ),
        ),
        View(
            instrument_name=Meters.LLM_OPERATION_DURATION,
            aggregation=ExplicitBucketHistogramAggregation(
                [
                    1,
                    4,
                    16,
                    64,
                    256,
                    1024,
                    4096,
                    16384,
                    65536,
                    262144,
                    1048576,
                    4194304,
                    16777216,
                    67108864,
                ]
            ),
        ),
        # llm-counter
        View(
            instrument_name="gen_ai.ioa.llm.counter",
            aggregation=ExplicitBucketHistogramAggregation(
                [
                    1,
                    2,
                    4,
                    8,
                    16,
                    32,
                    64,
                    128,
                    256,
                    512,
                    1024,
                    2048,
                    4096,
                    8192,
                    16384,
                    32768,
                ]
            ),
        ),
        # number-of-active-agents
        View(
            instrument_name="gen_ai.ioa.number_of_active_agents",
            aggregation=ExplicitBucketHistogramAggregation(
                [
                    1,
                    2,
                    4,
                    8,
                    16,
                    32,
                    64,
                    128,
                    256,
                    512,
                    1024,
                    2048,
                    4096,
                    8192,
                    16384,
                    32768,
                ]
            ),
        ),
        # response latency in ms
        View(
            instrument_name="gen_ai.ioa.llm.response_latency",
            aggregation=ExplicitBucketHistogramAggregation(
                [
                    0.01,
                    0.02,
                    0.04,
                    0.08,
                    0.16,
                    0.32,
                    0.64,
                    1.28,
                    2.56,
                    5.12,
                    10.24,
                    20.48,
                    40.96,
                    81.92,
                ]
            ),
        ),
    ]
