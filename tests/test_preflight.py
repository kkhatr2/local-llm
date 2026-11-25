import unittest
from pathlib import Path
from types import SimpleNamespace

from local_llm.config import ResourceSettings
from local_llm.preflight import GIB, ResourceError, ResourcePreflight


class ResourcePreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = ResourceSettings(
            min_free_disk_gb=1,
            reserve_disk_gb=0.5,
            max_estimated_model_memory_gb=2,
            reserve_system_memory_gb=0.5,
        )
        self.destination = Path("/mnt/models")
        self.model_path = Path("/mnt/models/alpha/model.gguf")

    def make_preflight(self, *, free=None, available=None, size=None):
        return ResourcePreflight(
            self.settings,
            disk_usage=lambda _: SimpleNamespace(free=free),
            memory_reader=lambda: available,
            file_size=lambda _: size,
        )

    def test_check_download_passes_when_free_bytes_are_sufficient(self) -> None:
        preflight = self.make_preflight(free=int(2 * GIB))

        preflight.check_download(self.destination)

    def test_check_download_fails_when_free_bytes_are_insufficient(self) -> None:
        preflight = self.make_preflight(free=int(1 * GIB))

        with self.assertRaises(ResourceError):
            preflight.check_download(self.destination)

    def test_check_download_fails_closed_when_disk_reader_raises(self) -> None:
        def fail(_path: Path) -> None:
            raise RuntimeError("disk read failed")

        preflight = ResourcePreflight(self.settings, disk_usage=fail)

        with self.assertRaises(ResourceError):
            preflight.check_download(self.destination)

    def test_check_load_passes_when_memory_is_sufficient(self) -> None:
        preflight = self.make_preflight(size=int(1 * GIB), available=int(2 * GIB))

        preflight.check_load(self.model_path)

    def test_check_load_fails_when_memory_is_insufficient(self) -> None:
        preflight = self.make_preflight(size=int(1 * GIB), available=int(1 * GIB))

        with self.assertRaises(ResourceError):
            preflight.check_load(self.model_path)

    def test_check_load_fails_closed_when_memory_reader_raises(self) -> None:
        def fail() -> None:
            raise RuntimeError("memory read failed")

        preflight = ResourcePreflight(
            self.settings,
            memory_reader=fail,
            file_size=lambda _: int(GIB),
        )

        with self.assertRaises(ResourceError):
            preflight.check_load(self.model_path)

    def test_check_load_fails_closed_when_file_size_reader_raises(self) -> None:
        def fail(_path: Path) -> None:
            raise RuntimeError("stat failed")

        preflight = ResourcePreflight(self.settings, file_size=fail)

        with self.assertRaises(ResourceError):
            preflight.check_load(self.model_path)

    def test_check_load_rejects_model_over_memory_estimate_limit(self) -> None:
        def fail_memory() -> None:
            self.fail("memory reader should not be called")

        preflight = ResourcePreflight(
            self.settings,
            memory_reader=fail_memory,
            file_size=lambda _: int(3 * GIB),
        )

        with self.assertRaises(ResourceError):
            preflight.check_load(self.model_path)


if __name__ == "__main__":
    unittest.main()
