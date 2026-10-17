import tomllib
import unittest
from pathlib import Path


class RailwayConfigTests(unittest.TestCase):
    def test_rollout_uses_deep_readiness_gate(self):
        config = tomllib.loads(Path("railway.toml").read_text())
        deploy = config["deploy"]
        self.assertEqual(deploy["healthcheckPath"], "/health/ready")
        self.assertEqual(deploy["healthcheckTimeout"], 300)


if __name__ == "__main__":
    unittest.main()
