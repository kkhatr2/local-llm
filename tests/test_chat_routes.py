import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from local_llm.cache import CacheConfig, ResponseCache
from local_llm.composition import Services, create_app
from local_llm.config import ModelRecord, Settings
from local_llm.preflight import ResourceError
from local_llm.registry import ModelRegistry


class _FakeDownloader:
    def __init__(self) -> None:
        self.calls = []

    def exists(self, record, root) -> bool:
        path = Path(root) / "models" / record.alias / record.filename
        return path.is_file() and path.stat().st_size > 0

    def download(self, record, root) -> Path:
        self.calls.append(record.alias)
        path = Path(root) / "models" / record.alias / record.filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-gguf")
        return path


class _FakePreflight:
    def __init__(self) -> None:
        self.download_calls = []
        self.load_calls = []
        self.fail_download = False
        self.fail_load = False

    def check_download(self, destination) -> None:
        self.download_calls.append(Path(destination))
        if self.fail_download:
            raise ResourceError("insufficient disk space")

    def check_load(self, model_path) -> None:
        self.load_calls.append(Path(model_path))
        if self.fail_load:
            raise ResourceError("insufficient available memory")


class _FakeRuntime:
    def __init__(self) -> None:
        self.load_calls = []
        self.chat_calls = []
        self.unload_calls = 0
        self.loaded = False
        self.fail_chat = False
        self.content_override = None

    def load(self, model_path, options) -> None:
        self.load_calls.append((Path(model_path), deepcopy(options)))
        self.loaded = True

    def chat(self, request) -> dict:
        self.chat_calls.append(deepcopy(request))
        if self.fail_chat:
            raise RuntimeError("runtime exploded /private/path")
        temperature = request.get("temperature")
        if self.content_override is not None:
            content = self.content_override
        else:
            content = "hello" if temperature is None else f"temp={temperature}"
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1,
            "model": request.get("model") or "alias-a",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    def unload(self) -> None:
        self.unload_calls += 1
        self.loaded = False

    def is_loaded(self) -> bool:
        return self.loaded


class _FakeLock:
    def __init__(self) -> None:
        self.available = True
        self.acquire_calls = 0
        self.release_calls = 0
        self.timeouts: list[float] = []

    def acquire(self, timeout=None) -> bool:
        self.acquire_calls += 1
        self.timeouts.append(timeout)
        return self.available

    def release(self) -> None:
        self.release_calls += 1


class ChatRoutesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.records = [
            ModelRecord(
                alias="alias-a",
                repo_id="owner/model-a",
                filename="model-a.gguf",
                options={"n_ctx": 8192},
            ),
            ModelRecord(
                alias="alias-b",
                repo_id="owner/model-b",
                filename="model-b.gguf",
            ),
        ]
        self.settings = Settings.model_validate(
            {
                "llama_defaults": {
                    "n_ctx": 16384,
                    "n_batch": 512,
                    "flash_attn": True,
                    "n_threads": 1,
                },
                "models": [record.model_dump() for record in self.records],
            }
        )
        self.registry = ModelRegistry(self.settings.models)
        self.downloader = _FakeDownloader()
        self.preflight = _FakePreflight()
        self.runtime = _FakeRuntime()
        self.lock = _FakeLock()
        self.cache = ResponseCache(
            CacheConfig(dir=".cache/responses"), root=self.root
        )
        self.services = Services(
            settings=self.settings,
            root=self.root,
            registry=self.registry,
            cache=self.cache,
            downloader=self.downloader,
            preflight=self.preflight,
            runtime=self.runtime,
            lock=self.lock,
        )
        self.client = TestClient(create_app(self.services))
        self.body = {"messages": [{"role": "user", "content": "hello"}]}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _url(self, alias: str = "alias-a") -> str:
        return f"/v1/models/{alias}/chat/completions"

    def _cache_files(self) -> list[Path]:
        return sorted((self.root / ".cache/responses").glob("*.json"))

    def test_health_does_not_load_or_download(self) -> None:
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(self.runtime.load_calls, [])
        self.assertEqual(self.downloader.calls, [])

    def test_models_lists_aliases_without_paths(self) -> None:
        response = self.client.get("/v1/models")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["object"], "list")
        self.assertEqual(
            [item["id"] for item in payload["data"]], ["alias-a", "alias-b"]
        )
        self.assertTrue(all("path" not in item for item in payload["data"]))

    def test_cache_miss_returns_completion_loads_and_unloads_only_requested_alias(
        self,
    ) -> None:
        response = self.client.post(self._url(), json=self.body)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["choices"][0]["message"]["content"], "hello")
        self.assertEqual(len(self.runtime.load_calls), 1)
        self.assertEqual(self.runtime.load_calls[0][0].parent.name, "alias-a")
        self.assertEqual(self.runtime.unload_calls, 1)
        self.assertFalse(self.runtime.loaded)
        self.assertEqual(len(self.preflight.load_calls), 1)
        self.assertEqual(len(self._cache_files()), 1)

    def test_captures_model_output_for_name_prompt(self) -> None:
        prompt = "what is your name, respond using minimal tokens?"
        self.runtime.content_override = "Qwen"

        response = self.client.post(
            self._url(),
            json={"messages": [{"role": "user", "content": prompt}]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.runtime.chat_calls[0]["messages"][0]["content"], prompt
        )
        content = response.json()["choices"][0]["message"]["content"]
        self.assertEqual(content, "Qwen")
        self.assertLessEqual(len(content.split()), 3)

    def test_repeated_request_returns_cache_without_runtime(self) -> None:
        first = self.client.post(self._url(), json=self.body)

        self.preflight.load_calls.clear()
        self.preflight.download_calls.clear()
        self.downloader.calls.clear()
        self.runtime.load_calls.clear()
        self.runtime.chat_calls.clear()
        self.runtime.unload_calls = 0

        second = self.client.post(self._url(), json=self.body)

        self.assertEqual(second.json(), first.json())
        self.assertEqual(self.preflight.load_calls, [])
        self.assertEqual(self.preflight.download_calls, [])
        self.assertEqual(self.downloader.calls, [])
        self.assertEqual(self.runtime.load_calls, [])
        self.assertEqual(self.runtime.chat_calls, [])
        self.assertEqual(self.runtime.unload_calls, 0)

    def test_different_sampling_parameter_creates_different_cache_key(self) -> None:
        self.client.post(self._url(), json=self.body)
        self.runtime.load_calls.clear()
        self.runtime.chat_calls.clear()

        response = self.client.post(
            self._url(), json={**self.body, "temperature": 0.9}
        )

        self.assertEqual(
            response.json()["choices"][0]["message"]["content"], "temp=0.9"
        )
        self.assertEqual(len(self.runtime.load_calls), 1)
        self.assertEqual(self.runtime.chat_calls[0]["temperature"], 0.9)
        self.assertEqual(len(self._cache_files()), 2)

    def test_missing_file_downloads_once_then_sampling_change_is_cache_miss(
        self,
    ) -> None:
        first = self.client.post(self._url(), json=self.body)
        self.assertEqual(first.status_code, 200)

        second = self.client.post(
            self._url(), json={**self.body, "temperature": 0.9}
        )

        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            second.json()["choices"][0]["message"]["content"], "temp=0.9"
        )
        self.assertEqual(len(self.downloader.calls), 1)
        self.assertEqual(len(self.preflight.download_calls), 1)

    def test_streaming_cache_miss_emits_sse_and_unloads(self) -> None:
        response = self.client.post(
            self._url(), json={**self.body, "stream": True}
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.headers["content-type"])
        self.assertIn("chat.completion.chunk", response.text)
        self.assertIn('"content":"hello"', response.text)
        self.assertTrue(response.text.endswith("data: [DONE]\n\n"))
        self.assertEqual(self.runtime.unload_calls, 1)
        self.assertFalse(self.runtime.loaded)

    def test_non_stream_cache_hit_streams_without_loading(self) -> None:
        self.client.post(self._url(), json=self.body)
        self.runtime.load_calls.clear()
        self.runtime.chat_calls.clear()
        self.runtime.unload_calls = 0

        response = self.client.post(
            self._url(), json={**self.body, "stream": True}
        )

        self.assertTrue(response.text.endswith("data: [DONE]\n\n"))
        self.assertEqual(self.runtime.load_calls, [])
        self.assertEqual(self.runtime.chat_calls, [])
        self.assertEqual(self.runtime.unload_calls, 0)

    def test_corrupt_cache_entry_is_treated_as_miss(self) -> None:
        self.client.post(self._url(), json=self.body)
        cache_file = self._cache_files()[0]
        cache_file.write_text("not json", encoding="utf-8")
        self.runtime.load_calls.clear()
        self.runtime.chat_calls.clear()

        response = self.client.post(self._url(), json=self.body)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.runtime.load_calls), 1)
        self.assertEqual(len(self.runtime.chat_calls), 1)

    def test_preflight_failure_returns_503_without_loading(self) -> None:
        self.preflight.fail_load = True

        response = self.client.post(self._url(), json=self.body)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["error"]["code"], "resource_unavailable"
        )
        self.assertEqual(self.runtime.load_calls, [])
        self.assertEqual(self._cache_files(), [])

    def test_unknown_alias_returns_stable_404(self) -> None:
        response = self.client.post(self._url("missing"), json=self.body)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    def test_invalid_body_returns_stable_422(self) -> None:
        response = self.client.post(self._url(), json={})

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["error"],
            {
                "code": "validation_error",
                "message": "Request validation failed.",
                "type": "RequestValidationError",
            },
        )

    def test_lock_contention_returns_stable_503(self) -> None:
        self.lock.available = False

        response = self.client.post(self._url(), json=self.body)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["error"]["code"], "resource_unavailable"
        )
        self.assertEqual(self.runtime.load_calls, [])
        self.assertEqual(self.lock.release_calls, 0)
        self.assertEqual(
            self.lock.timeouts, [self.settings.server.lock_timeout_seconds]
        )

    def test_runtime_failure_returns_sanitized_502_and_unloads(self) -> None:
        self.runtime.fail_chat = True

        response = self.client.post(self._url(), json=self.body)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "runtime_error")
        self.assertNotIn("/private/path", json.dumps(response.json()))
        self.assertEqual(self.runtime.unload_calls, 1)
        self.assertFalse(self.runtime.loaded)


if __name__ == "__main__":
    unittest.main()
