"""Tests for constellation-health instinct."""

import importlib.util
import json
import sys
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock

# Load the instinct script dynamically
INSTINCT_DIR = Path(__file__).parent.parent / "instincts" / "constellation-health"
spec = importlib.util.spec_from_file_location("check", INSTINCT_DIR / "check.py")
check_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_mod)


class TestConstellationHealth(unittest.TestCase):
    """Test the constellation health check instinct."""

    def test_agents_defined(self):
        """All 5 constellation agents should be defined."""
        self.assertEqual(len(check_mod.AGENTS), 5)
        for name in ["Iris", "Vera", "Sage", "Mira", "Lyra"]:
            self.assertIn(name, check_mod.AGENTS)

    def test_port_allocation_no_conflicts(self):
        """No two agents should share a port, and canvas ports (port+2) shouldn't conflict."""
        ports = set()
        for name, info in check_mod.AGENTS.items():
            gw = info["port"]
            canvas = gw + 2
            self.assertNotIn(gw, ports, f"{name} gateway port {gw} conflicts")
            self.assertNotIn(canvas, ports, f"{name} canvas port {canvas} conflicts")
            ports.add(gw)
            ports.add(canvas)

    def test_port_values_match_allocation(self):
        """Ports should match the locked allocation from March 9."""
        expected = {"Iris": 18789, "Vera": 18790, "Sage": 18793, "Mira": 18797, "Lyra": 18801}
        for name, port in expected.items():
            self.assertEqual(check_mod.AGENTS[name]["port"], port)

    def test_check_agent_down(self):
        """Agent on a closed port should return status=down."""
        result = check_mod.check_agent("Test", 19999)
        self.assertEqual(result["status"], "down")
        self.assertEqual(result["name"], "Test")

    def test_check_agent_up_mock(self):
        """Agent returning 200 on /health should be up."""
        mock_resp = MagicMock()
        mock_resp.getcode.return_value = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch.object(check_mod.urllib.request, "urlopen", return_value=mock_resp):
            result = check_mod.check_agent("TestAgent", 55555)
            self.assertEqual(result["status"], "up")
            self.assertEqual(result["http"], 200)

    def test_main_output_has_json(self):
        """main() should include a JSON summary line."""
        captured = StringIO()
        with patch("sys.stdout", captured):
            check_mod.main()
        output = captured.getvalue()
        # Find the JSON line after ---
        lines = output.split("---")
        self.assertGreaterEqual(len(lines), 2)
        summary = json.loads(lines[-1].strip())
        self.assertIn("total", summary)
        self.assertIn("up", summary)
        self.assertIn("down", summary)
        self.assertEqual(summary["total"], 5)

    def test_each_agent_has_emoji(self):
        """Each agent definition should have an emoji."""
        for name, info in check_mod.AGENTS.items():
            self.assertIn("emoji", info, f"{name} missing emoji")
            self.assertTrue(len(info["emoji"]) > 0)


if __name__ == "__main__":
    unittest.main()
