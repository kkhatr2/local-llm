import unittest
from pathlib import Path

from local_llm.config import ModelRecord
from local_llm.registry import (
    DuplicateAliasError,
    ModelState,
    ModelRegistry,
    RegistryEntry,
    UnknownAliasError,
)


def make_record(alias: str = "alpha") -> ModelRecord:
    return ModelRecord(
        alias=alias,
        repo_id="owner/name",
        filename="model.gguf",
    )


class ModelRegistryTests(unittest.TestCase):
    def test_list_returns_registered_records(self) -> None:
        registry = ModelRegistry([make_record("alpha"), make_record("beta")])

        records = registry.list()

        self.assertEqual([record.alias for record in records], ["alpha", "beta"])
        self.assertTrue(all(isinstance(record, ModelRecord) for record in records))

    def test_list_and_get_never_expose_local_paths(self) -> None:
        registry = ModelRegistry([make_record("alpha")])
        registry.mark_downloaded("alpha", Path("/tmp/secret/model.gguf"))

        for record in registry.list():
            self.assertFalse(hasattr(record, "path"))
        entry = registry.get("alpha")
        self.assertIsInstance(entry, RegistryEntry)
        self.assertEqual(entry.record.alias, "alpha")
        self.assertNotIn("path", type(entry.record).model_fields)

    def test_get_unknown_alias_raises(self) -> None:
        registry = ModelRegistry()

        with self.assertRaises(UnknownAliasError) as context:
            registry.get("missing")

        self.assertIn("missing", str(context.exception))

    def test_add_registers_new_alias(self) -> None:
        registry = ModelRegistry()

        registry.add(make_record("gamma"))

        self.assertEqual([record.alias for record in registry.list()], ["gamma"])
        entry = registry.get("gamma")
        self.assertEqual(entry.state, ModelState.REGISTERED)
        self.assertIsNone(entry.path)

    def test_add_duplicate_raises_and_names_alias(self) -> None:
        registry = ModelRegistry([make_record("alpha")])

        with self.assertRaises(DuplicateAliasError) as context:
            registry.add(make_record("alpha"))

        self.assertIn("alpha", str(context.exception))

    def test_remove_unregisters_alias(self) -> None:
        registry = ModelRegistry([make_record("alpha")])

        registry.remove("alpha")

        self.assertEqual(registry.list(), [])
        with self.assertRaises(UnknownAliasError):
            registry.get("alpha")

    def test_mark_downloaded_sets_state_and_path(self) -> None:
        registry = ModelRegistry([make_record("alpha")])
        path = Path("models/alpha/model.gguf")

        registry.mark_downloaded("alpha", path)

        entry = registry.get("alpha")
        self.assertEqual(entry.state, ModelState.DOWNLOADED)
        self.assertEqual(entry.path, path)

    def test_set_state_updates_state(self) -> None:
        registry = ModelRegistry([make_record("alpha")])

        registry.set_state("alpha", ModelState.LOADED)

        self.assertEqual(registry.get("alpha").state, ModelState.LOADED)


if __name__ == "__main__":
    unittest.main()