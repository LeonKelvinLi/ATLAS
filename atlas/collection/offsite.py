"""Create-only GCS backup upload and exact-generation downloaded verification.

This independent process has no broker imports or credentials. Authentication
uses the VM metadata access token only; no token is persisted or logged.
Official protocols: https://docs.cloud.google.com/storage/docs/json_api/v1/objects/insert
and https://docs.cloud.google.com/storage/docs/request-preconditions .
"""

from dataclasses import dataclass, field
import base64
import gzip
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import tarfile
import tempfile
import time
from urllib.parse import quote, urlencode, urlsplit

from atlas.collection import backup as local
from atlas.collection.state import file_lock


CHUNK = 1024 * 1024
JSON_LIMIT = 32768
BUCKET = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")
BACKUP_NAME = re.compile(r"atlas-backup-\d{8}T\d{6}Z-[a-f0-9]{16}")
TOKEN_URL = "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token"


class OffsiteError(ValueError):
    """Fixed diagnostics never include request bodies, headers or credentials."""


@dataclass(frozen=True)
class HTTPResult:
    status: int
    body: bytes = field(default=b"", repr=False)
    headers: dict = field(default_factory=dict, repr=False)


def _json(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    try:
        if type(raw) is not bytes or len(raw) > JSON_LIMIT:
            raise ValueError
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise OffsiteError("offsite_response_json_invalid") from None


def _space(root, minimum, needed=0):
    if shutil.disk_usage(root).free < minimum + needed:
        raise OffsiteError("offsite_disk_reserve_insufficient")


def _new(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                            getattr(os, "O_NOFOLLOW", 0), 0o600), "wb")


def _hashes(path):
    size, sha, md5 = 0, hashlib.sha256(), hashlib.md5(usedforsecurity=False)
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK):
            size += len(chunk)
            sha.update(chunk)
            md5.update(chunk)
    return {"size": size, "sha256": sha.hexdigest(), "md5": base64.b64encode(md5.digest()).decode()}


def _http(method, url, *, headers=None, source=None, sink=None, max_bytes=JSON_LIMIT,
          reserve=None):
    """One bounded request, no proxy environment, redirects or automatic retry."""
    parsed = urlsplit(url)
    metadata = url == TOKEN_URL and method == "GET" and source is None and sink is None
    storage = (parsed.scheme == "https" and parsed.netloc == "storage.googleapis.com"
               and not parsed.fragment and ((method == "POST" and parsed.path.startswith("/upload/storage/v1/b/"))
                   or (method == "GET" and parsed.path.startswith("/storage/v1/b/"))))
    if not (metadata or storage):
        raise OffsiteError("offsite_endpoint_refused")
    connection = (http.client.HTTPConnection("169.254.169.254", timeout=30) if metadata else
                  http.client.HTTPSConnection("storage.googleapis.com", timeout=30, context=ssl.create_default_context()))
    request_headers = dict(headers or {})
    started = time.monotonic()
    try:
        if source is None:
            connection.request(method, parsed.path + ("?" + parsed.query if parsed.query else ""), headers=request_headers)
        else:
            request_headers["Content-Length"] = str(source.stat().st_size)
            with source.open("rb") as stream:
                connection.request(method, parsed.path + "?" + parsed.query, body=stream, headers=request_headers)
        response = connection.getresponse()
        safe_headers = {key.lower(): value for key, value in response.getheaders()
                        if key.lower() in {"metadata-flavor", "content-length", "content-encoding", "x-goog-generation"}}
        if sink is not None and response.status == 200:
            count = 0
            with _new(sink) as output:
                while chunk := response.read(min(CHUNK, max_bytes + 1 - count)):
                    count += len(chunk)
                    if count > max_bytes or time.monotonic() - started > 900:
                        raise OffsiteError("offsite_download_bound_exceeded")
                    if reserve:
                        _space(*reserve, needed=len(chunk))
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            body = b""
        else:
            body = response.read(JSON_LIMIT + 1)
            if len(body) > JSON_LIMIT:
                raise OffsiteError("offsite_http_response_too_large")
        return HTTPResult(response.status, body, safe_headers)
    except OffsiteError:
        raise
    except (OSError, http.client.HTTPException, ValueError):
        raise OffsiteError("offsite_transport_failed") from None
    finally:
        connection.close()


def metadata_token(*, transport=None):
    response = (_http if transport is None else transport)(
        "GET", TOKEN_URL, headers={"Metadata-Flavor": "Google"}, max_bytes=JSON_LIMIT)
    if response.status != 200 or response.headers.get("metadata-flavor") != "Google":
        raise OffsiteError("offsite_metadata_token_unavailable")
    value = _json(response.body)
    if (type(value) is not dict or not {"access_token", "expires_in", "token_type"} <= value.keys()
            or value["token_type"] != "Bearer" or type(value["expires_in"]) is not int
            or not 30 <= value["expires_in"] <= 86400
            or type(value["access_token"]) is not str
            or not re.fullmatch(r"[!-~]{1,8192}", value["access_token"])):
        raise OffsiteError("offsite_metadata_token_invalid")
    return value["access_token"]


class _CheckedSource:
    def __init__(self, stream, entry, work, minimum):
        self.stream, self.entry, self.work, self.minimum = stream, entry, work, minimum
        self.sha, self.size = hashlib.sha256(), 0
    def read(self, count):
        _space(self.work, self.minimum)
        data = self.stream.read(min(count, CHUNK))
        self.size += len(data)
        self.sha.update(data)
        return data
    def verify(self):
        if self.size != self.entry["size"] or self.sha.hexdigest() != self.entry["sha256"] or self.stream.read(1):
            raise OffsiteError("offsite_source_changed_during_package")


def _package(snapshot, manifest, destination, minimum):
    raw_manifest = local._read(snapshot / "manifest.json", local.MAX_MANIFEST_BYTES)
    entries = [{"path": "manifest.json", "size": len(raw_manifest),
                "sha256": hashlib.sha256(raw_manifest).hexdigest()}] + [
        {**entry, "path": "payload/" + entry["path"]} for entry in manifest["files"]]
    with _new(destination) as output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0, compresslevel=6) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                for entry in sorted(entries, key=lambda row: row["path"]):
                    path = snapshot / entry["path"]
                    local._trusted(path)
                    header = tarfile.TarInfo(entry["path"])
                    header.size, header.mode, header.mtime = entry["size"], 0o600, 0
                    with path.open("rb") as stream:
                        source = _CheckedSource(stream, entry, destination.parent, minimum)
                        archive.addfile(header, source)
                        source.verify()
        output.flush()
        os.fsync(output.fileno())
    # Inventory and manifest must still describe the source after reading it.
    if local.verify_backup(snapshot) != manifest:
        raise OffsiteError("offsite_source_changed_during_package")
    return entries


class _TarBudget:
    def __init__(self, stream, maximum):
        self.stream, self.remaining = stream, maximum
    def read(self, count=-1):
        if count < 0 or count > CHUNK or count > self.remaining:
            raise OffsiteError("offsite_tar_read_bound_exceeded")
        result = self.stream.read(count)
        self.remaining -= len(result)
        return result


def _extract(package, destination, entries, maximum, minimum):
    """Never extract arbitrary paths, links, devices, metadata or extra files."""
    expected = {entry["path"]: entry for entry in entries}
    seen = set()
    destination.mkdir(mode=0o700)
    with gzip.open(package, "rb") as compressed:
        bounded = _TarBudget(compressed, maximum)
        with tarfile.open(fileobj=bounded, mode="r|") as archive:
            for member in archive:
                if (not member.isfile() or member.name not in expected or member.name in seen
                        or member.linkname or member.size != expected[member.name]["size"]
                        or member.mode != 0o600 or member.uid != 0 or member.gid != 0
                        or any(key not in {"path"} for key in member.pax_headers)):
                    raise OffsiteError("offsite_tar_member_refused")
                local._relative(member.name)
                target = destination / member.name
                # Parents are only beneath this new private extraction directory.
                parent = target.parent
                chain = []
                while parent != destination:
                    chain.append(parent)
                    parent = parent.parent
                for parent in reversed(chain):
                    parent.mkdir(mode=0o700, exist_ok=True)
                    local._trusted(parent, directory=True)
                if target.name == "registration.json" and target.parent.parent.name == "archives":
                    (target.parent / "days").mkdir(mode=0o700, exist_ok=True)
                source = archive.extractfile(member)
                sha, size = hashlib.sha256(), 0
                with _new(target) as output:
                    while chunk := source.read(min(CHUNK, member.size - size + 1)):
                        size += len(chunk)
                        if size > member.size:
                            raise OffsiteError("offsite_tar_size_mismatch")
                        _space(destination.parent, minimum, needed=len(chunk))
                        output.write(chunk)
                        sha.update(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if size != member.size or sha.hexdigest() != expected[member.name]["sha256"]:
                    raise OffsiteError("offsite_tar_file_checksum_mismatch")
                seen.add(member.name)
    if seen != set(expected):
        raise OffsiteError("offsite_tar_files_missing")
    return local.verify_backup(destination)


def _object_metadata(response, bucket, name, expected):
    if response.status not in (200, 201):
        raise OffsiteError("offsite_object_metadata_unavailable")
    value = _json(response.body)
    if (type(value) is not dict or value.get("name") != name or value.get("bucket") != bucket
            or type(value.get("generation")) is not str or not re.fullmatch(r"[1-9][0-9]{0,30}", value["generation"])
            or value.get("size") != str(expected["size"]) or value.get("md5Hash") != expected["md5"]
            or value.get("contentEncoding") not in (None, "identity")):
        raise OffsiteError("offsite_object_checksum_or_identity_mismatch")
    return value


def _newest(root):
    candidates = []
    for count, path in enumerate(root.iterdir(), 1):
        if count > local.MAX_FILES:
            raise OffsiteError("offsite_backup_selection_bound_exceeded")
        if BACKUP_NAME.fullmatch(path.name):
            candidates.append(path)
    if not candidates:
        raise OffsiteError("offsite_no_completed_backup")
    # A newest malformed/incomplete snapshot is a failure, never silent fallback.
    return max(candidates, key=lambda path: path.name)


def upload_verified_backup(backup, bucket, work_dir, *, backup_dir=None,
                           min_free_bytes=268435456, transport=None, token_provider=None):
    """Upload one immutable local snapshot and verify an independent download."""
    if type(bucket) is not str or not BUCKET.fullmatch(bucket):
        raise OffsiteError("offsite_bucket_invalid")
    if type(min_free_bytes) is not int or not 16 * 1024**2 <= min_free_bytes <= 100 * 1024**3:
        raise OffsiteError("offsite_disk_reserve_invalid")
    if (backup is None) == (backup_dir is None):
        raise OffsiteError("offsite_exactly_one_backup_source_required")
    work = local._path(work_dir)
    local._trusted(work, directory=True)
    selected = None if backup is None else local._path(backup)
    parent = local._path(backup_dir) if backup_dir is not None else selected.parent
    if work == parent or parent in work.parents or work in parent.parents:
        raise OffsiteError("offsite_work_source_overlap")
    requester = _http if transport is None else transport
    token = metadata_token if token_provider is None else token_provider
    def request(method, url, **kwargs):
        try:
            access_token = token()
            if type(access_token) is not str or not re.fullmatch(r"[!-~]{1,8192}", access_token):
                raise OffsiteError("offsite_metadata_token_invalid")
            headers = {"Authorization": "Bearer " + access_token, "Accept-Encoding": "identity", **kwargs.pop("headers", {})}
            return requester(method, url, headers=headers, **kwargs)
        except OffsiteError:
            raise
        except Exception:
            raise OffsiteError("offsite_transport_failed") from None
    with file_lock(work, ".offsite.lock"):
        with tempfile.TemporaryDirectory(prefix="upload-", dir=work) as temporary:
            stage = Path(temporary)
            stage.chmod(0o700)
            upload = stage / "upload.tar.gz"
            with local.backup_root_lock(parent):
                snapshot = _newest(parent) if selected is None else selected
                manifest = local.verify_backup(snapshot)
                source_size = sum(entry["size"] for entry in manifest["files"])
                # No assumed compression savings. Allow tar headers/PAX/padding,
                # gzip expansion, extraction and an extra bounded safety margin.
                tar_bound = source_size + local.MAX_MANIFEST_BYTES + (len(manifest["files"]) + 1) * 4096 + 65536
                packed_bound = tar_bound + tar_bound // 100 + CHUNK
                _space(work, min_free_bytes, needed=source_size + packed_bound + CHUNK)
                entries = _package(snapshot, manifest, upload, min_free_bytes)
                expected = _hashes(upload)
                if expected["size"] > packed_bound:
                    raise OffsiteError("offsite_package_size_bound_exceeded")
            name = "backups/" + manifest["backup_id"] + ".tar.gz"
            base = "https://storage.googleapis.com/storage/v1/b/" + bucket + "/o/" + quote(name, safe="")
            insert = "https://storage.googleapis.com/upload/storage/v1/b/" + bucket + "/o?" + urlencode({
                "uploadType": "media", "name": name, "ifGenerationMatch": "0"})
            response = request("POST", insert, source=upload, headers={
                "Content-Type": "application/gzip", "X-Goog-Hash": "md5=" + expected["md5"]})
            already_exists = response.status == 412
            if already_exists:
                response = request("GET", base)
            metadata = _object_metadata(response, bucket, name, expected)
            generation = metadata["generation"]
            # Reuse space: retain digests, remove only our own packaged copy.
            upload.unlink()
            download = stage / "download.tar.gz"
            response = request("GET", base + "?" + urlencode({"alt": "media", "generation": generation,
                               "ifGenerationMatch": generation}), sink=download, max_bytes=expected["size"],
                               reserve=(work, min_free_bytes))
            if response.status != 200 or not download.is_file() or _hashes(download) != expected:
                raise OffsiteError("offsite_download_checksum_or_status_mismatch")
            downloaded = _extract(download, stage / manifest["backup_id"], entries, tar_bound, min_free_bytes)
            if downloaded != manifest:
                raise OffsiteError("offsite_downloaded_manifest_mismatch")
            receipt = {"ok": True, "backup_id": manifest["backup_id"],
                "backup_created_at": manifest["created_at"], "bucket": bucket,
                "object_name": name, "generation": generation, "object_bytes": expected["size"],
                "object_sha256": expected["sha256"], "server_md5_verified": True,
                "already_existed": already_exists, "downloaded_snapshot_integrity_verified": True,
                "restore_operation_performed": False, "file_count": len(manifest["files"]),
                "source_bytes": source_size, "staging_required_bytes": source_size + packed_bound + CHUNK,
                "execution_enabled": False, "credentials_persisted": False,
                "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            invocation = os.environ.get("INVOCATION_ID", "")
            if re.fullmatch(r"[a-f0-9]{32}", invocation):
                receipt["offsite_service_invocation_id"] = invocation
    receipt["temporary_staging_removed"] = True
    return receipt


def write_receipt(path, result, work_dir):
    """Atomically persist only caller-sanitized operational status outside backup."""
    work, target = local._path(work_dir), local._path(path)
    local._trusted(work, directory=True)
    if target.parent != work or target.name.startswith("."):
        raise OffsiteError("offsite_receipt_path_invalid")
    if target.exists():
        local._trusted(target)
    fd, temporary = tempfile.mkstemp(prefix="receipt-", dir=work)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write((json.dumps(result, sort_keys=True, allow_nan=False) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        local._sync(work)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
