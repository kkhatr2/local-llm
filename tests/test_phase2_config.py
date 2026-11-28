import unittest
from pathlib import Path

from local_llm.config import ConfigLoader

ROOT = Path(__file__).resolve().parents[1]


def _load(text: str):
    return ConfigLoader(reader=lambda _: text).load("inline.yaml")


class Phase2ConfigTests(unittest.TestCase):
    def test_real_config_accepts_phase2_sections(self) -> None:
        settings = ConfigLoader().load(ROOT / "local_llm.yaml")
        self.assertEqual(settings.deployment.primary_target, "kubernetes")
        self.assertEqual(settings.compute.default_backend, "cpu")
        self.assertEqual(settings.registry.supply, "hf")
        self.assertEqual(settings.cache.scope, "per_instance")
        self.assertEqual(settings.security.secrets_backend, "kubernetes_secret")

    def test_unknown_active_backend_names_field(self) -> None:
        text = (ROOT / "local_llm.yaml").read_text(encoding="utf-8").replace(
            "active_backends: [cpu, cuda]", "active_backends: [cpu, metal]"
        )
        with self.assertRaises(ValueError) as caught:
            _load(text)
        self.assertIn("compute.active_backends", str(caught.exception))

    def test_unknown_supply_names_field(self) -> None:
        text = (ROOT / "local_llm.yaml").read_text(encoding="utf-8").replace(
            "supply: hf", "supply: mystery"
        )
        with self.assertRaises(ValueError) as caught:
            _load(text)
        self.assertIn("registry.supply", str(caught.exception))

    def test_non_fail_closed_fallback_names_field(self) -> None:
        text = (ROOT / "local_llm.yaml").read_text(encoding="utf-8").replace(
            "fallback: fail_closed", "fallback: allow_cpu"
        )
        with self.assertRaises(ValueError) as caught:
            _load(text)
        self.assertIn("compute.fallback", str(caught.exception))

    def test_invalid_cache_scope_names_field(self) -> None:
        text = (ROOT / "local_llm.yaml").read_text(encoding="utf-8").replace(
            "scope: per_instance", "scope: shared"
        )
        with self.assertRaises(ValueError) as caught:
            _load(text)
        self.assertIn("cache.scope", str(caught.exception))

    def test_mvp_only_config_still_validates(self) -> None:
        text = """\
server:
  host: 0.0.0.0
  port: 8000
llama_defaults:
  n_ctx: 16384
  n_batch: 512
  flash_attn: true
cache:
  enabled: true
  dir: .cache/responses
models:
  - alias: alpha
    repo_id: owner/model
    filename: model.gguf
"""
        settings = _load(text)
        self.assertEqual(settings.compute.default_backend, "cpu")
        self.assertEqual(settings.registry.supply, "hf")
        self.assertEqual(settings.cache.scope, "per_instance")
        self.assertEqual(settings.models[0].alias, "alpha")


if __name__ == "__main__":
    unittest.main()
