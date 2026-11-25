import os
import unittest


class ContainerEnvironmentTests(unittest.TestCase):
    def test_hf_token_is_injected_by_compose(self) -> None:
        token = os.environ.get("HF_TOKEN", "")

        self.assertTrue(
            token,
            "HF_TOKEN must be injected from private.txt by docker compose",
        )
        self.assertFalse(
            token.startswith("HF_TOKEN="),
            "HF_TOKEN must contain only the token value",
        )
        self.assertEqual(token, token.strip())


if __name__ == "__main__":
    unittest.main()
