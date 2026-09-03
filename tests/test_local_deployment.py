import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.local_security import (
    DownloadTooLargeError,
    UnsafeRemoteURLError,
    ValidatingRedirectHandler,
    read_limited,
    validate_remote_url,
)
from backend.server import app
from backend.task_store import get_task, mark_unfinished_tasks_interrupted, save_task, update_task
from scripts.generate_translation_json import source_display_name


def resolver_for(address: str):
    return lambda *_args, **_kwargs: [(2, 1, 6, "", (address, 443))]


class LocalSecurityTests(unittest.TestCase):
    def test_generated_source_label_does_not_include_local_path(self) -> None:
        self.assertEqual("paper.pdf", source_display_name(Path("/Users/example/private/paper.pdf")))

    def test_remote_url_rejects_private_and_accepts_public_addresses(self) -> None:
        with self.assertRaises(UnsafeRemoteURLError):
            validate_remote_url("http://127.0.0.1/paper.pdf", resolver_for("127.0.0.1"))
        with self.assertRaises(UnsafeRemoteURLError):
            validate_remote_url("http://169.254.169.254/latest/meta-data", resolver_for("169.254.169.254"))
        self.assertEqual(
            "https://example.org/paper.pdf",
            validate_remote_url("https://example.org/paper.pdf", resolver_for("93.184.216.34")),
        )

    def test_redirect_target_is_revalidated(self) -> None:
        request = SimpleNamespace(full_url="https://example.org/paper.pdf")
        with patch("backend.local_security.socket.getaddrinfo", resolver_for("127.0.0.1")):
            with self.assertRaises(UnsafeRemoteURLError):
                ValidatingRedirectHandler().redirect_request(
                    request, None, 302, "Found", {}, "http://internal.local/paper.pdf"
                )

    def test_limited_reader_rejects_large_responses(self) -> None:
        response = SimpleNamespace(headers={"Content-Length": "12"}, read=io.BytesIO(b"%PDF-data").read)
        with self.assertRaises(DownloadTooLargeError):
            read_limited(response, 8)

    def test_limited_reader_rejects_chunked_responses_without_length(self) -> None:
        response = SimpleNamespace(headers={}, read=io.BytesIO(b"%PDF-data").read)
        with self.assertRaises(DownloadTooLargeError):
            read_limited(response, 8, chunk_size=3)

    def test_local_api_rejects_bad_host_and_cross_origin_writes(self) -> None:
        client = TestClient(app)
        self.assertEqual(400, client.get("/api/health", headers={"host": "attacker.test"}).status_code)
        blocked = client.post(
            "/api/check-paper-cache",
            data={"url": "https://arxiv.org/pdf/1706.03762"},
            headers={"origin": "https://attacker.test"},
        )
        self.assertEqual(403, blocked.status_code)
        allowed = client.post(
            "/api/check-paper-cache",
            data={"url": "https://arxiv.org/pdf/1706.03762"},
            headers={"origin": "http://127.0.0.1:8000"},
        )
        self.assertEqual(200, allowed.status_code)

    def test_upload_limit_is_enforced(self) -> None:
        client = TestClient(app)
        with patch.dict(os.environ, {"MAX_UPLOAD_MB": "0"}):
            response = client.post(
                "/api/upload-paper",
                files={"pdf": ("large.pdf", b"%PDF-data", "application/pdf")},
                data={"output_name": "large.pdf"},
            )
        self.assertEqual(413, response.status_code)

    def test_download_limit_is_reported_as_payload_too_large(self) -> None:
        client = TestClient(app)
        with patch(
            "backend.server.download_pdf_bytes",
            side_effect=DownloadTooLargeError("remote PDF is too large"),
        ):
            response = client.post(
                "/api/download-paper",
                data={"url": "https://example.org/paper.pdf"},
            )
        self.assertEqual(413, response.status_code)

    def test_single_origin_frontend_and_sample_are_served(self) -> None:
        client = TestClient(app)
        root = client.get("/", follow_redirects=False)
        self.assertEqual(307, root.status_code)
        self.assertEqual("/viewer/", root.headers["location"])
        self.assertEqual(200, client.get("/viewer/").status_code)
        sample = client.get("/viewer/translations/attention-is-all-you-need.sample.json")
        self.assertEqual(200, sample.status_code)
        self.assertEqual("Attention Is All You Need", sample.json()["title"])

    def test_deployment_files_exclude_secrets_and_bind_loopback(self) -> None:
        root = Path(__file__).resolve().parents[1]
        dockerignore = (root / ".dockerignore").read_text(encoding="utf-8")
        compose = (root / "compose.yaml").read_text(encoding="utf-8")
        requirements = (root / "requirements.txt").read_text(encoding="utf-8").lower()
        self.assertIn(".env", dockerignore.splitlines())
        self.assertIn("papers_to_translate", dockerignore.splitlines())
        self.assertIn('127.0.0.1:${APP_PORT:-8000}:8000', compose)
        self.assertIn("ghcr.io/untyee/paper-parallel-reader:latest", compose)
        self.assertNotIn("build:", compose)
        self.assertNotIn("docling", requirements)

    def test_release_bundle_and_launchers_are_configured(self) -> None:
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
        mac_launcher = root / "启动.command"
        windows_launcher = root / "启动.ps1"
        batch_launcher = root / "启动-Windows.bat"
        self.assertTrue(mac_launcher.stat().st_mode & 0o100)
        self.assertTrue(windows_launcher.is_file())
        self.assertTrue(batch_launcher.is_file())
        self.assertIn("runner: ubuntu-24.04-arm", workflow)
        self.assertIn("platform: linux/amd64", workflow)
        self.assertIn("platform: linux/arm64", workflow)
        self.assertIn("target: runtime-ocr", workflow)
        self.assertIn("docker buildx imagetools create", workflow)
        self.assertIn("git archive --format=zip", workflow)
        self.assertIn('tags:\n      - "v*"', workflow)

    def test_readme_contains_only_feature_and_deployment_sections(self) -> None:
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        headings = [line for line in readme.splitlines() if line.startswith("## ")]
        self.assertEqual(["## 功能说明", "## 部署教程"], headings)
        self.assertIn("启动.command", readme)
        self.assertIn("启动-Windows.bat", readme)


class TaskStoreTests(unittest.TestCase):
    def test_task_state_persists_and_unfinished_tasks_are_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "tasks.sqlite3"
            task = {
                "taskId": "task-1",
                "status": "queued",
                "revision": 0,
                "createdAt": "now",
                "updatedAt": "now",
                "progress": {"status": "queued", "completedBatches": 2},
            }
            save_task(path, task)
            update_task(path, "task-1", status="running")
            self.assertEqual("running", get_task(path, "task-1")["status"])
            self.assertEqual(1, mark_unfinished_tasks_interrupted(path))
            recovered = get_task(path, "task-1")
            self.assertEqual("interrupted", recovered["status"])
            self.assertEqual(2, recovered["progress"]["completedBatches"])

    def test_completed_task_is_not_marked_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "tasks.sqlite3"
            save_task(
                path,
                {
                    "taskId": "task-complete",
                    "status": "completed",
                    "revision": 1,
                    "createdAt": "now",
                    "updatedAt": "now",
                    "progress": {"status": "completed", "completedBatches": 5},
                },
            )
            self.assertEqual(0, mark_unfinished_tasks_interrupted(path))
            self.assertEqual("completed", get_task(path, "task-complete")["status"])


if __name__ == "__main__":
    unittest.main()
