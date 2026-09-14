"""Offsite backups never trust upload success without independent readback."""

import base64
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import tarfile
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from atlas.collection import backup, offsite


NOW = 1789344000
BUCKET = "atlas-collector-backups-836932812958"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("offsite tests must not contact any network")
    monkeypatch.setattr(socket.socket, "connect", refuse)


def write(path, content):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o600)


@pytest.fixture
def state(tmp_path):
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    write(root / "state.sqlite3", b"")
    with sqlite3.connect(root / "state.sqlite3") as db:
        db.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO events VALUES (1, 'unaltered historical event')")
    return root


@pytest.fixture
def snapshot(state, tmp_path):
    return Path(backup.create_backup(state, tmp_path / "backups", now=NOW)["backup_path"])


@pytest.fixture
def work(tmp_path):
    root = tmp_path / "offsite"
    root.mkdir(mode=0o700)
    return root


class Storage:
    """A create/get-only fake: generation identity and stored bytes are separate."""
    def __init__(self):
        self.objects, self.calls = {}, []
        self.bad_md5 = False
        self.download_mode = "complete"
        self.upload_status = None

    def __call__(self, method, url, **kwargs):
        parsed = urlsplit(url)
        assert parsed.scheme == "https" and parsed.netloc == "storage.googleapis.com"
        assert kwargs["headers"]["Authorization"] == "Bearer fake-token"
        assert kwargs["headers"]["Accept-Encoding"] == "identity"
        self.calls.append((method, url, {key: value for key, value in kwargs.items() if key != "headers"}))
        query = parse_qs(parsed.query)
        if method == "POST":
            assert parsed.path == f"/upload/storage/v1/b/{BUCKET}/o"
            assert query["ifGenerationMatch"] == ["0"] and query["uploadType"] == ["media"]
            assert kwargs["headers"]["Content-Type"] == "application/gzip"
            name = query["name"][0]
            body = kwargs["source"].read_bytes()
            assert kwargs["headers"]["X-Goog-Hash"] == "md5=" + base64.b64encode(hashlib.md5(body).digest()).decode()
            if self.upload_status:
                return offsite.HTTPResult(self.upload_status, b"provider diagnostics are not user output")
            if name in self.objects:
                return offsite.HTTPResult(412)
            metadata = {"bucket": BUCKET, "name": name, "generation": "123456789",
                        "size": str(len(body)), "md5Hash": base64.b64encode(hashlib.md5(body).digest()).decode()}
            self.objects[name] = (body, metadata)
        else:
            assert method == "GET" and parsed.path.startswith(f"/storage/v1/b/{BUCKET}/o/")
            name = unquote(parsed.path.split("/o/", 1)[1])
            body, metadata = self.objects[name]
            if query.get("alt") == ["media"]:
                assert query == {"alt": ["media"], "generation": [metadata["generation"]],
                                 "ifGenerationMatch": [metadata["generation"]]}
                assert not (kwargs["sink"].parent / "upload.tar.gz").exists()
                assert kwargs["max_bytes"] == len(body)
                if self.download_mode == "status":
                    return offsite.HTTPResult(503)
                data = body[:-1] if self.download_mode == "partial" else body
                if self.download_mode == "corrupt":
                    data = bytes([body[0] ^ 1]) + body[1:]
                write(kwargs["sink"], data)
                return offsite.HTTPResult(200)
        returned = dict(metadata)
        if self.bad_md5:
            returned["md5Hash"] = "malformed-checksum"
        return offsite.HTTPResult(200, json.dumps(returned).encode())


def upload(snapshot, work, storage, **kwargs):
    return offsite.upload_verified_backup(snapshot, BUCKET, work,
        transport=storage, token_provider=lambda: "fake-token", **kwargs)


def test_create_upload_download_and_full_local_verification(snapshot, work, monkeypatch):
    storage = Storage()
    verified_paths = []
    verify = backup.verify_backup
    def observe(path):
        verified_paths.append(Path(path))
        return verify(path)
    monkeypatch.setattr(backup, "verify_backup", observe)
    monkeypatch.setattr(backup, "restore_backup", lambda *a, **k: pytest.fail("daily run must not create a second restore copy"))
    monkeypatch.setenv("INVOCATION_ID", "a" * 32)
    result = upload(snapshot, work, storage)
    assert result["ok"] and result["downloaded_snapshot_integrity_verified"]
    assert result["server_md5_verified"] and result["temporary_staging_removed"]
    assert result["restore_operation_performed"] is False
    assert result["backup_created_at"] == NOW and result["offsite_service_invocation_id"] == "a" * 32
    assert result["generation"] == "123456789" and not result["already_existed"]
    assert result["execution_enabled"] is False and result["credentials_persisted"] is False
    assert any(work in path.parents for path in verified_paths)
    assert backup.verify_backup(snapshot)
    assert {path.name for path in work.iterdir()} == {".offsite.lock"}


def test_retry_is_deterministic_create_only_and_verifies_existing_generation(snapshot, work):
    storage = Storage()
    first = upload(snapshot, work, storage)
    original = dict(storage.objects)
    second = upload(snapshot, work, storage)
    assert first["object_sha256"] == second["object_sha256"]
    assert second["already_existed"] and second["downloaded_snapshot_integrity_verified"]
    assert storage.objects == original
    assert [call[0] for call in storage.calls] == ["POST", "GET", "POST", "GET", "GET"]


def test_existing_same_name_with_different_bytes_refuses(snapshot, work):
    storage = Storage()
    result = upload(snapshot, work, storage)
    body, metadata = storage.objects[result["object_name"]]
    storage.objects[result["object_name"]] = (body, {**metadata, "md5Hash": "different"})
    with pytest.raises(offsite.OffsiteError, match="checksum_or_identity"):
        upload(snapshot, work, storage)
    assert storage.calls[-1][0] == "GET" and "sink" not in storage.calls[-1][2]


@pytest.mark.parametrize("failure", ["checksum", "upload", "partial", "corrupt", "status"])
def test_failed_transfer_never_reports_proof_or_removes_source(snapshot, work, failure):
    storage = Storage()
    if failure == "checksum":
        storage.bad_md5 = True
    elif failure == "upload":
        storage.upload_status = 503
    else:
        storage.download_mode = failure
    before = backup.verify_backup(snapshot)
    with pytest.raises(offsite.OffsiteError):
        upload(snapshot, work, storage)
    assert backup.verify_backup(snapshot) == before
    assert {path.name for path in work.iterdir()} == {".offsite.lock"}


def test_backup_source_locked_during_package_but_released_before_network(snapshot, state, work, tmp_path, monkeypatch):
    original = offsite._package
    def packaging(*args, **kwargs):
        with pytest.raises(backup.BackupError, match="backup_root_busy"):
            backup.create_backup(state, snapshot.parent, retention=1, now=NOW+1)
        assert snapshot.exists()
        return original(*args, **kwargs)
    monkeypatch.setattr(offsite, "_package", packaging)
    storage = Storage()
    def after_packaging(method, url, **kwargs):
        if method == "POST":
            replacement = backup.create_backup(state, snapshot.parent, retention=1, now=NOW+2)
            assert replacement["pruned_backups"] == [snapshot.name]
            assert not snapshot.exists()
        return storage(method, url, **kwargs)
    assert upload(snapshot, work, after_packaging)["ok"]


def test_newest_selection_does_not_fall_back_from_incomplete_snapshot(snapshot, state, work):
    newer = snapshot.parent / "atlas-backup-20260915T000000Z-1234567890abcdef"
    newer.mkdir(mode=0o700)
    storage = Storage()
    with pytest.raises(backup.BackupError):
        upload(None, work, storage, backup_dir=snapshot.parent)
    assert storage.calls == [] and snapshot.exists() and newer.exists()


def test_newest_completed_snapshot_selected(snapshot, state, work):
    newer = backup.create_backup(state, snapshot.parent, now=NOW+1)
    result = upload(None, work, Storage(), backup_dir=snapshot.parent)
    assert result["backup_id"] == newer["backup_id"]


def test_low_disk_refuses_before_auth_or_upload(snapshot, work, monkeypatch):
    monkeypatch.setattr(offsite.shutil, "disk_usage", lambda _: type("Disk", (), {"free": 268435456})())
    with pytest.raises(offsite.OffsiteError, match="disk_reserve_insufficient"):
        offsite.upload_verified_backup(snapshot, BUCKET, work,
            token_provider=lambda: pytest.fail("low disk must refuse before obtaining token"))
    assert {path.name for path in work.iterdir()} == {".offsite.lock"}


def test_only_verified_manifest_files_packaged_and_credentials_excluded(state, tmp_path, work):
    secret = b"private material excluded from the source state"
    write(state / "credentials.json", secret)
    write(state / "research_artifacts" / "rules.md", b"frozen research rules\n")
    snapshot = Path(backup.create_backup(state, tmp_path / "backups", now=NOW)["backup_path"])
    storage = Storage()
    result = upload(snapshot, work, storage)
    body = storage.objects[result["object_name"]][0]
    assert secret not in gzip.decompress(body) and b"fake-token" not in gzip.decompress(body)
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
        assert set(archive.getnames()) == {"manifest.json", "payload/state.sqlite3", "payload/research_artifacts/rules.md"}
    write(snapshot / "payload" / "credentials.json", b"unexpected")
    with pytest.raises(backup.BackupError):
        upload(snapshot, work, Storage())


def test_empty_sharded_archive_days_directory_reconstructed(state, tmp_path, work):
    from atlas.collection.archive import init_collection_archive
    from atlas.research.data_contract_v1 import default_contract
    (state / "archives").mkdir(mode=0o700)
    root = state / "archives" / "sharded-v1"
    init_collection_archive(root, default_contract("sharded-v1", NOW-180*86400, NOW), clock=lambda: NOW)
    snapshot = Path(backup.create_backup(state, tmp_path / "backups", now=NOW)["backup_path"])
    assert upload(snapshot, work, Storage())["downloaded_snapshot_integrity_verified"]


@pytest.mark.parametrize("kind", ["traversal", "symlink", "hardlink", "device", "extra", "duplicate", "mode", "pax"])
def test_unsafe_tar_members_refused_without_escape(tmp_path, kind):
    path = tmp_path / "bad.tar.gz"
    name = "../escape" if kind == "traversal" else "payload/a"
    member = tarfile.TarInfo(name)
    member.mode, member.size = 0o600, 1
    if kind == "symlink":
        member.type, member.linkname, member.size = tarfile.SYMTYPE, "../../escape", 0
    elif kind == "hardlink":
        member.type, member.linkname, member.size = tarfile.LNKTYPE, "../../escape", 0
    elif kind == "device":
        member.type, member.size = tarfile.CHRTYPE, 0
    elif kind == "mode":
        member.mode = 0o777
    elif kind == "pax":
        member.pax_headers = {"SCHILY.xattr.user.secret": "refused"}
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        archive.addfile(member, io.BytesIO(b"a"))
        if kind == "duplicate":
            archive.addfile(member, io.BytesIO(b"a"))
    entries = [{"path": name if kind != "extra" else "payload/other", "size": member.size,
                "sha256": hashlib.sha256(b"a").hexdigest()}]
    with pytest.raises((offsite.OffsiteError, backup.BackupError)):
        offsite._extract(path, tmp_path / "extract", entries, 1048576, 0)
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("change", [{"md5Hash": ""}, {"size": 1}, {"generation": "0"},
                                    {"generation": "1\n"}, {"bucket": "wrong"},
                                    {"contentEncoding": "gzip"}, {"name": "backups/wrong"}])
def test_metadata_identity_and_checksum_strict(change):
    expected = {"size": 1, "md5": "test"}
    data = {"size": "1", "md5Hash": "test", "generation": "1", "bucket": BUCKET, "name": "backups/item"}
    with pytest.raises(offsite.OffsiteError, match="checksum_or_identity"):
        offsite._object_metadata(offsite.HTTPResult(200, json.dumps({**data, **change}).encode()), BUCKET, "backups/item", expected)


def test_metadata_token_uses_fixed_endpoint_and_validates_headers():
    def metadata(method, url, **kwargs):
        assert method == "GET" and url == offsite.TOKEN_URL
        assert kwargs["headers"] == {"Metadata-Flavor": "Google"}
        return offsite.HTTPResult(200, b'{"access_token":"fake-token","expires_in":3599,"token_type":"Bearer"}', {"metadata-flavor": "Google"})
    assert offsite.metadata_token(transport=metadata) == "fake-token"
    with pytest.raises(offsite.OffsiteError, match="token_unavailable"):
        offsite.metadata_token(transport=lambda *a, **k: offsite.HTTPResult(200, b"{}"))


@pytest.mark.parametrize("body", [b'{"a":1,"a":2}', b'{"access_token":"token","expires_in":true,"token_type":"Bearer"}',
                                  b'{"access_token":"token\\r\\nheader","expires_in":50,"token_type":"Bearer"}',
                                  b"NaN", b"[", b"x" * (offsite.JSON_LIMIT+1)],
                         ids=["duplicate", "boolean-expiry", "header-injection", "nan", "malformed", "oversized"])
def test_bad_metadata_responses_fail_without_exposing_body(body):
    with pytest.raises(offsite.OffsiteError) as caught:
        offsite.metadata_token(transport=lambda *a, **k: offsite.HTTPResult(200, body, {"metadata-flavor": "Google"}))
    assert "token\\r" not in str(caught.value)


def test_receipt_is_private_atomic_and_outside_backup(snapshot, work):
    result = upload(snapshot, work, Storage())
    target = work / "latest.json"
    offsite.write_receipt(target, result, work)
    assert json.loads(target.read_bytes()) == result and target.stat().st_mode & 0o777 == 0o600
    offsite.write_receipt(target, {"ok": False, "error": "offsite_transport_failed"}, work)
    assert json.loads(target.read_bytes())["ok"] is False
    with pytest.raises(offsite.OffsiteError, match="receipt_path_invalid"):
        offsite.write_receipt(snapshot / "receipt.json", result, work)
    assert not list(work.glob("receipt-*"))


@pytest.mark.parametrize("url", ["http://storage.googleapis.com/storage/v1/b/a/o/b", "https://evil.invalid/storage/v1/b/a/o/b",
                                 "https://storage.googleapis.com.evil.invalid/storage/v1/b/a/o/b",
                                 "http://169.254.169.254/computeMetadata/v1/project/project-id"])
def test_transport_refuses_other_endpoints_without_network(url):
    with pytest.raises(offsite.OffsiteError, match="endpoint_refused"):
        offsite._http("GET", url)


def test_transport_streams_upload_and_download_with_small_bounded_reads(tmp_path, monkeypatch):
    body = b"x" * (offsite.CHUNK * 2 + 19)
    source, sink = tmp_path / "upload", tmp_path / "download"
    write(source, body)
    connections = []
    class Response:
        status = 200
        def __init__(self, value):
            self.value = io.BytesIO(value)
        def getheaders(self):
            return [("Content-Length", str(len(body))), ("X-Secret", "never return")]
        def read(self, count):
            assert 0 < count <= offsite.CHUNK
            return self.value.read(count)
    class Connection:
        def __init__(self, host, **kwargs):
            assert host == "storage.googleapis.com" and kwargs["timeout"] == 30
            assert kwargs["context"].check_hostname
            self.closed = False
            connections.append(self)
        def request(self, method, path, body=None, headers=None):
            assert headers["Authorization"] == "Bearer fake-token"
            if method == "POST":
                assert headers["Content-Length"] == str(source.stat().st_size)
                assert hasattr(body, "read") and body.read(19) == b"x" * 19
                self.response = Response(b"{}")
            else:
                self.response = Response(globals_body)
        def getresponse(self):
            return self.response
        def close(self):
            self.closed = True
    globals_body = body
    monkeypatch.setattr(offsite.http.client, "HTTPSConnection", Connection)
    headers = {"Authorization": "Bearer fake-token"}
    response = offsite._http("POST", f"https://storage.googleapis.com/upload/storage/v1/b/{BUCKET}/o?uploadType=media", source=source, headers=headers)
    assert response.body == b"{}" and "x-secret" not in response.headers
    offsite._http("GET", f"https://storage.googleapis.com/storage/v1/b/{BUCKET}/o/item?alt=media", sink=sink,
                  max_bytes=len(body), headers=headers, reserve=(tmp_path, 0))
    assert sink.read_bytes() == body and sink.stat().st_mode & 0o777 == 0o600
    assert all(connection.closed for connection in connections)


@pytest.mark.parametrize("case", ["oversized", "partial", "redirect"])
def test_transport_refuses_or_retains_failure_on_incomplete_response(tmp_path, monkeypatch, case):
    class Response:
        status = 302 if case == "redirect" else 200
        def getheaders(self):
            return [("Location", "https://evil.invalid/")]
        def read(self, count):
            if case == "partial":
                raise offsite.http.client.IncompleteRead(b"partial")
            return b"xx" if case == "oversized" else b""
    class Connection:
        def __init__(self, *a, **k):
            pass
        def request(self, *a, **k):
            pass
        def getresponse(self):
            return Response()
        def close(self):
            pass
    monkeypatch.setattr(offsite.http.client, "HTTPSConnection", Connection)
    target = tmp_path / "download"
    if case == "redirect":
        result = offsite._http("GET", f"https://storage.googleapis.com/storage/v1/b/{BUCKET}/o/item", sink=target)
        assert result.status == 302 and not target.exists()
    else:
        with pytest.raises(offsite.OffsiteError):
            offsite._http("GET", f"https://storage.googleapis.com/storage/v1/b/{BUCKET}/o/item", sink=target, max_bytes=1)


def test_cli_failure_redacts_exception_and_replaces_old_success_receipt(work, monkeypatch, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location("offsite_cli", Path(__file__).resolve().parents[1] / "scripts" / "atlas_offsite_backup.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    def failed(*args, **kwargs):
        raise RuntimeError("Bearer confidential-provider-token")
    monkeypatch.setattr(cli, "upload_verified_backup", failed)
    target = work / "latest.json"
    offsite.write_receipt(target, {"ok": True}, work)
    status = cli.main(["--backup-dir", str(work.parent / "backups"), "--bucket", BUCKET,
                       "--work-dir", str(work), "--receipt", str(target)])
    output = capsys.readouterr()
    assert status == 1 and "confidential" not in output.out + output.err
    report = json.loads(target.read_bytes())
    assert report["error"] == "offsite_operation_failed" and report["downloaded_snapshot_integrity_verified"] is False
