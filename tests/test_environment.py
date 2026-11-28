import os
import unittest
from pathlib import Path

import yaml


class ContainerEnvironmentTests(unittest.TestCase):
    def test_hf_token_is_well_formed_when_present(self) -> None:
        token = os.environ.get("HF_TOKEN")

        if token is None:
            self.assertNotIn("HF_TOKEN", os.environ)
            return

        self.assertNotEqual(token, "", "HF_TOKEN must not be empty when present")
        self.assertFalse(
            token.startswith("HF_TOKEN="),
            "HF_TOKEN must contain only the token value",
        )
        self.assertEqual(token, token.strip())

    def test_compose_marks_private_txt_optional(self) -> None:
        compose_path = Path("/app/docker-compose.yml")
        with compose_path.open(encoding="utf-8") as handle:
            config = yaml.safe_load(handle)

        for service in ("api", "tests", "dev"):
            entries = config["services"][service]["env_file"]
            matches = [
                entry
                for entry in entries
                if entry.get("path") == "./private.txt"
            ]
            self.assertEqual(len(matches), 1, service)
            self.assertEqual(entries, matches, service)
            self.assertIs(matches[0].get("required"), False, service)
            self.assertNotIn(
                "HF_TOKEN",
                config["services"][service].get("environment", {}),
                service,
            )


if __name__ == "__main__":
    unittest.main()
