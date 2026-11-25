import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from local_llm.cache import (
    CacheConfig,
    ResponseCache,
    iter_sse_frames,
    make_cache_key,
    weight_fingerprint,
)

MESSAGES = [{"role": "user", "content": "hello"}]

RESPONSE = {
    "id": "chatcmpl-cache-test",
    "object": "chat.completion",
    "created": 1769280000,
    "model": "phi4-mini",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "hi"},
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "total_tokens": 2,
    },
}


class ResponseCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.cache = ResponseCache(root=self.root, clock=lambda: self.now)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def key(self, **overrides: object) -> str:
        arguments = {
            "alias": "phi4-mini",
            "revision": "main",
            "fingerprint": "123:456",
            "messages": MESSAGES,
            "params": {"temperature": 0.7},
            "runtime": {"n_ctx": 4096},
        }
        arguments.update(overrides)
        return make_cache_key(**arguments)

    def test_put_then_get_returns_stored_response(self) -> None:
        key = self.key()

        self.assertTrue(
            self.cache.put(
                key,
                alias="phi4-mini",
                revision="main",
                response=RESPONSE,
            )
        )
        self.assertEqual(self.cache.get(key), RESPONSE)
        self.assertTrue(self.cache.path_for(key).exists())

    def test_transport_fields_do_not_change_key(self) -> None:
        first = self.key(params={"temperature": 0.7, "stream": True, "request_id": "r1"})
        second = self.key(params={"temperature": 0.7})

        self.assertEqual(first, second)

    def test_sampling_fields_change_key(self) -> None:
        first = self.key(params={"temperature": 0.7})
        second = self.key(params={"temperature": 0.8})

        self.assertNotEqual(first, second)

    def test_corrupt_entry_is_a_miss_and_is_deleted(self) -> None:
        key = self.key()
        path = self.cache.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")

        self.assertIsNone(self.cache.get(key))
        self.assertFalse(path.exists())

    def test_expired_entry_is_a_miss(self) -> None:
        key = self.key()
        self.cache.put(key, alias="phi4-mini", revision="main", response=RESPONSE)

        self.now += timedelta(days=31)

        self.assertIsNone(self.cache.get(key))

    def test_sse_replay_ends_with_done(self) -> None:
        frames = list(self.cache.replay_sse(RESPONSE))
        joined = "".join(frames)

        self.assertEqual(frames[-1], "data: [DONE]\n\n")
        self.assertIn('"content":"hi"', joined)
        self.assertIn('"finish_reason":"stop"', joined)

    def test_max_size_evicts_entries(self) -> None:
        config = CacheConfig(max_size_mb=0.00001)
        cache = ResponseCache(config, root=self.root, clock=lambda: self.now)
        first_key = self.key(fingerprint="1:1")
        second_key = self.key(fingerprint="2:2")

        cache.put(first_key, alias="phi4-mini", revision="main", response=RESPONSE)
        cache.put(second_key, alias="phi4-mini", revision="main", response=RESPONSE)

        total_bytes = sum(path.stat().st_size for path in cache.dir.glob("*.json"))
        self.assertLess(total_bytes, int(config.max_size_mb * 1024 * 1024))

    def test_cache_dir_may_not_escape_workspace(self) -> None:
        with self.assertRaises(ValueError):
            ResponseCache(CacheConfig(dir="../outside"), root=self.root)

    def test_weight_fingerprint_uses_file_metadata(self) -> None:
        model_file = self.root / "model.gguf"
        model_file.write_bytes(b"gguf")

        fingerprint = weight_fingerprint(model_file)

        self.assertIn(str(model_file.stat().st_size), fingerprint)
        self.assertIn(str(model_file.stat().st_mtime_ns), fingerprint)


if __name__ == "__main__":
    unittest.main()
