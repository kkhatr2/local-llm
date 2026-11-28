"""Hugging Face CLI downloader for GGUF model files."""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Callable

from local_llm.config import DownloadSettings, ModelRecord, PathSettings
from local_llm.paths import safe_model_path

LOGGER = logging.getLogger(__name__)


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
        return safe_model_path(
            root, self.path_settings.model_dir, record.alias, record.filename
        )

    def _publish_atomically(self, source: Path, destination: Path) -> None:
        """Fsync a completed download before publishing it as the model path."""

        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=destination.parent, prefix=".hf-", suffix=".part", delete=False
            ) as handle:
                temp_name = handle.name
                with source.open("rb") as downloaded:
                    while True:
                        chunk = downloaded.read(1024 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, destination)
            temp_name = None
        finally:
            if temp_name is not None:
                Path(temp_name).unlink(missing_ok=True)

    def exists(self, record: ModelRecord, root: Path | str) -> bool:
        destination = self._destination(record, root)
        return destination.is_file() and destination.stat().st_size > 0

    def download(self, record: ModelRecord, root: Path | str) -> Path:
        destination = self._destination(record, root)
        if self.exists(record, root):
            LOGGER.info(
                "model already present: alias=%s repo_id=%s; skipping download",
                record.alias,
                record.repo_id,
            )
            return destination

        if not self.repo_info(record.repo_id, record.revision):
            LOGGER.error(
                "model download rejected: alias=%s repo_id=%s reason=not_public",
                record.alias,
                record.repo_id,
            )
            raise NonPublicRepoError(
                f"repository is not public: {record.repo_id}"
            )

        LOGGER.info(
            "downloading model: alias=%s repo_id=%s filename=%s",
            record.alias,
            record.repo_id,
            record.filename,
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
            if return_code != 0:
                LOGGER.error(
                    "model download failed: alias=%s repo_id=%s exit_code=%s",
                    record.alias,
                    record.repo_id,
                    return_code,
                )
                raise DownloadCommandError(
                    f"download failed for {record.repo_id}"
                )
            if not destination.is_file() or destination.stat().st_size <= 0:
                LOGGER.error(
                    "downloaded file missing: alias=%s repo_id=%s",
                    record.alias,
                    record.repo_id,
                )
                raise DownloadedFileMissingError(
                    f"downloaded file missing for {record.alias}"
                )
            self._publish_atomically(destination, destination)
        except TimeoutError:
            LOGGER.error(
                "model download timed out: alias=%s repo_id=%s",
                record.alias,
                record.repo_id,
            )
            destination.unlink(missing_ok=True)
            raise DownloadTimeoutError(
                f"download timed out for {record.repo_id}"
            ) from None
        except DownloaderError:
            destination.unlink(missing_ok=True)
            raise
        except Exception as exc:
            LOGGER.error(
                "model download failed: alias=%s repo_id=%s error=%s",
                record.alias,
                record.repo_id,
                type(exc).__name__,
            )
            destination.unlink(missing_ok=True)
            raise DownloadCommandError(
                f"download failed for {record.repo_id}"
            ) from None

        LOGGER.info(
            "model download complete: alias=%s repo_id=%s",
            record.alias,
            record.repo_id,
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