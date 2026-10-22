"""Offline checks for discovery outside an IDE and frozen startup."""
import os
from pathlib import Path
import runpy
import sys
import unittest
from unittest.mock import Mock, patch

import sr_agent as sr


class PackagingTests(unittest.TestCase):
    def test_codex_falls_back_to_user_npm_install_without_path(self):
        shim = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "npm" / "codex.cmd"
        if not shim.is_file():
            self.skipTest("This machine has no standard user npm Codex installation")
        with patch.object(sr.shutil, "which", return_value=None):
            command = sr.codex_command()
        self.assertEqual(len(command), 1)
        self.assertEqual(Path(command[0]).name, "codex.exe")
        self.assertTrue(Path(command[0]).is_absolute())
        self.assertTrue(Path(command[0]).is_file())

    def test_node_falls_back_to_standard_install_without_path(self):
        with patch.object(sr.shutil, "which", return_value=None), patch.dict(os.environ, {"SR_NODE_PATH": ""}):
            node = sr.node_executable()
        if not node:
            self.skipTest("This machine uses a nonstandard Node.js installation")
        self.assertTrue(Path(node).is_file())
        self.assertTrue(Path(node).is_absolute())

    def test_explicit_missing_binary_is_reported_not_silently_replaced(self):
        with patch.dict(os.environ, {"SR_CODEX_PATH": str(sr.PROJECT_DIR / "missing-codex.exe")}):
            with self.assertRaises(FileNotFoundError):
                sr.codex_command()
        with patch.dict(os.environ, {"SR_NODE_PATH": str(sr.PROJECT_DIR / "missing-node.exe")}):
            with self.assertRaises(FileNotFoundError):
                sr.node_executable()

    def test_missing_connector_falls_back_with_useful_error(self):
        with patch.dict(os.environ, {"SR_TRADINGVIEW_MCP_DIR": str(sr.PROJECT_DIR / "missing-connector")}), \
                patch.object(sr.subprocess, "run") as run:
            result = sr.get_tradingview_watchlist()
        self.assertEqual(result["source"], "fallback")
        self.assertTrue(result["error"])
        self.assertIn(sr.DEFAULT_SYMBOL, result["symbols"])
        run.assert_not_called()

    def test_frozen_working_directory_is_executable_directory(self):
        executable = str(sr.PROJECT_DIR / "dist" / "SupportResistanceAgent.exe")
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", executable):
            module = runpy.run_path(sr.__file__)
        self.assertEqual(module["PROJECT_DIR"], sr.PROJECT_DIR / "dist")

    def test_native_startup_does_not_change_dll_search_path(self):
        with patch.object(sys, "frozen", False, create=True), patch("ctypes.windll.kernel32.SetDllDirectoryW") as reset:
            sr.prepare_frozen_runtime()
            reset.assert_not_called()

    def test_frozen_startup_resets_dll_search_for_external_children(self):
        with patch.object(sys, "frozen", True, create=True), patch("ctypes.windll.kernel32.SetDllDirectoryW", return_value=1) as reset:
            sr.prepare_frozen_runtime()
            reset.assert_called_once_with(None)


if __name__ == "__main__":
    unittest.main()
