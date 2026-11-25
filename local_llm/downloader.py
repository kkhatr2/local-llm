"""Hugging Face CLI downloader for GGUF model files."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from local_llm.config import DownloadSettings, ModelRecord, PathSettings


class DownloaderError(Exception):
    """Base class for downloader failures."""


class NonPublicRepoError(DownloaderError):
    """Raised when a repository is not public."""


class DownloadCommandError(DownloaderError):
    """Raised when the download command fails."""


class DownloadTimeoutError(DownloadCommandError):
    """Raised when the download command times out."""


class DownloadedFileMissingError(DownloaderError):
    """Raised when the expected file is missing after a successful download."""


class HfCliDownloader:
    def __init__(
        self,
        download_settings: DownloadSettings | None = None,
        *,
        path_settings: PathSettings | None = None,
        runner: Callable[[list[str], int], int],
        repo_info: Callable[[str, str], bool],
    ) -> None:
        self.download_settings = (
            download_settings if download_settings is not None else DownloadSettings()
        )
        self.path_settings = (
            path_settings if path_settings is not None else PathSettings()
        )
        self.runner = runner
        self.repo_info = repo_info

    def _destination(self, record: ModelRecord, root: Path | str) -> Path:
        return Path(root) / self.path_settings.model_dir / record.alias / record.filename

    def exists(self, record: ModelRecord, root: Path | str) -> bool:
        destination = self._destination(record, root)
        return destination.is_file() and destination.stat().st_size > 0

    def download(self, record: ModelRecord, root: Path | str) -> Path:
        destination = self._destination(record, root)
        if self.exists(record, root):
            return destination

        if not self.repo_info(record.repo_id, record.revision):
            raise NonPublicRepoError(
                f"repository is not public: {record.repo_id}"
            )

        destination.parent.mkdir(parents=True, exist_ok=True)

        command = [
            self.download_settings.command,
            "download",
            record.repo_id,
            record.filename,
            "--revision",
            record.revision,
            "--local-dir",
            str(destination.parent),
        ]

        try:
            return_code = self.runner(command, self.download_settings.timeout_seconds)
        except TimeoutError:
            raise DownloadTimeoutError(
                f"download timed out for {record.repo_id}"
            ) from None
        except Exception:
            raise DownloadCommandError(
                f"download failed for {record.repo_id}"
            ) from None

        if return_code != 0:
            raise DownloadCommandError(
                f"download failed for {record.repo_id}"
            )

        if not self.exists(record, root):
            raise DownloadedFileMissingError(
                f"downloaded file missing for {record.alias}"
            )

        return destination


__all__ = [
    "DownloadCommandError",
    "DownloadTimeoutError",
    "DownloadedFileMissingError",
    "DownloaderError",
    "HfCliDownloader",
    "NonPublicRepoError",
]