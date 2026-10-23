"""FastAPI route smoke tests; yt-dlp is faked so tests require no network."""
from __future__ import annotations

import importlib
import sys
import time
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


class FakeYoutubeDL:
    def __init__(self, options=None):
        self.options = options or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, url, download=False):
        return {
            "title": "تجربة فيديو", "duration": 61, "extractor_key": "Youtube",
            "formats": [
                {"height": 720, "vcodec": "avc1"},
                {"height": 1080, "vcodec": "avc1"},
                {"height": None, "vcodec": "none"},
            ],
        }

    def download(self, urls):
        destination = Path(self.options["outtmpl"].replace("%(ext)s", "mp4"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.options["progress_hooks"][0]({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100})
        destination.write_bytes(b"fake-mp4-test-content")
        self.options["progress_hooks"][0]({"status": "finished"})


@pytest.fixture(scope="module")
def module(tmp_path_factory):
    fake = types.ModuleType("yt_dlp")
    fake.YoutubeDL = FakeYoutubeDL
    utils = types.ModuleType("yt_dlp.utils")
    utils.DownloadError = type("DownloadError", (Exception,), {})
    sys.modules["yt_dlp"] = fake
    sys.modules["yt_dlp.utils"] = utils
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    mod = importlib.import_module("app")
    mod.DOWNLOAD_DIR = tmp_path_factory.mktemp("videos")
    return mod


@pytest.fixture
def client(module):
    return TestClient(module.app)


def test_landing_page_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "لقطة" in response.text


def test_valid_and_invalid_hosts(module):
    assert module.verified_url("https://youtu.be/sample") == "https://youtu.be/sample"
    for malicious in ("http://127.0.0.1:8080/", "http://youtube.com.attacker.tld/", "ftp://youtube.com/a", "http://user:pass@youtube.com/a"):
        with pytest.raises(Exception):
            module.verified_url(malicious)


def test_info(client):
    result = client.post("/api/info", json={"url": "https://www.youtube.com/watch?v=abc"})
    assert result.status_code == 200
    assert result.json()["qualities"] == [1080, 720]
    assert result.json()["title"] == "تجربة فيديو"


def test_download_and_file(client, module):
    response = client.post("/api/download", json={"url": "https://www.youtube.com/watch?v=abc", "quality": "1080", "format": "mp4"})
    assert response.status_code == 202
    job_id = response.json()["id"]
    for _ in range(80):
        status = client.get(f"/api/jobs/{job_id}")
        if status.json()["status"] == "ready":
            break
        time.sleep(0.05)
    assert status.json()["status"] == "ready"
    media = client.get(f"/api/jobs/{job_id}/file")
    assert media.status_code == 200
    assert media.content == b"fake-mp4-test-content"
    assert media.headers["content-disposition"].startswith("attachment;")


def test_format_validation(client):
    response = client.post("/api/download", json={"url": "https://youtu.be/abc", "quality": "wrong"})
    assert response.status_code == 400
    response = client.get("/api/jobs/not-a-valid-id/file")
    assert response.status_code == 404
