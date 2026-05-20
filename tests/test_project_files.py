import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class ProjectFileTests(unittest.TestCase):
    def test_docker_compose_mounts_state_directory(self):
        content = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("- ./state:/app/state", content)
        self.assertNotIn("./disabled_accounts.json.lock:/app/disabled_accounts.json.lock", content)
        self.assertNotIn("./disabled_accounts.json:/app/disabled_accounts.json", content)
        self.assertNotIn("./delete_blocked_accounts.json:/app/delete_blocked_accounts.json", content)

    def test_gitignore_ignores_state_directory(self):
        content = (ROOT / ".gitignore").read_text(encoding="utf-8")

        self.assertIn("state/", content)

    def test_env_example_no_longer_mentions_cpa_usage_query_interval(self):
        content = (ROOT / ".env.example").read_text(encoding="utf-8")

        self.assertNotIn("CPA_USAGE_QUERY_INTERVAL", content)
        self.assertIn("CPA_FILL_INTERVAL", content)
        self.assertIn("CPA_QUOTA_THRESHOLD", content)
        self.assertIn("CPA_QUOTA_RESET_NONE_RECHECK_SECONDS", content)
