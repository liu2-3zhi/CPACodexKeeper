import pathlib
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class DockerComposeTests(unittest.TestCase):
    def test_compose_uses_env_file_for_runtime_toggles(self):
        compose_text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("env_file:", compose_text)
        self.assertIn("- .env", compose_text)
        self.assertNotIn("environment:", compose_text)
