import re
import unittest
from pathlib import Path

import yaml

FALLBACK_ROOT = Path(__file__).resolve().parents[1]
REPO = Path("/workspace") if Path("/workspace").is_dir() else FALLBACK_ROOT

_DIGEST = re.compile(r"^[a-z0-9._/-]+@sha256:[0-9a-f]{64}$")


def _text(relative: str) -> str:
    return (REPO / relative).read_text(encoding="utf-8")


def _yaml(relative: str):
    return yaml.safe_load(_text(relative))


class Phase2ArtifactTests(unittest.TestCase):
    def test_digest_pins_are_consistent(self) -> None:
        pins = _yaml("deploy/image-digests.yaml")
        values = _yaml("helm/local-llm/values.yaml")
        compose = _yaml("docker-compose.yml")
        tf = _text("terraform/aws/variables.tf")

        for flavor in ("cpu", "cuda"):
            digest = pins[flavor]
            self.assertRegex(digest, _DIGEST)
            self.assertEqual(values["image"][f"{flavor}Digest"], digest)
            self.assertEqual(
                compose["x-phase2-image-digests"][flavor].strip('"'), digest
            )
            self.assertIn(f'default     = "{digest}"', tf)

    def test_compose_remains_loopback_only(self) -> None:
        compose = _text("docker-compose.yml")
        self.assertIn('"127.0.0.1:8000:8000"', compose)
        self.assertNotIn('"0.0.0.0:8000:8000"', compose)
        self.assertNotIn('"8000:8000"', compose)

    def test_images_have_no_model_token_or_cache_copy(self) -> None:
        dockerignore = _text(".dockerignore")
        for ignored in ("private.txt", "models/", ".cache/"):
            self.assertIn(ignored, dockerignore)
        for dockerfile in ("Dockerfile", "Dockerfile.cuda"):
            text = _text(dockerfile)
            self.assertIn("@sha256:", text.splitlines()[0])
            self.assertNotIn("COPY private.txt", text)
            self.assertNotIn("COPY models", text)
            self.assertNotIn("COPY .cache", text)
        self.assertTrue((REPO / "environment-cuda-64.lock").is_file())

    def test_helm_gateway_is_explicit_and_restricted(self) -> None:
        values = _text("helm/local-llm/values.yaml")
        self.assertIn("enabled: false", values)
        gateway = _text("helm/local-llm/templates/gateway.yaml")
        self.assertIn("{{- if .Values.gateway.enabled }}", gateway)
        self.assertIn("missingKeyStatus: 401", gateway)
        self.assertIn("invalidKeyStatus: 401", gateway)
        self.assertIn("forwardKeyValue: false", gateway)
        self.assertIn("value: /health", gateway)
        self.assertIn("value: {{ .Values.gateway.allowedAliasPrefix }}", gateway)
        self.assertNotIn("value: /v1/models\n", gateway)
        self.assertNotIn("/admin", gateway)
        helpers = _text("helm/local-llm/templates/_helpers.tpl")
        self.assertIn("image.flavor must be 'cpu' or 'cuda'", helpers)
        self.assertIn("RequestRedirect", gateway)
        self.assertIn('local-llm.io/min-tls-version: "1.2"', gateway)
        self.assertIn("certificateRefs", gateway)

    def test_cloud_pod_is_hardened(self) -> None:
        deployment = _text("helm/local-llm/templates/deployment.yaml")
        for token in (
            "runAsNonRoot: {{ .Values.security.runAsNonRoot }}",
            "readOnlyRootFilesystem: {{ .Values.security.readOnlyRootFilesystem }}",
            "allowPrivilegeEscalation: {{ .Values.security.allowPrivilegeEscalation }}",
            'drop: ["ALL"]',
            "type: {{ .Values.security.seccompProfile }}",
            "mountPath: /app/models",
            "mountPath: /app/.cache/responses",
            "mountPath: /tmp",
        ):
            self.assertIn(token, deployment)
        self.assertNotIn("hostPath", deployment)
        network = _text("helm/local-llm/templates/networkpolicy.yaml")
        self.assertIn("kind: NetworkPolicy", network)
        self.assertIn("policyTypes: [\"Ingress\", \"Egress\"]", network)

    def test_secrets_are_references_not_values(self) -> None:
        deployment = _text("helm/local-llm/templates/deployment.yaml")
        gateway = _text("helm/local-llm/templates/gateway.yaml")
        self.assertIn("name: local-llm-hf", deployment)
        self.assertIn("key: token", deployment)
        self.assertIn("name: {{ .Values.gateway.apiKeySecretRef }}", gateway)
        self.assertIn("name: {{ .Values.modelSupply.credentialsSecretRef }}", deployment)
        self.assertNotIn("hf_", _text("local_llm.yaml"))
        self.assertNotIn("sk-", _text("local_llm.yaml"))

    def test_application_logs_do_not_disclose_request_data(self) -> None:
        forbidden = ("messages", "prompt", "HF_TOKEN", "traceback", "exc_info")
        for path in (REPO / "local_llm").rglob("*.py"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if re.search(r"\b(?:LOGGER|logger)\.(?:info|error|warning|exception)", line):
                    lowered = line.lower()
                    for token in forbidden:
                        self.assertNotIn(token.lower(), lowered)
        app_source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (REPO / "local_llm").rglob("*.py")
        )
        self.assertNotIn("logger.exception", app_source)

    def test_terraform_is_pinned_and_provisions_phase2(self) -> None:
        versions = _text("terraform/aws/versions.tf")
        main = _text("terraform/aws/main.tf")
        for token in (
            'version = "~> 5.61.0"',
            'version = "~> 2.30.0"',
            'version = "~> 2.13.0"',
            'version = "5.13.0"',
            'version = "20.24.0"',
        ):
            self.assertIn(token, versions + main)
        self.assertIn("cpu = {", main)
        self.assertIn("cuda = {", main)
        self.assertIn('resource "aws_s3_bucket" "weights"', main)
        self.assertIn('name  = "gateway.enabled"', main)
        self.assertIn('name  = "networkPolicy.allowedNamespaces[0]"', main)
        self.assertNotIn(":latest", main)

    def test_supply_chain_gates_are_declared(self) -> None:
        text = _text("deploy/supply-chain.yaml")
        for token in ("cosign verify", "syft", "grype", "--fail-on high"):
            self.assertIn(token, text)

    def test_open_decisions_are_not_silently_closed(self) -> None:
        security = _text("docs/security-report.md")
        requirements = _text("docs/requirements.md")
        self.assertIn("ASSUMPTION-17` remains open", security)
        self.assertIn("rate limiting", security.lower())
        self.assertIn("ASSUMPTION-18` remains open", requirements)
        self.assertIn("ASSUMPTION-19` remains open", requirements)
        self.assertIn("ASSUMPTION-21` remains open", requirements)
        self.assertIn("Apple Metal is documentation-only", requirements)
        self.assertIn("documentation-only", security.lower())

    def test_config_file_is_source_for_helm(self) -> None:
        source = _text("local_llm.yaml")
        copied = _text("helm/local-llm/files/local_llm.yaml")
        self.assertEqual(source, copied)
        configmap = _text("helm/local-llm/templates/configmap.yaml")
        self.assertIn('.Files.Get "files/local_llm.yaml"', configmap)


if __name__ == "__main__":
    unittest.main()
