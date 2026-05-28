# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import asyncio
import types
from unittest.mock import patch

from ioa_observe.sdk.instrumentations.slim import SLIMInstrumentor


def test_create_session_async_accepts_destination_kwarg():
    captured = {}

    class App:
        async def create_session_async(self, config, destination=None, *args, **kwargs):
            captured["destination"] = destination

    fake_bindings = types.SimpleNamespace(App=App)

    with patch("ioa_observe.sdk.instrumentations.slim.TracerWrapper") as tw:
        tw.return_value.get_tracer.return_value = None
        SLIMInstrumentor()._instrument_app(fake_bindings)

    asyncio.run(App.create_session_async(App(), "config", destination="dest_name"))
    assert captured["destination"] == "dest_name"


def test_create_session_and_wait_async_accepts_destination_kwarg():
    captured = {}

    class App:
        async def create_session_and_wait_async(
            self, config, destination=None, *args, **kwargs
        ):
            captured["destination"] = destination

    fake_bindings = types.SimpleNamespace(App=App)

    with patch("ioa_observe.sdk.instrumentations.slim.TracerWrapper") as tw:
        tw.return_value.get_tracer.return_value = None
        SLIMInstrumentor()._instrument_app(fake_bindings)

    asyncio.run(
        App.create_session_and_wait_async(App(), "config", destination="dest_name")
    )
    assert captured["destination"] == "dest_name"
