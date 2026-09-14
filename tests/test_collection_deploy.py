"""Offline package/installer checks. No host account, service or /etc mutation."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("collector_installer_under_test", ROOT / "scripts/install_collector.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def test_check_mode_reads_package_but_never_installs_or_creates_host_state(monkeypatch, capsys):
    def refuse(*args, **kwargs):
        pytest.fail("--check must never perform installation")
    monkeypatch.setattr(installer, "_run", refuse)
    monkeypatch.setattr(installer, "_host_requirements", refuse)
    monkeypatch.setattr(installer, "stage_release", refuse)
    monkeypatch.setattr(installer, "_directory", refuse)
    assert installer.main(["--check"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["host_changed"] is False and result["live_trading"] is False
    assert result["third_party_python_dependencies"] == 0


def test_release_allowlist_excludes_orders_models_secrets_and_general_entrypoint(tmp_path):
    package = tmp_path / "release"
    manifest = installer.stage_release(package)
    assert set(manifest) == set(installer.FILES)
    actual = {str(path.relative_to(package)) for path in package.rglob("*") if path.is_file()}
    assert actual == set(installer.FILES) | {"release.json"}
    assert not any(word in name for name in actual for word in ("broker", "risk.py", "ledger", ".pkl", "journal.db", ".env", "__main__"))
    assert installer._release(package) == manifest
    with pytest.raises(ValueError, match="destination_exists"):
        installer.stage_release(package)
    (package / "atlas/research/data_collector.py").write_text("broken")
    with pytest.raises(ValueError, match="integrity_mismatch"):
        installer._release(package)


@pytest.mark.parametrize("script", ["atlas_collect.py", "atlas_health.py", "atlas_backup.py", "atlas_offsite_backup.py"])
def test_exact_release_entrypoints_help_without_dependencies_or_network(tmp_path, script):
    package = tmp_path / "release"
    installer.stage_release(package)
    result = subprocess.run([sys.executable, "-I", "-S", str(package / "scripts" / script), "--help"],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def test_units_match_real_cli_and_enforce_nonroot_data_only_resource_boundary():
    unit = (ROOT / "deploy/atlas-collector.service").read_text()
    for setting in (
        "User=atlas-collector", "Group=atlas-collector", "Restart=always", "RestartSec=15",
        "KillSignal=SIGTERM", "UMask=0077", "NoNewPrivileges=true", "ProtectSystem=strict",
        "ProtectHome=true", "PrivateTmp=true", "PrivateDevices=true", "CapabilityBoundingSet=",
        "MemoryMax=600M", "CPUQuota=90%", "ReadWritePaths=/var/lib/atlas-collector",
        "Environment=LIVE_TRADING=false", "Environment=ATLAS_LIVE_TRADING_ENABLED=false",
        "Environment=ATLAS_LIVE_AUTO_ENABLED=false", "WantedBy=multi-user.target",
    ):
        assert setting in unit
    assert "scripts/atlas_collect.py run --config /etc/atlas-collector/config.json" in unit
    backup = (ROOT / "deploy/atlas-collector-backup.service").read_text()
    assert "scripts/atlas_backup.py create --state-dir /var/lib/atlas-collector" in backup
    assert "RestrictAddressFamilies=AF_UNIX" in backup
    timer = (ROOT / "deploy/atlas-collector-backup.timer").read_text()
    assert "Persistent=true" in timer and "OnCalendar=*-*-* 04:15:00 UTC" in timer


def test_verified_pre_offsite_release_is_accepted_but_arbitrary_partial_release_is_not(tmp_path):
    package = tmp_path / "legacy-release"
    installer.stage_release(package)
    manifest_path = package / "release.json"
    manifest = json.loads(manifest_path.read_text())
    for name in installer.OFFSITE_FILES:
        (package / name).unlink()
        del manifest[name]
    manifest_path.write_text(json.dumps(manifest))
    assert installer._release(package) == manifest
    del manifest["atlas/collection/backup.py"]
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="allowlist_invalid"):
        installer._release(package)


@pytest.fixture
def host(tmp_path, monkeypatch):
    home = tmp_path / "host"
    home.mkdir()
    paths = {"CONFIG_DIR": home / "etc/atlas-collector", "STATE_DIR": home / "var/lib/atlas-collector",
             "BACKUP_DIR": home / "var/backups/atlas-collector", "BASE": home / "opt/atlas-collector",
             "SYSTEMD_DIR": home / "etc/systemd/system", "JOURNALD_DIR": home / "etc/systemd/journald.conf.d"}
    for name, path in paths.items():
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        if name == "SYSTEMD_DIR":
            path.mkdir(mode=0o755)
        monkeypatch.setattr(installer, name, path)
    uid = os.getuid() or 1000
    account = SimpleNamespace(pw_uid=uid, pw_gid=os.getgid(), pw_shell="/usr/sbin/nologin")
    monkeypatch.setattr(installer.pwd, "getpwnam", lambda _: account)
    monkeypatch.setattr(installer, "_host_requirements", lambda: None)
    calls = []
    def run(args):
        calls.append(list(args))
        if "init" in args and "atlas_collect.py" in " ".join(args):
            assert args[:4] == ["runuser", "-u", "atlas-collector", "--"]
            assert paths["STATE_DIR"].stat().st_uid == uid
            assert paths["STATE_DIR"].stat().st_mode & 0o777 == 0o700
            config = paths["CONFIG_DIR"] / "config.json"
            assert config.stat().st_uid == uid and config.stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(installer, "_run", run)
    return SimpleNamespace(paths=paths, calls=calls, account=account, run=run)


def test_fresh_install_initializes_only_as_service_user_and_excludes_secrets_from_state(host):
    assert installer.main(["--coinbase-only"]) == 0
    config = json.loads((host.paths["CONFIG_DIR"] / "config.json").read_text())
    assert config["providers"] == ["coinbase"] and config["live_trading"] is False
    assert config["alpaca_key_file"] is None
    target = host.paths["BASE"] / "current"
    assert target.is_symlink() and target.resolve().parent == host.paths["BASE"] / "releases"
    calls = host.calls
    app_calls = [row for row in calls if "/usr/bin/python3" in row]
    assert app_calls and all(row[:4] == ["runuser", "-u", "atlas-collector", "--"] for row in app_calls)
    assert sum("init" in row for row in app_calls) == 1
    assert ["systemctl", "is-active", "atlas-collector.service"] in calls
    assert not any("backup" in name or "credential" in name for name in os.listdir(host.paths["STATE_DIR"]))


def test_offsite_opt_in_installs_success_hook_without_weakening_local_backup(host):
    bucket = "atlas-collector-backups-836932812958"
    installer.main(["--coinbase-only", "--offsite-bucket", bucket])
    assert (host.paths["CONFIG_DIR"] / "offsite.env").read_text() == "ATLAS_OFFSITE_BUCKET=" + bucket + "\n"
    hook = host.paths["SYSTEMD_DIR"] / installer.OFFSITE_DROPIN
    assert "OnSuccess=atlas-collector-offsite.service" in hook.read_text()
    backup = (host.paths["SYSTEMD_DIR"] / "atlas-collector-backup.service").read_text()
    assert "RestrictAddressFamilies=AF_UNIX" in backup and "--retention 7" in backup
    offsite = (host.paths["SYSTEMD_DIR"] / "atlas-collector-offsite.service").read_text()
    assert "User=atlas-collector" in offsite and "--backup-dir /var/backups/atlas-collector" in offsite
    assert "InaccessiblePaths=/var/lib/atlas-collector -/etc/atlas-collector/alpaca-data.json" in offsite
    assert not any("atlas_offsite_backup.py" in " ".join(row) and "--help" not in row for row in host.calls)


def test_failed_activation_restores_previous_offsite_bucket_and_hook(host, monkeypatch):
    installer.main(["--coinbase-only", "--offsite-bucket", "old-backup-bucket"])
    env = host.paths["CONFIG_DIR"] / "offsite.env"
    hook = host.paths["SYSTEMD_DIR"] / installer.OFFSITE_DROPIN
    original = (env.read_bytes(), hook.read_bytes())
    def fail_activate(args):
        host.calls.append(args)
        if args == ["systemctl", "is-active", "atlas-collector.service"]:
            raise subprocess.CalledProcessError(3, args)
    monkeypatch.setattr(installer, "_run", fail_activate)
    with pytest.raises(subprocess.CalledProcessError):
        installer.main(["--offsite-bucket", "new-backup-bucket"])
    assert (env.read_bytes(), hook.read_bytes()) == original


@pytest.mark.parametrize("bucket", ["gs://bucket", "bucket name", "bucket\nOTHER=value", "UPPERCASE"])
def test_offsite_bucket_cannot_inject_environment_or_extra_arguments(host, bucket):
    with pytest.raises(SystemExit):
        installer.main(["--coinbase-only", "--offsite-bucket", bucket])
    assert host.calls == []


def test_update_backs_up_old_release_before_stop_and_preserves_configuration_and_state(host, monkeypatch):
    installer.main(["--coinbase-only"])
    old_release = (host.paths["BASE"] / "current").resolve()
    cfg_path = host.paths["CONFIG_DIR"] / "config.json"
    original_config = cfg_path.read_bytes()
    sentinel = host.paths["STATE_DIR"] / "research-proof.bin"
    sentinel.write_bytes(b"original research state")
    host.calls.clear()
    assert installer.main([]) == 0
    backup = next(i for i, args in enumerate(host.calls) if "create" in args and "atlas_backup.py" in " ".join(args))
    stop = host.calls.index(["systemctl", "stop", "atlas-collector.service"])
    assert backup < stop
    assert str(old_release / "scripts/atlas_backup.py") in host.calls[backup]
    assert not any("init" in args for args in host.calls)
    assert not any(args[0] == "apt-get" for args in host.calls)
    assert cfg_path.read_bytes() == original_config and sentinel.read_bytes() == b"original research state"


def test_failed_backup_does_not_stop_collector_or_replace_current(host, monkeypatch):
    installer.main(["--coinbase-only"])
    old_release = (host.paths["BASE"] / "current").resolve()
    host.calls.clear()
    def fail_backup(args):
        host.calls.append(args)
        if "create" in args:
            raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(installer, "_run", fail_backup)
    with pytest.raises(subprocess.CalledProcessError):
        installer.main([])
    assert (host.paths["BASE"] / "current").resolve() == old_release
    assert ["systemctl", "stop", "atlas-collector.service"] not in host.calls


def test_failed_new_release_activation_rolls_back_link_units_without_rolling_back_data(host, monkeypatch, tmp_path):
    installer.main(["--coinbase-only"])
    target = host.paths["BASE"] / "current"
    old_release = target.resolve()
    old_units = {name: (host.paths["SYSTEMD_DIR"] / name).read_bytes() for name in installer.UNITS}
    sentinel = host.paths["STATE_DIR"] / "research-proof.bin"
    sentinel.write_bytes(b"do not replace state")
    source = tmp_path / "new-source"
    source.mkdir()
    for name in (*installer.FILES, *("deploy/" + unit for unit in installer.UNITS),
                 "deploy/atlas-collector-journald.conf", "deploy/atlas-collector-backup-offsite.conf"):
        dest = source / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, dest)
    with (source / "atlas/collection/__init__.py").open("a") as output:
        output.write("\n# Changed release fixture.\n")
    monkeypatch.setattr(installer, "SOURCE", source)
    host.calls.clear()
    def fail_activate(args):
        host.calls.append(args)
        if args == ["systemctl", "is-active", "atlas-collector.service"]:
            raise subprocess.CalledProcessError(3, args)
    monkeypatch.setattr(installer, "_run", fail_activate)
    with pytest.raises(subprocess.CalledProcessError):
        installer.main([])
    assert target.resolve() == old_release
    assert {name: (host.paths["SYSTEMD_DIR"] / name).read_bytes() for name in installer.UNITS} == old_units
    assert sentinel.read_bytes() == b"do not replace state"
    assert host.calls[-1] == ["systemctl", "restart", "atlas-collector.service"]


def test_partial_initialization_is_retained_and_cannot_be_silently_reused(host):
    host.paths["STATE_DIR"].mkdir(mode=0o700)
    (host.paths["STATE_DIR"] / "partial.db").write_bytes(b"evidence")
    with pytest.raises(ValueError, match="partial_installation"):
        installer.main(["--coinbase-only"])
    assert (host.paths["STATE_DIR"] / "partial.db").read_bytes() == b"evidence"
    assert host.calls == []


def test_selected_key_is_validated_without_secret_text_in_error_and_not_copied_to_state(host, tmp_path):
    path = tmp_path / "selected-key.json"
    path.write_text('{"api_key":"test-key","api_secret":"test-secret"}')
    path.chmod(0o600)
    installer.main(["--alpaca-key-file", str(path)])
    private = host.paths["CONFIG_DIR"] / "alpaca-data.json"
    assert private.stat().st_mode & 0o777 == 0o600
    assert private.stat().st_uid == host.account.pw_uid
    assert not list(host.paths["STATE_DIR"].rglob("*key*"))
    path.write_text('{"api_key":"test-key","api_key":"leak","api_secret":"test-secret"}')
    with pytest.raises(ValueError) as caught:
        installer._key_file(str(path))
    assert "test-secret" not in str(caught.value) and "leak" not in str(caught.value)


def test_conflicting_install_modes_refuse_before_host_calls(host):
    with pytest.raises(SystemExit):
        installer.main(["--coinbase-only", "--alpaca-key-file", "/selected/key"])
    assert host.calls == []


@pytest.mark.skipif(os.environ.get("ATLAS_INSTALLER_LINUX_SMOKE") != "1",
                    reason="explicit disposable root Linux container required")
def test_linux_installer_real_uid_permissions_and_verified_update(monkeypatch):
    """Real Linux filesystem/users/init/backup; service manager calls simulated.

    This opt-in test must run in a disposable network-disabled container. It
    deliberately does not claim a systemd PID1, running collector or reboot test.
    """
    assert sys.platform == "linux" and os.geteuid() == 0 and Path("/.dockerenv").is_file()
    assert not Path("/etc/atlas-collector").exists()
    Path("/run/systemd/system").mkdir(parents=True, exist_ok=True)
    simulated = []
    def run(args):
        if args[0] in {"apt-get", "systemctl"}:
            simulated.append(args)
            return
        subprocess.run(args, check=True, timeout=60)
    monkeypatch.setattr(installer, "_run", run)
    assert installer.main(["--coinbase-only"]) == 0
    account = installer.pwd.getpwnam("atlas-collector")
    assert account.pw_uid != 0
    config = Path("/etc/atlas-collector/config.json")
    state = Path("/var/lib/atlas-collector")
    assert config.stat().st_uid == account.pw_uid
    assert config.stat().st_mode & 0o777 == 0o600
    assert state.stat().st_uid == account.pw_uid and state.stat().st_mode & 0o777 == 0o700
    for path in state.rglob("*"):
        assert path.stat().st_uid == account.pw_uid
    before = config.read_bytes()
    assert installer.main([]) == 0
    assert config.read_bytes() == before
    backups = [path for path in Path("/var/backups/atlas-collector").iterdir() if path.is_dir()]
    assert len(backups) == 1
    current = Path("/opt/atlas-collector/current")
    subprocess.run(installer._as_collector(current / "scripts/atlas_backup.py", "verify", "--backup", backups[0]),
                   check=True, timeout=60)
    assert ["systemctl", "stop", "atlas-collector.service"] in simulated
