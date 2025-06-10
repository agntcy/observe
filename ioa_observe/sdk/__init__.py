# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import os
import sys
from pathlib import Path

from typing import Optional, Set
from colorama import Fore
from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.sdk.metrics.export import MetricExporter
from opentelemetry.sdk._logs.export import LogExporter
from opentelemetry.sdk.resources import SERVICE_NAME
from opentelemetry.propagators.textmap import TextMapPropagator
from opentelemetry.util.re import parse_env_headers

from ioa_observe.sdk.metrics.metrics import MetricsWrapper, init_metrics_exporter
from ioa_observe.sdk.logging.logging import LoggerWrapper
from ioa_observe.sdk.telemetry import Telemetry
from ioa_observe.sdk.instruments import Instruments
from ioa_observe.sdk.config import (
    is_content_tracing_enabled,
    is_tracing_enabled,
    is_metrics_enabled,
    is_logging_enabled,
)
from ioa_observe.sdk.tracing.tracing import (
    TracerWrapper,
    set_association_properties,
    set_external_prompt_tracing_context,
    #    init_spans_exporter,
)
from typing import Dict
from ioa_observe.sdk.client.client import Client
import logging


class Observe:
    AUTO_CREATED_KEY_PATH = str(Path.home() / ".cache" / "observe" / "auto_created_key")
    AUTO_CREATED_URL = str(Path.home() / ".cache" / "observe" / "auto_created_url")

    __tracer_wrapper: TracerWrapper
    __app_name: Optional[str] = None
    __client: Optional[Client] = None

    @staticmethod
    def init(
        app_name: str = sys.argv[0],
        api_endpoint: str = "https://api.observe.agntcy.org",
        api_key: Optional[str] = None,
        enabled: bool = True,
        headers: Dict[str, str] = {},
        disable_batch=False,
        telemetry_enabled: bool = True,
        exporter: Optional[SpanExporter] = None,
        metrics_exporter: MetricExporter = None,
        metrics_headers: Dict[str, str] = None,
        logging_exporter: LogExporter = None,
        logging_headers: Dict[str, str] = None,
        processor: Optional[SpanProcessor] = None,
        propagator: TextMapPropagator = None,
        observe_sync_enabled: bool = False,
        should_enrich_metrics: bool = True,
        resource_attributes: dict = {},
        instruments: Optional[Set[Instruments]] = None,
        block_instruments: Optional[Set[Instruments]] = None,
        image_uploader=None,
    ) -> Optional[Client]:
        if not enabled:
            TracerWrapper.set_disabled(True)
            print(
                Fore.YELLOW
                + "Observe instrumentation is disabled via init flag"
                + Fore.RESET
            )
            return

        telemetry_enabled = (
            telemetry_enabled
            and (os.getenv("OBSERVE_TELEMETRY") or "true").lower() == "true"
        )
        if telemetry_enabled:
            Telemetry()

        api_endpoint = os.getenv("IOA_OBSERVE_BASE_URL") or api_endpoint
        api_key = os.getenv("IOA_OBSERVE") or api_key
        Observe.__app_name = app_name

        if (
            observe_sync_enabled
            and api_endpoint.find("observe.com") != -1
            and api_key
            and (exporter is None)
            and (processor is None)
        ):
            print(Fore.GREEN + "observe syncing configuration and prompts" + Fore.RESET)
        if not is_tracing_enabled():
            print(Fore.YELLOW + "Tracing is disabled" + Fore.RESET)
            return

        enable_content_tracing = is_content_tracing_enabled()

        if exporter or processor:
            print(Fore.GREEN + "observe exporting traces to a custom exporter")

        headers = os.getenv("OBSERVE_HEADERS") or headers

        if isinstance(headers, str):
            headers = parse_env_headers(headers)

        if (
            not exporter
            and not processor
            and api_endpoint == "https://api.observe.com"
            and not api_key
        ):
            print(
                Fore.RED
                + "Error: Missing observe API key,"
                + " go to https://app.observe.com/settings/api-keys to create one"
            )
            print("Set the OBSERVE_API_KEY environment variable to the key")
            print(Fore.RESET)
            return

        if not exporter and not processor and headers:
            print(
                Fore.GREEN
                + f"observe exporting traces to {api_endpoint}, authenticating with custom headers"
            )

        if api_key and not exporter and not processor and not headers:
            print(
                Fore.GREEN
                + f"observe exporting traces to {api_endpoint} authenticating with bearer token"
            )
            headers = {
                "Authorization": f"Bearer {api_key}",
            }

        print(Fore.RESET)

        import warnings

        warnings.filterwarnings("ignore")
        logging.getLogger("opentelemetry.sdk.metrics._internal.export").setLevel(
            logging.ERROR
        )
        # Set the log level for OpenTelemetry context module to ERROR or higher
        logging.getLogger("opentelemetry.context").setLevel(logging.CRITICAL)
        # For the SDK warnings
        logging.getLogger("opentelemetry.sdk.trace").setLevel(logging.ERROR)

        # Tracer init
        resource_attributes.update({SERVICE_NAME: app_name})
        TracerWrapper.set_static_params(
            resource_attributes, enable_content_tracing, api_endpoint, headers
        )
        Observe.__tracer_wrapper = TracerWrapper(
            disable_batch=disable_batch,
            processor=processor,
            propagator=propagator,
            exporter=exporter,
            should_enrich_metrics=should_enrich_metrics,
            image_uploader=image_uploader,
            instruments=instruments,
            block_instruments=block_instruments,
        )

        if not is_metrics_enabled() or not metrics_exporter and exporter:
            print(Fore.YELLOW + "Metrics are disabled" + Fore.RESET)
        else:
            metrics_endpoint = os.getenv("OBSERVE_METRICS_ENDPOINT") or api_endpoint
            metrics_headers = (
                os.getenv("OBSERVE_METRICS_HEADERS") or metrics_headers or headers
            )
            if metrics_exporter or processor:
                print(Fore.GREEN + "observe exporting metrics to a custom exporter")

            MetricsWrapper.set_static_params(
                resource_attributes, metrics_endpoint, metrics_headers
            )
            Observe.__metrics_wrapper = MetricsWrapper(
                exporter=exporter
                if exporter
                else init_metrics_exporter(
                    TracerWrapper.endpoint, TracerWrapper.headers
                )
            )

        if is_logging_enabled() and (logging_exporter or not exporter):
            logging_endpoint = os.getenv("OBSERVE_LOGGING_ENDPOINT") or api_endpoint
            logging_headers = (
                os.getenv("OBSERVE_LOGGING_HEADERS") or logging_headers or headers
            )
            if logging_exporter or processor:
                print(Fore.GREEN + "observe exporting logs to a custom exporter")

            LoggerWrapper.set_static_params(
                resource_attributes, logging_endpoint, logging_headers
            )
            Observe.__logger_wrapper = LoggerWrapper(exporter=logging_exporter)

        if not api_key:
            return
        Observe.__client = Client(
            api_key=api_key, app_name=app_name, api_endpoint=api_endpoint
        )
        return Observe.__client

    def set_association_properties(properties: dict) -> None:
        set_association_properties(properties)

    def set_prompt(template: str, variables: dict, version: int):
        set_external_prompt_tracing_context(template, variables, version)

    @staticmethod
    def get():
        """
        Returns the shared SDK client instance, using the current global configuration.

        To use the SDK as a singleton, first make sure you have called :func:`observe.init()`
        at startup time. Then ``get()`` will return the same shared :class:`observe.client.Client`
        instance each time. The client will be initialized if it has not been already.

        If you need to create multiple client instances with different configurations, instead of this
        singleton approach you can call the :class:`observe.client.Client` constructor directly instead.
        """
        if not Observe.__client:
            raise Exception(
                "Client not initialized, you should call observe.init() first. "
                "If you are still getting this error - you are missing the api key"
            )
        return Observe.__client
