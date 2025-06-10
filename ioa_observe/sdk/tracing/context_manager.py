# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from contextlib import contextmanager

from ioa_observe.sdk.tracing.tracing import TracerWrapper


@contextmanager
def get_tracer(flush_on_exit: bool = False):
    wrapper = TracerWrapper()
    try:
        yield wrapper.get_tracer()
    finally:
        if flush_on_exit:
            wrapper.flush()
