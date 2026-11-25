import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_llm.cache import CacheConfig, ResponseCache
from local_llm.composition import Services, create_app
from local_llm.config import ModelRecord, Settings
from local_llm.preflight import ResourceError
from local_llm.registry import ModelRegistry, ModelState


class _FakeDownloader:
    def __init__(self) -> None:
        self.calls = []

    def exists(self, record, root) -> bool:
        path = Path(root) / "models" / record.alias / record.filename
        return path.is_file() and path.stat().st_size > 0

    def download(self, record, root) -> Path:
        self.calls.append(record.model_dump(mode="json"))
        path = Path(root) / "models" / record.alias / record.filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-gguf")
        return path


class _FakePreflight:
    def __init__(self) -> None:
        self.download_calls = []
        self.fail_download = False

    def check_download(self, destination) -> None:
        self.download_calls.append(Path(destination))
        if self.fail_download:
            raise ResourceError("insufficient disk space")


class _FakeLock:
    def __init__(self) -> None:
        self.available = True
        self.acquire_calls = 0
        self.release_calls = 0

    def acquire(self, timeout=None) -> bool:
        self.acquire_calls += 1
        return self.available

    def release(self) -> None:
        self.release_calls += 1


class _RuntimeStub:
    def __init__(self) -> None:
        self.loaded = False
        self.unload_calls = 0

    def is_loaded(self) -> bool:
        return self.loaded

    def unload(self) -> None:
        self.loaded = False
        self.unload_calls += 1

    def load(self, model_path, options) -> None:
        self.loaded = True

    def chat(self, request):
        return {
            "id": "chatcmpl-admin",
            "object": "chat.completion",
            "created": 1,
            "model": "alias-a",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
        }


class AdminRoutesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        existing = ModelRecord(
            alias="alias-a",
            repo_id="owner/model-a",
            filename="model-a.gguf",
        )
        self.settings = Settings.model_validate(
            {
                "llama_defaults": {
                    "n_ctx": 16384,
                    "n_batch": 512,
                    "flash_attn": True,
                    "n_threads": 1,
                },
                "models": [existing.model_dump()],
            }
        )
        self.registry = ModelRegistry(self.settings.models)
        self.downloader = _FakeDownloader()
        self.preflight = _FakePreflight()
        self.runtime = _RuntimeStub()
        self.lock = _FakeLock()
        self.services = Services(
            settings=self.settings,
            root=self.root,
            registry=self.registry,
            cache=ResponseCache(CacheConfig(dir=".cache/responses"), root=self.root),
            downloader=self.downloader,
            preflight=self.preflight,
            runtime=self.runtime,
            lock=self.lock,
        )
        path = self.downloader.download(existing, self.root)
        self.registry.mark_downloaded(existing.alias, path)
        self.downloader.calls.clear()
        self.client = TestClient(create_app(self.services, include_admin=True))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_add_downloads_and_registers_new_alias(self) -> None:
        payload = {
            "alias": "alias-b",
            "repo_id": "owner/model-b",
            "filename": "model-b.gguf",
            "options": {"n_ctx": 8192, "temperature": 0.5},
        }

        response = self.client.post("/admin/models", json=payload)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["alias"], "alias-b")
        self.assertEqual(self.downloader.calls[0]["alias"], "alias-b")
        self.assertEqual(len(self.preflight.download_calls), 1)
        self.assertEqual(
            [record.alias for record in self.registry.list()],
            ["alias-a", "alias-b"],
        )
        self.assertEqual(
            self.registry.get("alias-b").state, ModelState.DOWNLOADED
        )
        self.assertTrue(
            (self.root / "models/alias-b/model-b.gguf").is_file()
        )
        self.assertNotIn("path", response.json())

    def test_add_duplicate_alias_returns_409_without_download(self) -> None:
        payload = {
            "alias": "alias-a",
            "repo_id": "owner/other",
            "filename": "other.gguf",
        }

        response = self.client.post("/admin/models", json=payload)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "conflict")
        self.assertEqual(self.downloader.calls, [])

    def test_add_with_insufficient_disk_returns_503_and_writes_no_file(self) -> None:
        self.preflight.fail_download = True
        payload = {
            "alias": "alias-b",
            "repo_id": "owner/model-b",
            "filename": "model-b.gguf",
        }

        response = self.client.post("/admin/models", json=payload)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["error"]["code"], "resource_unavailable"
        )
        self.assertEqual(self.downloader.calls, [])
        self.assertFalse((self.root / "models/alias-b/model-b.gguf").exists())
        self.assertEqual(len(self.registry.list()), 1)

    def test_add_invalid_filename_returns_stable_422(self) -> None:
        payload = {
            "alias": "alias-b",
            "repo_id": "owner/model-b",
            "filename": "model-b.bin",
        }

        response = self.client.post("/admin/models", json=payload)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "validation_error")

    def test_unload_loaded_alias_clears_runtime_and_state(self) -> None:
        self.runtime.loaded = True
        self.registry.set_state("alias-a", ModelState.LOADED)

        response = self.client.post("/admin/models/alias-a/unload")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"alias": "alias-a", "unloaded": True})
        self.assertEqual(self.runtime.unload_calls, 1)
        self.assertEqual(self.lock.acquire_calls, 1)
        self.assertEqual(self.lock.release_calls, 1)
        self.assertFalse(self.runtime.loaded)
        self.assertEqual(
            self.registry.get("alias-a").state, ModelState.UNLOADED
        )

    def test_unload_not_loaded_alias_is_noop(self) -> None:
        response = self.client.post("/admin/models/alias-a/unload")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"alias": "alias-a", "unloaded": False})
        self.assertEqual(self.runtime.unload_calls, 0)
        self.assertEqual(self.lock.acquire_calls, 1)
        self.assertEqual(self.lock.release_calls, 1)
        self.assertEqual(
            self.registry.get("alias-a").state, ModelState.DOWNLOADED
        )

    def test_unload_unknown_alias_returns_404(self) -> None:
        response = self.client.post("/admin/models/missing/unload")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    def test_delete_unregisters_alias_and_chat_then_returns_404(self) -> None:
        delete_response = self.client.delete("/admin/models/alias-a")

        self.assertEqual(delete_response.status_code, 204)
        self.assertEqual(delete_response.content, b"")
        self.assertEqual(self.registry.list(), [])
        chat_response = self.client.post(
            "/v1/models/alias-a/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
        self.assertEqual(chat_response.status_code, 404)

    def test_delete_unknown_alias_returns_404(self) -> None:
        response = self.client.delete("/admin/models/missing")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    def test_delete_does_not_unload_runtime(self) -> None:
        self.runtime.loaded = True
        self.registry.set_state("alias-a", ModelState.LOADED)

        response = self.client.delete("/admin/models/alias-a")

        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.runtime.unload_calls, 0)
        self.assertTrue(self.runtime.loaded)


if __name__ == "__main__":
    unittest.main()
