import inspect
import tempfile
import unittest
from pathlib import Path

from local_llm.cache import CacheConfig, ResponseCache
from local_llm.config import ModelRecord
from local_llm.model_supply import ModelSupply, SupplyError
from local_llm.sync import ObjectCredentials, ObjectStorageError, ObjectStorageSync


class _Response:
    def __init__(self, chunks, *, status_code: int = 200) -> None:
        self.chunks = chunks
        self.status_code = status_code

    def iter_bytes(self):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


class _Client:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls = []

    def get(self, url, headers=None):
        self.calls.append((url, headers or {}))
        return self.response


class _LegacyDownloader:
    def __init__(self, root: Path, record: ModelRecord) -> None:
        self.root = root
        self.record = record
        self.download_calls = 0

    def exists(self, record, root) -> bool:
        path = Path(root) / "models" / record.alias / record.filename
        return path.is_file() and path.stat().st_size > 0

    def download(self, record, root) -> Path:
        self.download_calls += 1
        path = Path(root) / "models" / record.alias / record.filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"gguf")
        return path


class Phase2CacheSupplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.record = ModelRecord(
            alias="alpha", repo_id="owner/model", filename="model.gguf"
        )

    def test_cache_directory_uses_instance_id(self) -> None:
        cache = ResponseCache(
            CacheConfig(dir=".cache/responses"),
            root=self.root,
            instance_id="pod-a",
        )
        self.assertEqual(cache.dir, self.root / ".cache" / "responses" / "pod-a")

    def test_two_instances_do_not_share_entries(self) -> None:
        first = ResponseCache(CacheConfig(dir="cache"), root=self.root, instance_id="a")
        second = ResponseCache(CacheConfig(dir="cache"), root=self.root, instance_id="b")
        key = "0" * 64
        response = {"id": "one"}
        self.assertTrue(first.put(key, alias="alpha", revision="main", response=response))
        self.assertIsNone(second.get(key))
        self.assertEqual(first.get(key), response)

    def test_instance_id_rejects_traversal(self) -> None:
        with self.assertRaises(ValueError):
            ResponseCache(CacheConfig(dir="cache"), root=self.root, instance_id="../escape")

    def test_object_storage_sync_publishes_complete_weight(self) -> None:
        client = _Client(_Response([b"gg", b"uf"]))
        sync = ObjectStorageSync(
            client,
            ObjectCredentials("key", "secret"),
            "bucket",
            "gguf/",
            "us-east-1",
        )
        path = sync.sync(self.record, self.root)
        self.assertEqual(path.read_bytes(), b"gguf")
        self.assertNotIn("secret", str(client.calls[0][1]))

    def test_sync_failure_leaves_no_partial_weights(self) -> None:
        client = _Client(_Response([b"partial", RuntimeError("boom")]))
        sync = ObjectStorageSync(
            client,
            ObjectCredentials("key", "secret"),
            "bucket",
            "gguf/",
            "us-east-1",
        )
        with self.assertRaises(ObjectStorageError):
            sync.sync(self.record, self.root)
        alias_dir = self.root / "models" / "alpha"
        self.assertFalse((alias_dir / "model.gguf").exists())
        self.assertEqual(list(alias_dir.glob("*.part")), [])

    def test_object_storage_request_path_never_syncs(self) -> None:
        client = _Client(_Response([b"gguf"]))
        sync = ObjectStorageSync(
            client,
            ObjectCredentials("key", "secret"),
            "bucket",
            "gguf/",
            "us-east-1",
        )
        supply = ModelSupply("object_storage", sync=sync)
        with self.assertRaises(SupplyError):
            supply.resolve(self.record, self.root)
        self.assertEqual(client.calls, [])
        sync.sync(self.record, self.root)
        self.assertEqual(supply.resolve(self.record, self.root).read_bytes(), b"gguf")
        self.assertEqual(len(client.calls), 1)

    def test_hf_supply_preserves_legacy_downloader_contract(self) -> None:
        downloader = _LegacyDownloader(self.root, self.record)
        supply = ModelSupply("hf", downloader=downloader)
        path = supply.resolve(self.record, self.root)
        self.assertEqual(path.read_bytes(), b"gguf")
        self.assertEqual(downloader.download_calls, 1)

    def test_supply_modules_do_not_import_lifecycle_lock(self) -> None:
        from local_llm import model_supply, sync

        object_storage_source = inspect.getsource(sync.object_storage)
        supply_source = inspect.getsource(model_supply)
        self.assertNotIn("local_llm.locking", object_storage_source)
        self.assertNotIn("local_llm.locking", supply_source)


if __name__ == "__main__":
    unittest.main()
