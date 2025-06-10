# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from importlib.metadata import Distribution, distributions


def _get_package_name(dist: Distribution) -> str | None:
    try:
        return dist.name.lower()
    except (KeyError, AttributeError):
        return None


installed_packages = {
    name for dist in distributions() if (name := _get_package_name(dist)) is not None
}


def is_package_installed(package_name: str) -> bool:
    return package_name.lower() in installed_packages
