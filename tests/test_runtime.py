import unittest

from local_llm.runtime import (
    ChatRequest,
    LlamaRuntimeAdapter,
    merge_load_options,
    merge_sampling_options,
)

MESSAGES = [{"role": "user", "content": "hello"}]


class _FakeLlama:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self.completion_calls = []

    def create_chat_completion(self, messages, stream=False, **sampling):
        self.completion_calls.append((messages, stream, dict(sampling)))
        if stream:
            return [{"id": "chunk", "choices": [{"delta": {"content": "hi"}}]}]
        return {"id": "resp", "choices": [{"message": {"content": "hi"}}]}

    def close(self):
        self.closed = True


def _make_factory(record):
    def factory(**kwargs):
        instance = _FakeLlama(**kwargs)
        record["instance"] = instance
        record["kwargs"] = kwargs
        return instance

    return factory


class LlamaRuntimeAdapterTests(unittest.TestCase):
    def test_load_calls_factory_with_load_options(self):
        record = {}
        adapter = LlamaRuntimeAdapter(
            _make_factory(record),
            llama_defaults={"n_ctx": 16384, "n_batch": 512, "flash_attn": True, "verbose": False},
        )
        adapter.load("model.gguf", {"n_ctx": 8192, "ignored": "x"})
        self.assertEqual(
            record["kwargs"],
            {
                "model_path": "model.gguf",
                "n_ctx": 8192,
                "n_batch": 512,
                "flash_attn": True,
                "verbose": False,
            },
        )

    def test_load_uses_llama_defaults_for_missing_options(self):
        record = {}
        adapter = LlamaRuntimeAdapter(
            _make_factory(record),
            llama_defaults={"n_ctx": 4096, "n_batch": 256},
        )
        adapter.load("model.gguf")
        self.assertEqual(
            record["kwargs"],
            {"model_path": "model.gguf", "n_ctx": 4096, "n_batch": 256},
        )

    def test_load_stores_model_sampling_options(self):
        record = {}
        adapter = LlamaRuntimeAdapter(_make_factory(record))
        adapter.load("model.gguf", {"temperature": 0.5, "top_p": 0.9, "n_ctx": 1024})
        self.assertEqual(adapter._model_sampling, {"temperature": 0.5, "top_p": 0.9})

    def test_is_loaded_false_before_load(self):
        adapter = LlamaRuntimeAdapter(lambda **kwargs: _FakeLlama(**kwargs))
        self.assertFalse(adapter.is_loaded())

    def test_load_failure_does_not_leave_loaded_instance(self):
        def factory(**kwargs):
            raise RuntimeError("boom")

        adapter = LlamaRuntimeAdapter(factory)
        with self.assertRaises(RuntimeError):
            adapter.load("model.gguf")
        self.assertFalse(adapter.is_loaded())

    def test_unload_clears_instance_and_calls_close(self):
        record = {}
        adapter = LlamaRuntimeAdapter(_make_factory(record))
        adapter.load("model.gguf")
        instance = record["instance"]
        adapter.unload()
        self.assertFalse(adapter.is_loaded())
        self.assertTrue(instance.closed)
        self.assertEqual(adapter._model_sampling, {})

    def test_unload_is_noop_when_not_loaded(self):
        adapter = LlamaRuntimeAdapter(lambda **kwargs: _FakeLlama(**kwargs))
        adapter.unload()
        self.assertFalse(adapter.is_loaded())

    def test_chat_requires_loaded_instance(self):
        adapter = LlamaRuntimeAdapter(lambda **kwargs: _FakeLlama(**kwargs))
        with self.assertRaises(RuntimeError):
            adapter.chat(ChatRequest(messages=MESSAGES))

    def test_chat_calls_create_chat_completion_with_merged_sampling(self):
        record = {}
        adapter = LlamaRuntimeAdapter(
            _make_factory(record),
            llama_defaults={"n_ctx": 1024},
            generation_defaults={"temperature": 0.1, "top_p": 0.2, "top_k": 20},
        )
        adapter.load("model.gguf", {"temperature": 0.5})
        request = ChatRequest(messages=MESSAGES, top_p=0.7, options={"min_p": 0.3})
        result = adapter.chat(request)
        self.assertEqual(result["id"], "resp")
        instance = record["instance"]
        self.assertEqual(len(instance.completion_calls), 1)
        messages, stream, sampling = instance.completion_calls[0]
        self.assertEqual(messages, MESSAGES)
        self.assertFalse(stream)
        self.assertEqual(sampling["temperature"], 0.5)
        self.assertEqual(sampling["top_p"], 0.7)
        self.assertEqual(sampling["top_k"], 20)
        self.assertEqual(sampling["min_p"], 0.3)

    def test_chat_request_options_override_top_level_sampling(self):
        record = {}
        adapter = LlamaRuntimeAdapter(_make_factory(record), generation_defaults={})
        adapter.load("model.gguf")
        request = ChatRequest(messages=MESSAGES, temperature=0.5, options={"temperature": 0.9})
        adapter.chat(request)
        _, _, sampling = record["instance"].completion_calls[0]
        self.assertEqual(sampling["temperature"], 0.9)

    def test_chat_raises_value_error_without_messages(self):
        record = {}
        adapter = LlamaRuntimeAdapter(_make_factory(record))
        adapter.load("model.gguf")
        with self.assertRaises(ValueError):
            adapter.chat(ChatRequest(messages=[]))

    def test_merge_load_options_applies_precedence(self):
        result = merge_load_options(
            {"n_ctx": 1, "n_batch": 2, "flash_attn": False},
            {"n_ctx": 3, "unknown": 4},
            {"n_batch": 5, "n_threads": 6},
        )
        self.assertEqual(result, {"n_ctx": 3, "n_batch": 5, "flash_attn": False, "n_threads": 6})

    def test_merge_sampling_options_applies_precedence(self):
        result = merge_sampling_options(
            {"temperature": 0.1, "top_p": 0.2, "top_k": 20, "frequency_penalty": None},
            {"temperature": 0.5, "min_p": 0.4},
            {"temperature": 0.7, "top_p": None},
            {"top_p": 0.9, "stop": [], "n": 0, "seed": False},
        )
        self.assertEqual(result["temperature"], 0.7)
        self.assertEqual(result["top_p"], 0.9)
        self.assertEqual(result["top_k"], 20)
        self.assertEqual(result["min_p"], 0.4)
        self.assertEqual(result["stop"], [])
        self.assertEqual(result["n"], 0)
        self.assertFalse(result["seed"])
        self.assertNotIn("frequency_penalty", result)


if __name__ == "__main__":
    unittest.main()
