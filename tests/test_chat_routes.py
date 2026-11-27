import json
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from local_llm.cache import CacheConfig, ResponseCache
from local_llm.chat_routes import _resolve_path
from local_llm.downloader import DownloaderError
from local_llm.composition import Services, create_app
from local_llm.config import ModelRecord, Settings
from local_llm.locking import AliasDownloadLocks
from local_llm.preflight import ResourceError
from local_llm.registry import ModelState, ModelRegistry


class _FakeDownloader:
    def __init__(self, lock=None) -> None:
        self.calls = []
        self.lock = lock
        self.lock_held_during_download: list[bool] = []
        self.fail_download = False

    def exists(self, record, root) -> bool:
        path = Path(root) / "models" / record.alias / record.filename
        return path.is_file() and path.stat().st_size > 0

    def download(self, record, root) -> Path:
        self.calls.append(record.alias)
        if self.lock is not None:
            self.lock_held_during_download.append(self.lock.held)
        if self.fail_download:
            raise DownloaderError("download failed")
        time.sleep(0.02)
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


class _FakeInstance:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeRuntime:
    def __init__(self) -> None:
        self.load_calls = []
        self.chat_calls = []
        self.unload_calls = 0
        self.fail_chat = False
        self.content_override = None
        self.idle_unload_seconds = 180.0
        self.clock_value = 0.0
        self._instance = None
        self._loaded_alias = None
        self._idle_event = None
        self._idle_deadline = None
        self._idle_callback = None
        self.cancel_idle_unload_calls = 0
        self.schedule_idle_unload_calls = 0
        self.refresh_idle_unload_calls = 0
        self.unloaded_instances = []

    @property
    def loaded_alias(self) -> str | None:
        return self._loaded_alias

    @property
    def idle_deadline(self) -> float | None:
        return self._idle_deadline

    @property
    def idle_callback(self):
        return self._idle_callback

    def _clock(self) -> float:
        return self.clock_value

    def is_loaded(self) -> bool:
        return self._instance is not None

    def cancel_idle_unload(self) -> None:
        self.cancel_idle_unload_calls += 1
        event = self._idle_event
        self._idle_event = None
        self._idle_deadline = None
        self._idle_callback = None
        if event is not None:
            event.set()

    def schedule_idle_unload(self, *, on_unload=None) -> float | None:
        if not self.is_loaded():
            return None
        self.schedule_idle_unload_calls += 1
        self._idle_event = threading.Event()
        self._idle_callback = on_unload
        self._idle_deadline = self._clock() + self.idle_unload_seconds
        return self._idle_deadline

    def refresh_idle_unload(self) -> float | None:
        if not self.is_loaded():
            return None
        callback = self._idle_callback
        if callback is None:
            return None
        self.refresh_idle_unload_calls += 1
        self._idle_event = threading.Event()
        self._idle_deadline = self._clock() + self.idle_unload_seconds
        return self._idle_deadline

    def load(self, model_path, options=None, *, alias=None) -> None:
        self.cancel_idle_unload()
        self.load_calls.append((Path(model_path), deepcopy(options), alias))
        self._instance = _FakeInstance()
        self._loaded_alias = alias
        self._idle_event = None
        self._idle_deadline = None
        self._idle_callback = None

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
        self.cancel_idle_unload()
        self.unload_calls += 1
        old = self._instance
        self._instance = None
        self._loaded_alias = None
        self._idle_event = None
        self._idle_deadline = None
        self._idle_callback = None
        if old is not None:
            old.closed = True
            self.unloaded_instances.append(old)


class _FakeLock:
    def __init__(self) -> None:
        self.available = True
        self.held = False
        self.acquire_calls = 0
        self.release_calls = 0
        self.timeouts: list[float] = []

    def acquire(self, timeout=None) -> bool:
        self.acquire_calls += 1
        self.timeouts.append(timeout)
        if not self.available:
            return False
        self.held = True
        return True

    def release(self) -> None:
        self.release_calls += 1
        self.held = False


class _FailingCache(ResponseCache):
    def put(self, key, *, alias, revision, response) -> bool:
        raise RuntimeError("cache write failed")


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
        self.lock = _FakeLock()
        self.downloader = _FakeDownloader(self.lock)
        self.preflight = _FakePreflight()
        self.runtime = _FakeRuntime()
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

    def test_first_cache_miss_loads_and_schedules_idle_unload(
        self,
    ) -> None:
        response = self.client.post(self._url(), json=self.body)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["choices"][0]["message"]["content"], "hello")
        self.assertEqual(len(self.runtime.load_calls), 1)
        self.assertEqual(self.runtime.load_calls[0][0].parent.name, "alias-a")
        self.assertEqual(self.runtime.load_calls[0][2], "alias-a")
        self.assertEqual(self.runtime.unload_calls, 0)
        self.assertTrue(self.runtime.is_loaded())
        self.assertEqual(self.runtime.loaded_alias, "alias-a")
        self.assertEqual(self.runtime.schedule_idle_unload_calls, 1)
        self.assertIsNotNone(self.runtime.idle_deadline)
        self.assertEqual(self.runtime.idle_deadline, 180.0)
        self.assertEqual(len(self.preflight.load_calls), 1)
        self.assertEqual(len(self._cache_files()), 1)

    def test_three_sequential_prompts_same_alias_use_one_instance_and_refresh_idle_deadline(
        self,
    ) -> None:
        prompts = [
            {"messages": [{"role": "user", "content": "first prompt"}]},
            {"messages": [{"role": "user", "content": "second prompt"}]},
            {"messages": [{"role": "user", "content": "third prompt"}]},
        ]

        first = self.client.post(self._url(), json=prompts[0])
        self.assertEqual(first.status_code, 200)
        self.assertEqual(len(self.runtime.load_calls), 1)
        self.assertEqual(len(self.preflight.load_calls), 1)
        self.assertEqual(len(self.runtime.chat_calls), 1)
        self.assertEqual(len(self._cache_files()), 1)
        self.assertEqual(self.runtime.schedule_idle_unload_calls, 1)
        self.assertEqual(self.runtime.refresh_idle_unload_calls, 0)
        self.assertEqual(self.runtime.idle_deadline, 180.0)
        self.assertEqual(self.runtime.unload_calls, 0)

        self.runtime.load_calls.clear()
        self.preflight.load_calls.clear()
        self.runtime.clock_value = 100.0
        second = self.client.post(self._url(), json=prompts[1])
        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(self.runtime.load_calls), 0)
        self.assertEqual(len(self.preflight.load_calls), 0)
        self.assertEqual(len(self.runtime.chat_calls), 2)
        self.assertEqual(len(self._cache_files()), 2)
        self.assertEqual(self.runtime.schedule_idle_unload_calls, 2)
        self.assertEqual(self.runtime.refresh_idle_unload_calls, 0)
        self.assertEqual(self.runtime.idle_deadline, 280.0)

        self.runtime.load_calls.clear()
        self.preflight.load_calls.clear()
        self.runtime.clock_value = 200.0

        third = self.client.post(self._url(), json=prompts[2])
        self.assertEqual(third.status_code, 200)
        self.assertEqual(len(self.runtime.load_calls), 0)
        self.assertEqual(len(self.preflight.load_calls), 0)
        self.assertEqual(len(self.runtime.chat_calls), 3)
        self.assertEqual(len(self._cache_files()), 3)
        self.assertEqual(self.runtime.schedule_idle_unload_calls, 3)
        self.assertEqual(self.runtime.refresh_idle_unload_calls, 0)
        self.assertEqual(self.runtime.idle_deadline, 380.0)
        self.assertEqual(self.runtime.unload_calls, 0)
        self.assertTrue(self.runtime.is_loaded())
        self.assertEqual(self.runtime.loaded_alias, "alias-a")

    def test_idle_expiry_unloads_instance_and_marks_registry_unloaded(self) -> None:
        response = self.client.post(self._url(), json=self.body)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.runtime.is_loaded())
        self.assertEqual(self.registry.get("alias-a").state, ModelState.LOADED)

        callback = self.runtime.idle_callback
        self.assertIsNotNone(callback)
        self.runtime.unload()
        callback()

        self.assertFalse(self.runtime.is_loaded())
        self.assertEqual(len(self.runtime.unloaded_instances), 1)
        self.assertTrue(self.runtime.unloaded_instances[0].closed)
        self.assertEqual(self.registry.get("alias-a").state, ModelState.UNLOADED)

    def test_switching_alias_unloads_previous_alias_immediately(self) -> None:
        first = self.client.post(self._url(), json=self.body)
        self.assertEqual(first.status_code, 200)

        self.runtime.load_calls.clear()
        self.preflight.load_calls.clear()
        self.runtime.unload_calls = 0

        second = self.client.post(self._url("alias-b"), json=self.body)
        self.assertEqual(second.status_code, 200)

        self.assertEqual(self.runtime.unload_calls, 1)
        self.assertTrue(self.runtime.is_loaded())
        self.assertEqual(self.runtime.loaded_alias, "alias-b")
        self.assertEqual(len(self.runtime.load_calls), 1)
        self.assertEqual(self.runtime.load_calls[0][2], "alias-b")
        self.assertEqual(len(self.preflight.load_calls), 1)
        self.assertEqual(self.registry.get("alias-a").state, ModelState.UNLOADED)
        self.assertEqual(self.registry.get("alias-b").state, ModelState.LOADED)
        self.assertEqual(len(self.runtime.unloaded_instances), 1)
        self.assertTrue(self.runtime.unloaded_instances[0].closed)

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
        self.assertEqual(first.status_code, 200)

        self.preflight.load_calls.clear()
        self.preflight.download_calls.clear()
        self.downloader.calls.clear()
        self.runtime.load_calls.clear()
        self.runtime.chat_calls.clear()
        self.runtime.unload_calls = 0
        self.runtime.refresh_idle_unload_calls = 0
        self.runtime.schedule_idle_unload_calls = 0

        second = self.client.post(self._url(), json=self.body)

        self.assertEqual(second.json(), first.json())
        self.assertEqual(self.preflight.load_calls, [])
        self.assertEqual(self.preflight.download_calls, [])
        self.assertEqual(self.downloader.calls, [])
        self.assertEqual(self.runtime.load_calls, [])
        self.assertEqual(self.runtime.chat_calls, [])
        self.assertEqual(self.runtime.unload_calls, 0)
        self.assertEqual(self.runtime.refresh_idle_unload_calls, 1)
        self.assertEqual(self.runtime.schedule_idle_unload_calls, 0)
        self.assertTrue(self.runtime.is_loaded())

    def test_different_sampling_parameter_creates_different_cache_key(self) -> None:
        self.client.post(self._url(), json=self.body)
        self.runtime.load_calls.clear()
        self.runtime.chat_calls.clear()
        self.preflight.load_calls.clear()

        response = self.client.post(
            self._url(), json={**self.body, "temperature": 0.9}
        )

        self.assertEqual(
            response.json()["choices"][0]["message"]["content"], "temp=0.9"
        )
        self.assertEqual(len(self.runtime.load_calls), 0)
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

    def test_streaming_cache_miss_emits_sse_and_schedules_idle_unload(self) -> None:
        response = self.client.post(
            self._url(), json={**self.body, "stream": True}
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.headers["content-type"])
        self.assertIn("chat.completion.chunk", response.text)
        self.assertIn('"content":"hello"', response.text)
        self.assertTrue(response.text.endswith("data: [DONE]\n\n"))
        self.assertEqual(self.runtime.unload_calls, 0)
        self.assertTrue(self.runtime.is_loaded())
        self.assertEqual(self.runtime.schedule_idle_unload_calls, 1)
        self.assertIsNotNone(self.runtime.idle_deadline)

    def test_non_stream_cache_hit_streams_without_loading(self) -> None:
        self.client.post(self._url(), json=self.body)
        self.runtime.load_calls.clear()
        self.runtime.chat_calls.clear()
        self.runtime.unload_calls = 0
        self.runtime.refresh_idle_unload_calls = 0
        self.runtime.schedule_idle_unload_calls = 0

        response = self.client.post(
            self._url(), json={**self.body, "stream": True}
        )

        self.assertTrue(response.text.endswith("data: [DONE]\n\n"))
        self.assertEqual(self.runtime.load_calls, [])
        self.assertEqual(self.runtime.chat_calls, [])
        self.assertEqual(self.runtime.unload_calls, 0)
        self.assertEqual(self.runtime.refresh_idle_unload_calls, 1)
        self.assertEqual(self.runtime.schedule_idle_unload_calls, 0)
        self.assertTrue(self.runtime.is_loaded())

    def test_corrupt_cache_entry_is_treated_as_miss(self) -> None:
        self.client.post(self._url(), json=self.body)
        cache_file = self._cache_files()[0]
        cache_file.write_text("not json", encoding="utf-8")
        self.runtime.load_calls.clear()
        self.runtime.chat_calls.clear()

        response = self.client.post(self._url(), json=self.body)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.runtime.load_calls), 0)
        self.assertEqual(len(self.runtime.chat_calls), 1)
        self.assertEqual(self.runtime.unload_calls, 0)
        self.assertTrue(self.runtime.is_loaded())

    def test_preflight_failure_returns_503_without_loading(self) -> None:
        self.preflight.fail_load = True

        response = self.client.post(self._url(), json=self.body)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["error"]["code"], "resource_unavailable"
        )
        self.assertEqual(self.runtime.load_calls, [])
        self.assertEqual(self._cache_files(), [])

    def test_restore_preflight_failure_returns_503_without_download(self) -> None:
        self.preflight.fail_download = True

        response = self.client.post(self._url(), json=self.body)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["error"]["code"], "resource_unavailable"
        )
        self.assertEqual(self.downloader.calls, [])
        self.assertEqual(self.runtime.load_calls, [])
        entry = self.registry.get("alias-a")
        self.assertEqual(entry.state, ModelState.REGISTERED)
        self.assertIsNone(entry.path)

    def test_download_failure_keeps_registry_registered(self) -> None:
        self.downloader.fail_download = True

        response = self.client.post(self._url(), json=self.body)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "download_error")
        self.assertEqual(self.downloader.calls, ["alias-a"])
        entry = self.registry.get("alias-a")
        self.assertEqual(entry.state, ModelState.REGISTERED)
        self.assertIsNone(entry.path)
        self.assertEqual(self.runtime.load_calls, [])

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

    def test_download_runs_without_holding_lifecycle_lock(self) -> None:
        response = self.client.post(self._url(), json=self.body)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.downloader.calls, ["alias-a"])
        self.assertEqual(self.downloader.lock_held_during_download, [False])

    def test_concurrent_same_alias_resolution_downloads_once(self) -> None:
        services = Services(
            settings=self.settings,
            root=self.root,
            registry=self.registry,
            cache=self.cache,
            downloader=self.downloader,
            preflight=self.preflight,
            runtime=self.runtime,
            lock=threading.Lock(),
            download_locks=AliasDownloadLocks(),
        )
        start = threading.Barrier(2)
        paths = []
        errors = []

        def resolve() -> None:
            try:
                start.wait(timeout=5)
                paths.append(_resolve_path(services, "alias-a"))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=resolve) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(paths), 2)
        self.assertEqual(paths[0], paths[1])
        self.assertEqual(self.downloader.calls, ["alias-a"])
        self.assertTrue((self.root / "models/alias-a/model-a.gguf").is_file())

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
        self.assertFalse(self.runtime.is_loaded())
        self.assertEqual(self.runtime.schedule_idle_unload_calls, 0)
        self.assertEqual(self.registry.get("alias-a").state, ModelState.UNLOADED)

    def test_cache_put_failure_unloads_and_does_not_schedule_idle_unload(self) -> None:
        self.services.cache = _FailingCache(CacheConfig(dir=".cache/responses"), root=self.root)

        response = self.client.post(self._url(), json=self.body)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "runtime_error")
        self.assertEqual(self.runtime.unload_calls, 1)
        self.assertFalse(self.runtime.is_loaded())
        self.assertEqual(self.runtime.schedule_idle_unload_calls, 0)
        self.assertEqual(self.registry.get("alias-a").state, ModelState.UNLOADED)


if __name__ == "__main__":
    unittest.main()
