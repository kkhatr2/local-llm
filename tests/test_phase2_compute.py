import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_llm.compute import BackendSelector, DevicePlacement, PlacementError
from local_llm.composition import build_app
from local_llm.config import BackendDefaults, ComputeSettings, ResourceSettings

ROOT = Path(__file__).resolve().parents[1]


class _Probe:
    def __init__(self, cuda: bool = True, vram: int | None = 8 * 1024**3) -> None:
        self.cuda = cuda
        self.vram = vram

    def cuda_available(self) -> bool:
        return self.cuda

    def cuda_vram_bytes(self) -> int | None:
        return self.vram


class _NoPreflight:
    def check_download(self, root) -> None:
        return None

    def check_load(self, root, model_path) -> None:
        return None


class Phase2ComputeTests(unittest.TestCase):
    def _compute(self, **overrides) -> ComputeSettings:
        values = {
            "default_backend": "cpu",
            "active_backends": ("cpu", "cuda"),
            "documented_backends": ("metal",),
            "fallback": "fail_closed",
            "backend_defaults": BackendDefaults(),
        }
        values.update(overrides)
        return ComputeSettings(**values)

    def test_cuda_probe_returns_cuda_backend(self) -> None:
        decision = BackendSelector(
            self._compute(default_backend="cuda"), _Probe(cuda=True)
        ).select()
        self.assertEqual(decision.backend, "cuda")
        self.assertEqual(decision.load_options["n_gpu_layers"], 33)
        self.assertTrue(decision.available)

    def test_cpu_backend_uses_zero_gpu_layers(self) -> None:
        decision = BackendSelector(self._compute(), _Probe(cuda=True)).select()
        self.assertEqual(decision.backend, "cpu")
        self.assertEqual(decision.load_options["n_gpu_layers"], 0)

    def test_cuda_backend_defaults_are_applied_to_runtime_options(self) -> None:
        settings = self._compute(
            default_backend="cuda",
            backend_defaults=BackendDefaults(cuda={"n_gpu_layers": 17}),
        )
        decision = BackendSelector(settings, _Probe(cuda=True)).select()
        self.assertEqual(decision.load_options["n_gpu_layers"], 17)

    def test_missing_cuda_fails_closed_with_backend_name(self) -> None:
        placement = DevicePlacement(
            ResourceSettings(cuda_vram_limit_gb=8), _Probe(cuda=False)
        )
        with self.assertRaises(PlacementError) as caught:
            placement.validate("cuda", 1024, n_gpu_layers=33)
        self.assertIn("cuda", str(caught.exception))

    def test_cuda_vram_limit_rejects_large_model(self) -> None:
        placement = DevicePlacement(
            ResourceSettings(cuda_vram_limit_gb=1), _Probe(cuda=True)
        )
        with self.assertRaises(PlacementError) as caught:
            placement.validate("cuda", 2 * 1024**3, n_gpu_layers=33)
        self.assertIn("cuda", str(caught.exception))

    def test_load_preflight_returns_503_when_cuda_is_unavailable(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        config_text = (ROOT / "local_llm.yaml").read_text(encoding="utf-8").replace(
            "default_backend: cpu", "default_backend: cuda"
        )
        config_path = root / "local_llm.yaml"
        config_path.write_text(config_text, encoding="utf-8")
        alias_dir = root / "models" / "qwen25-coder-05b"
        alias_dir.mkdir(parents=True)
        (alias_dir / "qwen2.5-coder-0.5b-instruct-q4_k_m.gguf").write_bytes(b"gguf")

        app = build_app(
            config_path,
            root=root,
            startup_download=False,
            backend_probes=_Probe(cuda=False),
            preflight=_NoPreflight(),
            instance_id="compute-test",
        )
        with TestClient(app) as client:
            response = client.post(
                "/v1/models/qwen25-coder-05b/chat/completions",
                json={"messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "resource_unavailable")
        self.assertIn("cuda", response.json()["error"]["message"])

    def test_one_runtime_instance_is_shared(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        app = build_app(
            ROOT / "local_llm.yaml",
            root=root,
            startup_download=False,
            backend_probes=_Probe(cuda=True),
            preflight=_NoPreflight(),
        )
        services = app.state.services
        self.assertIs(services.runtime, app.state.services.runtime)
        self.assertFalse(services.runtime.is_loaded())


if __name__ == "__main__":
    unittest.main()
