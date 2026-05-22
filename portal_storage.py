"""Backend-pluggable document storage for the portal.

Two backends, selected at first use by the GCS_BUCKET env var:
  * LocalStorage — writes to ./uploads/<company>/<filename>. Default for dev.
  * GCSStorage   — writes to gs://<GCS_BUCKET>/<company>/<filename>. REQUIRED
                   for Cloud Run, whose container FS is ephemeral and is NOT
                   shared across instances. Without GCS, every redeploy or
                   scale-out drops user-uploaded documents on the floor.

Module-level entry points (save/download/delete) delegate to the backend that
get_backend() resolves on first call.

Service-account setup (one-time, prod):
    BUCKET=dodcyber-portal-docs   # or rosesli-portal-docs
    SA=$(gcloud run services describe my-website --region us-central1 \\
            --format='value(spec.template.spec.serviceAccountName)')
    gcloud storage buckets create "gs://$BUCKET" --location=us-central1 \\
            --uniform-bucket-level-access
    gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \\
            --member="serviceAccount:$SA" --role=roles/storage.objectAdmin
    gcloud run services update my-website --region us-central1 \\
            --update-env-vars "GCS_BUCKET=$BUCKET"
"""
import io
import os
from abc import ABC, abstractmethod
from typing import BinaryIO

from flask import Response, abort, send_file


def _safe_stored_name(stored_name: str) -> str:
    """Reject any stored_name that isn't a flat, non-dotfile basename.

    stored_name is generated as uuid.uuid4().hex + ext, so it should never
    contain separators or leading dots. We reject loudly (403) rather than
    silently stripping path components — surfacing bad data is the goal.
    """
    if (
        not stored_name
        or stored_name in (".", "..")
        or "/" in stored_name
        or "\\" in stored_name
        or stored_name.startswith(".")
    ):
        abort(403)
    return stored_name


class StorageBackend(ABC):
    @abstractmethod
    def save(self, company: str, stored_name: str, fileobj: BinaryIO) -> None: ...

    @abstractmethod
    def download(self, company: str, stored_name: str, original_name: str) -> Response: ...

    @abstractmethod
    def delete(self, company: str, stored_name: str) -> None: ...


class LocalStorage(StorageBackend):
    def __init__(self, base_dir: str):
        self.base_dir = os.path.abspath(base_dir)
        self.base_real = os.path.realpath(self.base_dir)

    def _path(self, company: str, stored_name: str) -> str:
        return os.path.join(self.base_dir, company, _safe_stored_name(stored_name))

    def save(self, company, stored_name, fileobj):
        company_dir = os.path.join(self.base_dir, company)
        os.makedirs(company_dir, exist_ok=True)
        # Flask's FileStorage.save writes via shutil; plain file-likes need write().
        save = getattr(fileobj, "save", None)
        if callable(save):
            save(self._path(company, stored_name))
        else:
            with open(self._path(company, stored_name), "wb") as out:
                out.write(fileobj.read())

    def download(self, company, stored_name, original_name):
        path = os.path.realpath(self._path(company, stored_name))
        if not path.startswith(self.base_real + os.sep):
            abort(403)
        if not os.path.isfile(path):
            abort(404)
        return send_file(path, download_name=original_name, as_attachment=True)

    def delete(self, company, stored_name):
        try:
            os.remove(self._path(company, stored_name))
        except OSError:
            pass  # idempotent


class GCSStorage(StorageBackend):
    def __init__(self, bucket_name: str):
        # Lazy import — dev environments without google-cloud-storage installed
        # should still be able to import portal_storage as long as GCS_BUCKET
        # is unset.
        from google.cloud import storage  # noqa: WPS433
        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket_name)
        self._bucket_name = bucket_name

    def _blob_name(self, company: str, stored_name: str) -> str:
        return f"{company}/{_safe_stored_name(stored_name)}"

    def save(self, company, stored_name, fileobj):
        blob = self._bucket.blob(self._blob_name(company, stored_name))
        blob.upload_from_file(fileobj, rewind=True)

    def download(self, company, stored_name, original_name):
        blob = self._bucket.blob(self._blob_name(company, stored_name))
        if not blob.exists():
            abort(404)
        buf = io.BytesIO()
        blob.download_to_file(buf)
        buf.seek(0)
        return send_file(
            buf,
            download_name=original_name,
            as_attachment=True,
            mimetype=blob.content_type or "application/octet-stream",
        )

    def delete(self, company, stored_name):
        blob = self._bucket.blob(self._blob_name(company, stored_name))
        try:
            blob.delete()
        except Exception:  # NotFound + transient — keep delete idempotent
            pass


_backend: StorageBackend | None = None


def get_backend() -> StorageBackend:
    global _backend
    if _backend is None:
        bucket = os.environ.get("GCS_BUCKET", "").strip()
        if bucket:
            _backend = GCSStorage(bucket)
        else:
            default_base = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "uploads"
            )
            _backend = LocalStorage(default_base)
    return _backend


def reset_backend_for_tests() -> None:
    """Re-resolve the backend; tests use this when toggling env vars."""
    global _backend
    _backend = None


def save(company: str, stored_name: str, fileobj: BinaryIO) -> None:
    get_backend().save(company, stored_name, fileobj)


def download(company: str, stored_name: str, original_name: str) -> Response:
    return get_backend().download(company, stored_name, original_name)


def delete(company: str, stored_name: str) -> None:
    get_backend().delete(company, stored_name)
