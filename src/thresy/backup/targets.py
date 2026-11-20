"""Backup targets. They only ever *create* objects.

The storage credential may create objects but not read, list, overwrite or delete them, so a
target offers exactly one operation, :meth:`Target.put`: create an object under a name that
does not exist yet. Per-run objects have names that are unique per run; FIT files keep stable
names, and "already there" (Google Cloud Storage answers 412 to a create with
``ifGenerationMatch=0``; a local file exists) counts as success.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import unquote, urlparse


class TargetError(RuntimeError):
    """The target is misconfigured or refused an upload."""


class Target(Protocol):
    url: str

    def put(self, name: str, source: Path) -> bool:
        """Create object ``name`` from the file ``source``. True: created; False: an object
        of that name already existed (it is left as it is). Raises TargetError."""
        ...


def _check_name(name: str) -> str:
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or ".." in path.parts or name.endswith("/"):
        raise TargetError(f"invalid object name {name!r}")
    return name


class LocalTarget:
    """``file:///path``: a directory (another disk, a mounted share). Files are created
    exclusively and never overwritten, like objects in the bucket."""

    def __init__(self, root: Path, url: str | None = None) -> None:
        self.root = root
        self.url = url or root.as_uri()

    def put(self, name: str, source: Path) -> bool:
        target = self.root / _check_name(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.partial-{os.getpid()}")
        try:
            with source.open("rb") as src, tmp.open("xb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
            try:
                os.link(tmp, target)  # fails if the name exists: never overwrite
            except FileExistsError:
                return False
            except OSError:
                # A file system without hard links (some shares): check, then rename. There is
                # only one writer, the backup itself.
                if target.exists():
                    return False
                tmp.rename(target)
            return True
        except OSError as exc:
            raise TargetError(f"cannot write {target}: {exc.strerror or exc}") from exc
        finally:
            tmp.unlink(missing_ok=True)


class GcsTarget:
    """``gs://bucket/prefix``: Google Cloud Storage, through a credential with the role
    ``roles/storage.objectCreator`` only. Uploads use ``ifGenerationMatch=0`` (create only)
    and never read the bucket or its objects."""

    def __init__(self, bucket: str, prefix: str, client: Any, url: str) -> None:
        self.bucket_name = bucket
        self.prefix = prefix.strip("/")
        self.client = client
        self.url = url
        self._bucket = None

    def object_name(self, name: str) -> str:
        _check_name(name)
        return f"{self.prefix}/{name}" if self.prefix else name

    def put(self, name: str, source: Path) -> bool:
        from google.api_core import exceptions

        if self._bucket is None:
            # client.bucket() only builds a reference; get_bucket() would read the bucket.
            self._bucket = self.client.bucket(self.bucket_name)
        blob = self._bucket.blob(self.object_name(name))
        try:
            blob.upload_from_filename(
                str(source),
                content_type="application/octet-stream",
                if_generation_match=0,
                checksum="crc32c",
                timeout=600,
            )
        except exceptions.PreconditionFailed:
            return False
        except exceptions.Forbidden as exc:
            raise TargetError(
                f"upload of {self.object_name(name)} refused (403): the credential needs the "
                f"role roles/storage.objectCreator on gs://{self.bucket_name} ({exc.message})"
            ) from exc
        except exceptions.NotFound as exc:
            raise TargetError(f"bucket gs://{self.bucket_name} not found") from exc
        except exceptions.GoogleAPICallError as exc:
            raise TargetError(f"upload of {self.object_name(name)} failed: {exc}") from exc
        return True


def gcs_client(credentials_file: Path | None) -> Any:
    """A storage client from a service-account key file, or Application Default Credentials.
    Creating it makes no request."""
    try:
        from google.cloud import storage
        from google.oauth2 import service_account
    except ImportError as exc:  # pragma: no cover - a declared dependency
        raise TargetError(f"google-cloud-storage is not installed: {exc}") from exc
    if (
        credentials_file is not None
        and credentials_file.exists()
        and credentials_file.stat().st_size == 0
    ):
        credentials_file = None  # the Compose default mounts /dev/null: no key configured
    if credentials_file is not None:
        try:
            credentials = service_account.Credentials.from_service_account_file(
                str(credentials_file),
                scopes=["https://www.googleapis.com/auth/devstorage.read_write"],
            )
        except (OSError, ValueError) as exc:
            raise TargetError(
                f"cannot use the service-account key {credentials_file}: {exc}"
            ) from exc
        return storage.Client(project=credentials.project_id, credentials=credentials)
    try:
        return storage.Client()
    except Exception as exc:  # google.auth.exceptions.DefaultCredentialsError and friends
        raise TargetError(
            "no credential for Google Cloud Storage: set COACH_BACKUP_GCS_CREDENTIALS to a "
            f"service-account key file ({exc})"
        ) from exc


def open_target(url: str, *, gcs_credentials: Path | None = None, client: Any = None) -> Target:
    """The target for ``COACH_BACKUP_URL``. ``client`` replaces the storage client (tests)."""
    parsed = urlparse(url)
    if parsed.scheme == "file":
        if parsed.netloc not in ("", "localhost"):
            raise TargetError(f"file URLs must be file:///absolute/path, got {url!r}")
        path = Path(unquote(parsed.path))
        if not path.is_absolute():
            raise TargetError(f"file URLs need an absolute path, got {url!r}")
        return LocalTarget(path, url)
    if parsed.scheme == "gs":
        if not parsed.netloc:
            raise TargetError(f"gs URLs need a bucket: gs://bucket/prefix, got {url!r}")
        return GcsTarget(
            parsed.netloc,
            parsed.path,
            client if client is not None else gcs_client(gcs_credentials),
            url.rstrip("/"),
        )
    raise TargetError(f"COACH_BACKUP_URL must start with file:// or gs://, got {url!r}")
