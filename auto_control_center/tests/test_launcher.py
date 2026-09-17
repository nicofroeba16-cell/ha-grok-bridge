import os
import unittest
from unittest.mock import patch

from auto_control_center.__main__ import main


class LauncherTests(unittest.TestCase):
    @patch("auto_control_center.__main__.uvicorn.run")
    def test_launcher_is_fixed_to_loopback(self, run):
        with patch.dict(os.environ, {"ACC_PORT": "18877"}, clear=False):
            main()
        run.assert_called_once_with(
            "auto_control_center.app:app",
            host="127.0.0.1",
            port=18877,
            reload=False,
        )

    @patch("auto_control_center.__main__.uvicorn.run")
    def test_invalid_port_fails_before_server_start(self, run):
        with patch.dict(os.environ, {"ACC_PORT": "70000"}, clear=False):
            with self.assertRaises(SystemExit):
                main()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
