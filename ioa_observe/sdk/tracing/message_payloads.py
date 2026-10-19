# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import json
import logging

from ioa_observe.sdk.config import is_content_tracing_enabled


def serialize_message_payload(value, *, protocol: str) -> str | None:
    if value is None or not is_content_tracing_enabled():
        return None

    def encode(item):
        if hasattr(item, "DESCRIPTOR"):
            from google.protobuf.json_format import MessageToDict

            return MessageToDict(item)
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json", by_alias=True)
        if hasattr(item, "__dict__"):
            return vars(item)
        raise TypeError(f"Unsupported {protocol} payload type: {type(item).__name__}")

    try:
        return json.dumps(value, default=encode, sort_keys=True)
    except (TypeError, ValueError):
        logging.warning(
            "Unable to serialize %s message payload", protocol, exc_info=True
        )
        return None
