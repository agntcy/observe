# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import unittest
from ioa_observe.sdk import version


class TestVersion(unittest.TestCase):
    def test_version_string(self):
        """Test that the version string is properly formatted."""
        self.assertIsInstance(version.__version__, str)
        self.assertRegex(version.__version__, r"^\d+\.\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
