import unittest

from local_llm.streaming import CompletionAccumulator, sse_data


class CompletionAccumulatorTests(unittest.TestCase):
    def test_accumulator_builds_completed_response(self) -> None:
        accumulator = CompletionAccumulator()
        chunks = [
            {
                "id": "chatcmpl-test",
                "created": 123,
                "model": "alias-a",
                "choices": [{"index": 0, "delta": {"role": "assistant"}}],
            },
            {
                "id": "chatcmpl-test",
                "choices": [{"index": 0, "delta": {"content": "Hel"}}],
            },
            {
                "id": "chatcmpl-test",
                "choices": [{"index": 0, "delta": {"content": "lo"}}],
            },
            {
                "id": "chatcmpl-test",
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 2},
            },
        ]

        for chunk in chunks:
            accumulator.add(chunk)

        self.assertEqual(
            accumulator.response(),
            {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 123,
                "model": "alias-a",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Hello"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 2},
            },
        )

    def test_accumulator_tracks_choice_indices_independently(self) -> None:
        accumulator = CompletionAccumulator()
        accumulator.add(
            {
                "choices": [
                    {"index": 1, "delta": {"content": "second"}},
                    {"index": 0, "delta": {"content": "first"}},
                ]
            }
        )

        response = accumulator.response()

        self.assertEqual(
            [choice["message"]["content"] for choice in response["choices"]],
            ["first", "second"],
        )
        self.assertNotIn("usage", response)

    def test_empty_stream_is_rejected(self) -> None:
        accumulator = CompletionAccumulator()

        with self.assertRaisesRegex(RuntimeError, "empty stream"):
            accumulator.response()

    def test_sse_data_uses_canonical_json(self) -> None:
        self.assertEqual(sse_data({"b": 1, "a": "x"}), 'data: {"a":"x","b":1}\n\n')


if __name__ == "__main__":
    unittest.main()
