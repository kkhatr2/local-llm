import json
import re
import unittest
from pathlib import Path
from typing import Any


def _load_jsonc(path: str) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    text = re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    loaded = json.loads(text)
    if not isinstance(loaded, dict):
        raise ValueError("opencode config must be an object")
    return loaded


class OpenCodeConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = _load_jsonc("opencode.jsonc")
        cls.providers = cls.config["providers"]

    def _local_providers(self) -> dict[str, Any]:
        return {
            name: provider
            for name, provider in self.providers.items()
            if name.startswith("local-")
        }

    def test_default_model_is_free_tier_opencode(self) -> None:
        self.assertEqual(self.config["model"], "opencode/big-pickle")

    def test_local_providers_use_supported_v2_fields(self) -> None:
        local = self._local_providers()
        self.assertTrue(local)

        for name, provider in local.items():
            model = provider["models"]["chat"]
            self.assertNotIn("limits", model, name)
            self.assertNotIn("options", model, name)
            self.assertIn("limit", model, name)
            self.assertIn("body", model, name)
            self.assertIn("options", model["body"], name)

    def test_local_providers_cap_output_at_96_tokens(self) -> None:
        expected = {
            "local-phi4",
            "local-qwen-coder",
            "local-qwen-coder-05b",
        }
        local = self._local_providers()

        self.assertEqual(set(local), expected)
        for name, provider in local.items():
            model = provider["models"]["chat"]
            self.assertEqual(model["limit"]["output"], 96, name)
            self.assertEqual(model["body"]["options"]["max_tokens"], 96, name)


if __name__ == "__main__":
    unittest.main()
