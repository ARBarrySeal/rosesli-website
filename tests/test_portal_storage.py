"""Pytest suite for portal_storage abstraction.

Covers LocalStorage save / download / delete + the basename safety guard.
GCSStorage is exercised via a fake bucket so google-cloud-storage is NOT a
test-time dependency.
"""
import io

import pytest
from flask import Flask
from werkzeug.exceptions import Forbidden, NotFound

import portal_storage
from portal_storage import GCSStorage, LocalStorage, _safe_stored_name


# ─── Filename safety ──────────────────────────────────────────────────────────


def test_safe_stored_name_accepts_uuid_filename():
    with Flask(__name__).test_request_context():
        assert _safe_stored_name("abc123.pdf") == "abc123.pdf"


@pytest.mark.parametrize("bad", [
    "../etc/passwd",
    "..\\windows\\system32",
    "/etc/passwd",
    "subdir/file.pdf",
    ".hidden",
    "",
])
def test_safe_stored_name_rejects_traversal_or_hidden(bad):
    with Flask(__name__).test_request_context():
        with pytest.raises(Forbidden):
            _safe_stored_name(bad)


# ─── LocalStorage ──────────────────────────────────────────────────────────────


class _FlaskFile:
    """Mimics werkzeug.FileStorage.save(path) for upload tests."""
    def __init__(self, data: bytes):
        self.data = data

    def save(self, path: str):
        with open(path, "wb") as f:
            f.write(self.data)


def test_local_storage_save_creates_company_dir(tmp_path):
    backend = LocalStorage(str(tmp_path))
    backend.save("dod", "abc.pdf", _FlaskFile(b"hello"))
    assert (tmp_path / "dod" / "abc.pdf").read_bytes() == b"hello"


def test_local_storage_save_accepts_plain_file_like(tmp_path):
    backend = LocalStorage(str(tmp_path))
    backend.save("dod", "raw.bin", io.BytesIO(b"raw bytes"))
    assert (tmp_path / "dod" / "raw.bin").read_bytes() == b"raw bytes"


def test_local_storage_delete_is_idempotent(tmp_path):
    backend = LocalStorage(str(tmp_path))
    backend.save("dod", "x.pdf", _FlaskFile(b"x"))
    backend.delete("dod", "x.pdf")
    assert not (tmp_path / "dod" / "x.pdf").exists()
    backend.delete("dod", "x.pdf")  # second call must not raise


def test_local_storage_download_returns_attachment(tmp_path):
    backend = LocalStorage(str(tmp_path))
    backend.save("dod", "report.pdf", _FlaskFile(b"%PDF"))
    app = Flask(__name__)
    with app.test_request_context():
        resp = backend.download("dod", "report.pdf", "Quarterly Report.pdf")
        assert resp.status_code == 200
        assert "attachment" in resp.headers["Content-Disposition"]
        assert "Quarterly Report.pdf" in resp.headers["Content-Disposition"]


def test_local_storage_download_missing_file_404(tmp_path):
    backend = LocalStorage(str(tmp_path))
    app = Flask(__name__)
    with app.test_request_context():
        with pytest.raises(NotFound):
            backend.download("dod", "nonexistent.pdf", "x.pdf")


def test_local_storage_download_rejects_traversal(tmp_path):
    backend = LocalStorage(str(tmp_path))
    app = Flask(__name__)
    with app.test_request_context():
        with pytest.raises(Forbidden):
            backend.download("dod", "../escape.pdf", "escape.pdf")


# ─── GCSStorage (fake bucket — no google-cloud-storage required) ──────────────


class _FakeBlob:
    def __init__(self, store: dict, name: str):
        self._store = store
        self.name = name
        self.content_type = "application/octet-stream"

    def upload_from_file(self, fileobj, rewind=False):
        if rewind:
            fileobj.seek(0)
        self._store[self.name] = fileobj.read()

    def download_to_file(self, fileobj):
        fileobj.write(self._store[self.name])

    def exists(self):
        return self.name in self._store

    def delete(self):
        if self.name not in self._store:
            raise KeyError(self.name)
        del self._store[self.name]


class _FakeBucket:
    def __init__(self):
        self._store: dict = {}

    def blob(self, name: str):
        return _FakeBlob(self._store, name)


@pytest.fixture
def gcs_backend(monkeypatch):
    backend = GCSStorage.__new__(GCSStorage)
    backend._bucket = _FakeBucket()
    backend._bucket_name = "test-bucket"
    backend._client = None
    return backend


def test_gcs_storage_save_and_blob_naming(gcs_backend):
    gcs_backend.save("dod", "abc.pdf", io.BytesIO(b"contents"))
    assert "dod/abc.pdf" in gcs_backend._bucket._store
    assert gcs_backend._bucket._store["dod/abc.pdf"] == b"contents"


def test_gcs_storage_download_returns_blob(gcs_backend):
    gcs_backend.save("rosesli", "x.pdf", io.BytesIO(b"abc"))
    app = Flask(__name__)
    with app.test_request_context():
        resp = gcs_backend.download("rosesli", "x.pdf", "Report.pdf")
        assert resp.status_code == 200
        assert "Report.pdf" in resp.headers["Content-Disposition"]


def test_gcs_storage_download_missing_blob_404(gcs_backend):
    app = Flask(__name__)
    with app.test_request_context():
        with pytest.raises(NotFound):
            gcs_backend.download("dod", "missing.pdf", "x.pdf")


def test_gcs_storage_delete_idempotent(gcs_backend):
    gcs_backend.save("dod", "x.pdf", io.BytesIO(b"a"))
    gcs_backend.delete("dod", "x.pdf")
    gcs_backend.delete("dod", "x.pdf")  # missing blob is fine
    assert "dod/x.pdf" not in gcs_backend._bucket._store


# ─── Backend resolution ───────────────────────────────────────────────────────


def test_get_backend_defaults_to_local(monkeypatch):
    monkeypatch.delenv("GCS_BUCKET", raising=False)
    portal_storage.reset_backend_for_tests()
    assert isinstance(portal_storage.get_backend(), LocalStorage)
    portal_storage.reset_backend_for_tests()


def test_get_backend_uses_gcs_when_bucket_env_set(monkeypatch):
    """Sanity check: backend selection reads GCS_BUCKET env."""
    monkeypatch.setenv("GCS_BUCKET", "some-bucket")
    portal_storage.reset_backend_for_tests()
    # Don't actually instantiate (would need google-cloud-storage); just
    # confirm the selection logic by mocking the class.
    original = portal_storage.GCSStorage
    instantiated = []

    class _StubGCS:
        def __init__(self, name):
            instantiated.append(name)

    monkeypatch.setattr(portal_storage, "GCSStorage", _StubGCS)
    portal_storage.reset_backend_for_tests()
    backend = portal_storage.get_backend()
    assert instantiated == ["some-bucket"]
    assert isinstance(backend, _StubGCS)
    monkeypatch.setattr(portal_storage, "GCSStorage", original)
    portal_storage.reset_backend_for_tests()
