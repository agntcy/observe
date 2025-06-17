# SPDX-FileCopyrightText: Copyright (c) 2025 Cisco and/or its affiliates.
# SPDX-License-Identifier: Apache-2.0

import importlib.metadata
import logging
import subprocess

from packaging.requirements import Requirement

logging.basicConfig(level=logging.INFO)


def get_installed_version(package_name):
    """
    Get the installed version of a package.

    Parameters:
        package_name (str): The name of the package.

    Returns:
        str or None: The installed version if installed, else None.
    """
    try:
        return importlib.metadata.version(package_name)
    except importlib.metadata.PackageNotFoundError:
        logging.debug(f"Package '{package_name}' is not installed.")
        return None
    except Exception as e:
        logging.error(f"Error getting installed version for '{package_name}': {e}")
        return None


def upgrade_packages(requirements_file):
    """
    Upgrade packages listed in the requirements.txt.txt file to their latest versions.

    Parameters:
        requirements_file (str): Path to the requirements.txt.txt file.

    Returns:
        None
    """
    try:
        with open(requirements_file, "r") as file:
            lines = file.readlines()
    except Exception as e:
        logging.error(f"Error reading requirements.txt file: {e}")
        return

    updated_lines = []

    for line in lines:
        package_line = line.strip()

        # Ignore comments and empty lines
        if not package_line or package_line.startswith("#"):
            updated_lines.append(line.rstrip("\n"))
            continue

        try:
            req = Requirement(package_line)
        except Exception as e:
            logging.warning(f"Could not parse the line: '{package_line}'. Error: {e}")
            updated_lines.append(line.rstrip("\n"))
            continue

        package_name = req.name
        extras = req.extras  # Set of extras
        specifier = req.specifier  # SpecifierSet
        markers = req.marker
        url = req.url  # For VCS or URL requirements.txt

        # Skip if it's a URL or VCS requirement
        if url:
            updated_lines.append(line.rstrip("\n"))
            continue

        # Build the package name with extras for installation
        package_with_extras = package_name
        if extras:
            extras_str = "[" + ",".join(extras) + "]"
            package_with_extras += extras_str

        logging.info(f"Upgrading package '{package_with_extras}'...")

        try:
            # Upgrade the package to the latest version
            subprocess.run(
                ["pip", "install", "--upgrade", package_with_extras], check=True
            )
        except subprocess.CalledProcessError as e:
            logging.error(f"Error upgrading package '{package_with_extras}': {e}")
            updated_lines.append(line.rstrip("\n"))
            continue

        # Get the latest installed version
        new_version = get_installed_version(package_name)
        if new_version:
            # Reconstruct the requirement line with the new version
            updated_req_str = package_name
            if extras:
                updated_req_str += "[" + ",".join(extras) + "]"
            updated_req_str += f"=={new_version}"
            if markers:
                updated_req_str += f"; {markers}"
            updated_lines.append(updated_req_str)
        else:
            logging.warning(
                f"Could not determine the installed version of '{package_name}'. Keeping the original line."
            )
            updated_lines.append(line.rstrip("\n"))

    # Write the updated requirements.txt to the file
    try:
        with open(requirements_file, "w") as file:
            file.write("\n".join(updated_lines) + "\n")
    except Exception as e:
        logging.error(f"Error writing to requirements.txt file: {e}")
        return

    logging.info(
        "All packages have been upgraded and requirements.txt.txt has been updated."
    )


if __name__ == "__main__":
    upgrade_packages("requirements.txt")
