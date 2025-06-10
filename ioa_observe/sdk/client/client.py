# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import sys

from .http import HTTPClient
from ioa_observe.sdk.version import __version__


class Client:
    """
    observe Client for interacting with the observe API.

    Applications should configure the client at startup time and continue to use it throughout the lifetime
    of the application, rather than creating instances on the fly. The best way to do this is with the
    singleton methods :func:`observe.init()` and :func:`observe.get()`. However, you may also call
    the constructor directly if you need to maintain multiple instances.
    """

    app_name: str
    api_endpoint: str
    api_key: str
    _http: HTTPClient
    kv_store: dict = None

    def __init__(
        self,
        api_key: str,
        app_name: str = sys.argv[0],
        api_endpoint: str = "https://api.observe.com",
    ):
        """
        Initialize a new observe client.

        Args:
            api_key (str): Your observe API key
            app_name (Optional[str], optional): The name of your application. Defaults to sys.argv[0].
            api_endpoint (Optional[str], optional): Custom API endpoint. Defaults to https://api.observe.com.
        """
        if not api_key or not api_key.strip():
            raise ValueError("API key is required")

        self.app_name = app_name
        self.api_endpoint = api_endpoint or "https://api.observe.com"
        self.api_key = api_key
        self._http = HTTPClient(
            base_url=self.api_endpoint, api_key=self.api_key, version=__version__
        )


class KVStore(object):
    """
    Key-Value Store for storing key-value pairs (Singleton).
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(KVStore, cls).__new__(cls)
            cls._instance.store = {}
        return cls._instance

    def set(self, key: str, value: str):
        self.store[key] = value

    def get(self, key: str):
        return self.store.get(key)


kv_store = KVStore()
