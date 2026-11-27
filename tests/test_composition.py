import tempfile
import threading
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_llm.composition import build_app
from local_llm.preflight import ResourceError
from local_llm.registry import ModelState


class _FakePreflight:
    def __init__(self) -> None:
        self.download_calls = []
        self.load_calls = []
        self.fail_download = False

    def check_download(self, destination) -> None:
        self.download_calls.append(Path(destination))
        if self.fail_download:
            raise ResourceError("insufficient disk space")

    def check_load(self, model_path) -> None:
        self.load_calls.append(Path(model_path))


class _FakeLlama:
    instances = []

    def __init__(self, model_path=None, **options) -> None:
        self.model_path = Path(model_path)
        self.load_options = dict(options)
        self.sampling_calls = []
        self.closed = False
        self.instances.append(self)

    def create_chat_completion(self, messages, stream=False, **sampling):
        self.sampling_calls.append(dict(sampling))
        return {
            "id": "chatcmpl-composed",
            "object": "chat.completion",
            "created": 1,
            "model": "alias-a",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "composed"},
                    "finish_reason": "stop",
                }
            ],
        }

    def close(self) -> None:
        self.closed = True


class CompositionRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_path = self.root / "local_llm.yaml"
        self.config_text = """\
server:
  host: 0.0.0.0
  port: 8000
llama_defaults:
  n_ctx: 16384
  n_batch: 512
  flash_attn: true
  n_threads: 1
generation_defaults:
  temperature: 0.1
  top_p: 0.2
  top_k: 20
  min_p: 0.08
  repeat_penalty: 1.1
  max_tokens: 512
cache:
  enabled: true
  dir: .cache/responses
  ttl_days: 30
  max_size_mb: 1024
  replay_sse: true
models:
  - alias: alias-a
    repo_id: owner/model-a
    filename: model-a.gguf
    options:
      n_ctx: 8192
  - alias: alias-b
    repo_id: owner/model-b
    filename: model-b.gguf
"""
        self.config_path.write_text(self.config_text, encoding="utf-8")
        self.runner_calls = []
        self.repo_info_calls = []
        self.preflight = _FakePreflight()
        _FakeLlama.instances = []

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _runner(self, command, timeout):
        self.runner_calls.append(list(command))
        filename = command[3]
        local_dir = Path(command[command.index("--local-dir") + 1])
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / filename).write_bytes(b"fake-gguf")
        return 0

    def _repo_info(self, repo_id, revision):
        self.repo_info_calls.append((repo_id, revision))
        return True

    def _build(self, **kwargs):
        defaults = {
            "root": self.root,
            "runner": self._runner,
            "repo_info": self._repo_info,
            "llama_factory": lambda model_path, **options: _FakeLlama(
                model_path=model_path, **options
            ),
            "preflight": self.preflight,
        }
        defaults.update(kwargs)
        return build_app(str(self.config_path), **defaults)

    def test_startup_registers_models_and_downloads_without_loading(self) -> None:
        app = self._build()

        with TestClient(app) as client:
            response = client.get("/v1/models")
            services = app.state.services

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["id"] for item in response.json()["data"]],
            ["alias-a", "alias-b"],
        )
        self.assertEqual(len(self.runner_calls), 2)
        self.assertEqual(len(self.repo_info_calls), 2)
        self.assertEqual(_FakeLlama.instances, [])
        for alias in ("alias-a", "alias-b"):
            self.assertEqual(
                services.registry.get(alias).state, ModelState.DOWNLOADED
            )
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), self.config_text)

    def test_startup_skips_cli_for_existing_model_file(self) -> None:
        existing = self.root / "models/alias-a/model-a.gguf"
        existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_bytes(b"already-present")

        with TestClient(self._build()) as client:
            response = client.get("/v1/models")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.runner_calls), 1)
        self.assertIn("model-b", self.runner_calls[0][2])
        self.assertEqual(len(self.preflight.download_calls), 1)

    def test_startup_downloads_run_in_parallel(self) -> None:
        barrier = threading.Barrier(2)
        commands = []
        commands_lock = threading.Lock()

        def runner(command, timeout):
            with commands_lock:
                commands.append(list(command))
            barrier.wait(timeout=10)
            filename = command[3]
            local_dir = Path(command[command.index("--local-dir") + 1])
            local_dir.mkdir(parents=True, exist_ok=True)
            (local_dir / filename).write_bytes(b"fake-gguf")
            return 0

        app = self._build(runner=runner)
        with TestClient(app) as client:
            response = client.get("/v1/models")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(commands), 2)
        self.assertEqual(
            {command[2] for command in commands},
            {"owner/model-a", "owner/model-b"},
        )

    def test_startup_preflight_failure_fails_closed(self) -> None:
        self.preflight.fail_download = True
        app = self._build()

        with self.assertRaises(ResourceError):
            with TestClient(app):
                pass

        self.assertEqual(self.runner_calls, [])
        self.assertFalse((self.root / "models/alias-a/model-a.gguf").exists())

    def test_chat_uses_injected_runtime_with_merged_options(self) -> None:
        app = self._build()
        with TestClient(app) as client:
            response = client.post(
                "/v1/models/alias-a/chat/completions",
                json={"messages": [{"role": "user", "content": "hello"}]},
            )
            services = app.state.services

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["choices"][0]["message"]["content"], "composed"
        )
        self.assertEqual(len(_FakeLlama.instances), 1)
        instance = _FakeLlama.instances[0]
        self.assertEqual(instance.model_path.parent.name, "alias-a")
        self.assertEqual(instance.load_options["n_ctx"], 8192)
        self.assertEqual(instance.load_options["n_batch"], 512)
        self.assertTrue(instance.load_options["flash_attn"])
        self.assertEqual(instance.load_options["n_threads"], 1)
        sampling = instance.sampling_calls[0]
        self.assertEqual(sampling["temperature"], 0.1)
        self.assertEqual(sampling["top_p"], 0.2)
        self.assertEqual(sampling["top_k"], 20)
        self.assertEqual(sampling["min_p"], 0.08)
        self.assertEqual(sampling["repeat_penalty"], 1.1)
        self.assertFalse(instance.closed)
        runtime = services.runtime
        self.assertTrue(runtime.is_loaded())
        self.assertEqual(runtime.loaded_alias, "alias-a")
        self.assertIsNotNone(runtime.idle_deadline)
        self.assertIsInstance(runtime.idle_deadline, float)
        self.assertGreater(runtime.idle_deadline, 0.0)
        self.assertEqual(services.registry.get("alias-a").state, ModelState.LOADED)

        services.runtime.unload()
        self.assertFalse(runtime.is_loaded())
        self.assertTrue(instance.closed)

    def test_production_app_exposes_admin_add(self) -> None:
        with TestClient(self._build()) as client:
            response = client.post(
                "/admin/models",
                json={
                    "alias": "alias-c",
                    "repo_id": "owner/model-c",
                    "filename": "model-c.gguf",
                },
            )
            models = client.get("/v1/models")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["alias"], "alias-c")
        self.assertIn("alias-c", [item["id"] for item in models.json()["data"]])
        self.assertTrue((self.root / "models/alias-c/model-c.gguf").is_file())
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), self.config_text)

    def test_api_module_exposes_app(self) -> None:
        import local_llm.api as api_module

        self.assertIsNotNone(api_module.app)
        self.assertEqual(api_module.app.state.settings.server.port, 8000)


if __name__ == "__main__":
    unittest.main()
