import unittest

from local_llm.downloader import DownloadCommandError, DownloadTimeoutError
from local_llm.error_mapper import ErrorMapper
from local_llm.preflight import ResourceError
from local_llm.registry import DuplicateAliasError, UnknownAliasError


class ErrorMapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mapper = ErrorMapper()

    def assert_mapped(self, exception: BaseException, status: int, code: str) -> dict:
        result = self.mapper.map(exception)
        self.assertEqual(result.status_code, status)
        self.assertEqual(set(result.body), {"error"})
        self.assertEqual(set(result.body["error"]), {"code", "message", "type"})
        self.assertEqual(result.body["error"]["code"], code)
        return result.body["error"]

    def test_validation_error_maps_to_422(self) -> None:
        self.assert_mapped(ValueError("bad field"), 422, "validation_error")

    def test_unknown_alias_maps_to_404(self) -> None:
        self.assert_mapped(UnknownAliasError("missing"), 404, "not_found")

    def test_duplicate_alias_maps_to_409(self) -> None:
        self.assert_mapped(DuplicateAliasError("alpha"), 409, "conflict")

    def test_resource_error_maps_to_503(self) -> None:
        self.assert_mapped(ResourceError("low disk"), 503, "resource_unavailable")

    def test_download_error_maps_to_502(self) -> None:
        self.assert_mapped(DownloadCommandError("failed"), 502, "download_error")

    def test_download_timeout_maps_to_504(self) -> None:
        self.assert_mapped(DownloadTimeoutError("slow"), 504, "download_timeout")

    def test_runtime_error_maps_to_502(self) -> None:
        self.assert_mapped(RuntimeError("boom"), 502, "runtime_error")

    def test_timeout_error_maps_to_504(self) -> None:
        self.assert_mapped(TimeoutError("slow"), 504, "runtime_timeout")

    def test_unknown_exception_maps_to_500(self) -> None:
        self.assert_mapped(Exception("unknown"), 500, "internal_error")

    def test_message_is_sanitized(self) -> None:
        error = self.assert_mapped(
            RuntimeError("C:\\secret\\model.gguf Traceback"), 502, "runtime_error"
        )

        self.assertNotIn("/", error["message"])
        self.assertNotIn("\\", error["message"])
        self.assertNotIn("Traceback", error["message"])
        self.assertEqual(error["type"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()
