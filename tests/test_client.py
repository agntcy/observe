# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import pytest
from ioa_observe.sdk.client import Client, KVStore, kv_store


def client_initializes_correctly_with_valid_api_key():
    client = Client(
        api_key="valid_api_key",
        app_name="TestApp",
        api_endpoint="https://custom.endpoint.com",
    )
    assert client.api_key == "valid_api_key"
    assert client.app_name == "TestApp"
    assert client.api_endpoint == "https://custom.endpoint.com"


def client_raises_error_with_empty_api_key():
    with pytest.raises(ValueError, match="API key is required"):
        Client(api_key="")


def kv_store_singleton_behavior_is_consistent():
    store1 = KVStore()
    store2 = KVStore()
    assert store1 is store2


def kv_store_sets_and_retrieves_values_correctly():
    kv_store.set("key1", "value1")
    assert kv_store.get("key1") == "value1"


def kv_store_returns_none_for_nonexistent_key():
    assert kv_store.get("nonexistent_key") is None
