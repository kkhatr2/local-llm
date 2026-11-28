import re
import shutil
import subprocess
import unittest
from pathlib import Path
from urllib.parse import urlparse

import yaml


BASE_DIGEST = "39b820af05f0f9bb986ca145a0d077bbd3bd866256277dd869fefb93bd03cd21"
BASE_PLATFORM = "linux/amd64"


class DependencyLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.lock_path = Path("/app/environment-linux-64.lock")
        cls.environment_path = Path("/app/environment.yaml")
        cls.dockerfile_path = Path("/app/Dockerfile")
        cls.compose_path = Path("/app/docker-compose.yml")
        cls.lock_text = cls.lock_path.read_text(encoding="utf-8")
        cls.lines = cls.lock_text.splitlines()
        cls.urls = [line for line in cls.lines if line.startswith("https://")]
        cls.installed = cls._installed_packages()

    @staticmethod
    def _installed_packages() -> dict[str, str]:
        result = subprocess.run(
            ["conda", "list", "-n", "local-llm"],
            capture_output=True,
            text=True,
            check=True,
        )
        installed: dict[str, str] = {}
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and not line.startswith("#"):
                installed[parts[0]] = parts[1]
        return installed

    def test_lock_is_explicit_hashed_linux64(self) -> None:
        self.assertIn("@EXPLICIT", self.lines)
        self.assertIn("# platform: linux-64", self.lines)
        self.assertGreaterEqual(len(self.urls), 100)
        for url in self.urls:
            with self.subTest(url=url):
                self.assertRegex(url, r"^https://\S+#[0-9a-f]{64}$")
                parts = [part for part in urlparse(url).path.split("/") if part]
                self.assertGreaterEqual(len(parts), 3)
                self.assertIn(parts[-2], {"linux-64", "noarch"})
                self.assertRegex(parts[-1], r"^.+\.(?:conda|tar\.bz2)$")

    def test_lock_covers_every_declared_dependency(self) -> None:
        config = yaml.safe_load(self.environment_path.read_text(encoding="utf-8"))
        declared = [dependency for dependency in config["dependencies"] if isinstance(dependency, str)]
        self.assertTrue(declared)
        for dependency in declared:
            match = re.match(r"^([A-Za-z0-9_.-]+)", dependency)
            self.assertIsNotNone(match, f"cannot parse dependency {dependency!r}")
            name = match.group(1)
            version = self.installed.get(name)
            self.assertIsNotNone(version, f"{name} is not installed")
            with self.subTest(dependency=dependency):
                self.assertTrue(
                    any(f"/{name}-{version}-" in url for url in self.urls),
                    f"{name} {version} is missing from the lock",
                )

            constraint = dependency[match.end() :].strip()
            if constraint.startswith("="):
                expected = constraint.lstrip("=").strip().rstrip("*").rstrip(".")
                self.assertTrue(expected)
                self.assertTrue(
                    version == expected or version.startswith(f"{expected}."),
                    f"installed {name} {version} does not satisfy {dependency}",
                )

    def test_dockerfile_installs_only_from_exact_lock(self) -> None:
        content = self.dockerfile_path.read_text(encoding="utf-8")
        from_lines = [line for line in content.splitlines() if line.startswith("FROM ")]
        self.assertEqual(len(from_lines), 1)
        self.assertIn(f"@sha256:{BASE_DIGEST}", from_lines[0])
        self.assertNotIn("linux/arm64", content)
        self.assertIn("COPY environment-linux-64.lock /app/environment-linux-64.lock", content)
        self.assertRegex(
            content,
            r"conda create .*--file /app/environment-linux-64\.lock",
        )
        self.assertNotIn("conda env create -f", content)
        self.assertNotIn("pip install --upgrade pip", content)

    def test_regenerated_lock_matches_committed_lock(self) -> None:
        result = subprocess.run(
            ["conda", "list", "-n", "local-llm", "--explicit", "--sha256"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout, self.lock_text)

    def test_compose_targets_base_platform(self) -> None:
        config = yaml.safe_load(self.compose_path.read_text(encoding="utf-8"))
        services = config["services"]
        self.assertTrue(services)
        for name, service in services.items():
            with self.subTest(service=name):
                self.assertEqual(service.get("platform"), BASE_PLATFORM)

    def test_installed_versions_match_lock_and_hf_cli_exists(self) -> None:
        for name in ("python", "huggingface_hub", "llama-cpp-python"):
            with self.subTest(package=name):
                self.assertIn(name, self.installed)
                version = self.installed[name]
                self.assertTrue(
                    any(f"/{name}-{version}-" in url for url in self.urls),
                    f"{name} {version} does not match the lock",
                )
        self.assertIsNotNone(shutil.which("hf"), "hf CLI entry point must exist")


if __name__ == "__main__":
    unittest.main()
