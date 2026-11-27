import unittest

from local_llm.config import ConfigLoader, Settings

VALID_YAML = """
llama_defaults:
  n_ctx: 16384
  n_batch: 512
  flash_attn: true
  n_threads: 8
max_n_ctx: 32768
models:
  - alias: alpha
    repo_id: owner/name
    filename: model.gguf
"""

DUPLICATE_YAML = VALID_YAML + """  - alias: alpha
    repo_id: owner/name
    filename: other.gguf
"""


class ConfigLoaderTests(unittest.TestCase):
    def test_real_config_loads(self) -> None:
        settings = ConfigLoader().load()

        self.assertIsInstance(settings, Settings)
        self.assertEqual(len(settings.models), 3)
        aliases = [model.alias for model in settings.models]
        self.assertIn("phi4-mini", aliases)
        self.assertIn("qwen25-coder-3b", aliases)
        self.assertIn("qwen25-coder-05b", aliases)
        self.assertEqual(settings.llama_defaults.n_ctx, 16384)
        self.assertEqual(settings.llama_defaults.n_batch, 512)
        self.assertTrue(settings.llama_defaults.flash_attn)
        self.assertTrue(settings.cache_config.enabled)
        self.assertEqual(settings.server.lock_timeout_seconds, 600)

    def test_env_override_and_explicit_path(self) -> None:
        seen: list[str] = []

        def reader(path: str) -> str:
            seen.append(path)
            return VALID_YAML

        loader = ConfigLoader(env={"LOCAL_LLM_CONFIG": "custom.yaml"}, reader=reader)
        loader.load()
        loader.load("explicit.yaml")

        self.assertEqual(seen, ["custom.yaml", "explicit.yaml"])

    def test_duplicate_alias_rejected(self) -> None:
        with self.assertRaises(ValueError) as context:
            ConfigLoader(env={}, reader=lambda _: DUPLICATE_YAML).load()

        self.assertIn("alpha", str(context.exception))

    def test_invalid_alias_rejected_with_field_name(self) -> None:
        bad = VALID_YAML.replace("alias: alpha", "alias: bad.alias", 1)

        with self.assertRaises(ValueError) as context:
            ConfigLoader(env={}, reader=lambda _: bad).load()

        self.assertIn("alias", str(context.exception).lower())

    def test_non_gguf_filename_rejected_with_field_name(self) -> None:
        bad = VALID_YAML.replace("filename: model.gguf", "filename: model.bin", 1)

        with self.assertRaises(ValueError) as context:
            ConfigLoader(env={}, reader=lambda _: bad).load()

        message = str(context.exception).lower()
        self.assertTrue("filename" in message or "gguf" in message)

    def test_invalid_n_ctx_rejected(self) -> None:
        bad = VALID_YAML.replace("n_ctx: 16384", "n_ctx: 999999", 1)

        with self.assertRaises(ValueError) as context:
            ConfigLoader(env={}, reader=lambda _: bad).load()

        self.assertIn("n_ctx", str(context.exception))

    def test_invalid_lock_timeout_rejected_with_field_name(self) -> None:
        bad = VALID_YAML.replace(
            "llama_defaults:\n",
            "server:\n  lock_timeout_seconds: 0\nllama_defaults:\n",
            1,
        )

        with self.assertRaises(ValueError) as context:
            ConfigLoader(env={}, reader=lambda _: bad).load()

        self.assertIn("lock_timeout_seconds", str(context.exception))

    def test_missing_required_field_names_field(self) -> None:
        bad = VALID_YAML.replace("    repo_id: owner/name\n", "", 1)

        with self.assertRaises(ValueError) as context:
            ConfigLoader(env={}, reader=lambda _: bad).load()

        self.assertIn("repo_id", str(context.exception))


if __name__ == "__main__":
    unittest.main()
