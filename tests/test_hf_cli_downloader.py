import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_llm.config import ModelRecord
from local_llm.downloader import HfCliDownloader, NonPublicRepoError


class HfCliDownloaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.record = ModelRecord(
            alias="alpha",
            repo_id="owner/name",
            filename="model.gguf",
        )
        self.destination = self.root / "models" / "alpha" / "model.gguf"

    def test_download_calls_runner_and_returns_path(self) -> None:
        calls: list[tuple[list[str], int]] = []

        def runner(command: list[str], timeout: int) -> int:
            calls.append((command, timeout))
            local_dir = Path(command[command.index("--local-dir") + 1])
            (local_dir / self.record.filename).write_bytes(b"gguf")
            return 0

        downloader = HfCliDownloader(runner=runner, repo_info=lambda *_: True)

        result = downloader.download(self.record, self.root)

        self.assertEqual(
            calls,
            [
                (
                    [
                        "huggingface-cli",
                        "download",
                        "owner/name",
                        "model.gguf",
                        "--revision",
                        "main",
                        "--local-dir",
                        str(self.destination.parent),
                    ],
                    1800,
                )
            ],
        )
        self.assertEqual(result, self.destination)
        self.assertTrue(result.is_file())
        self.assertGreater(result.stat().st_size, 0)

    def test_existing_file_skips_runner(self) -> None:
        self.destination.parent.mkdir(parents=True)
        self.destination.write_bytes(b"gguf")

        def fail(*_args: object, **_kwargs: object) -> None:
            self.fail("injected dependency should not be called")

        downloader = HfCliDownloader(runner=fail, repo_info=fail)

        self.assertEqual(downloader.download(self.record, self.root), self.destination)

    def test_non_public_repo_rejects_before_runner(self) -> None:
        def fail_runner(*_args: object, **_kwargs: object) -> None:
            self.fail("runner should not be called")

        downloader = HfCliDownloader(
            runner=fail_runner,
            repo_info=lambda *_: False,
        )

        with self.assertRaises(NonPublicRepoError):
            downloader.download(self.record, self.root)

        self.assertFalse(self.destination.parent.exists())
        self.assertFalse((self.root / "models").exists())


if __name__ == "__main__":
    unittest.main()
