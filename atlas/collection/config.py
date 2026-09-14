"""Strict external configuration for the dedicated GET-only collector."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat


class CollectionError(ValueError):
    """Fixed diagnostic suitable for a log; never reflects input contents."""


DEFAULTS = {"poll_seconds": 60, "overlap_seconds": 900,
            "max_backoff_seconds": 3600, "min_free_bytes": 268435456}
REQUIRED = {"version", "mode", "live_trading", "state_dir", "archive_dir",
            "generation_id", "research_start", "providers", "alpaca_key_file"}


def check_environment():
    for name in ("LIVE_TRADING", "ATLAS_LIVE_TRADING_ENABLED", "ATLAS_LIVE_AUTO_ENABLED"):
        if os.environ.get(name, "false").lower() not in ("false", "0", "off", "no", ""):
            raise CollectionError("collector_execution_environment_refused")


def external_path(value):
    if type(value) is not str or not value or "\x00" in value:
        raise CollectionError("collector_path_invalid")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise CollectionError("collector_path_invalid")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise CollectionError("collector_symlink_refused")
    checkout = Path(__file__).resolve().parents[2]
    if path == checkout or checkout in path.parents:
        raise CollectionError("collector_state_inside_checkout_refused")
    return path


def private(path, *, directory=False):
    try:
        info = path.lstat()
        if (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
                or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
                or (not directory and info.st_nlink != 1)):
            raise ValueError
    except (OSError, ValueError):
        raise CollectionError("collector_private_path_required") from None


def read_json(path, *, limit=32768):
    path = external_path(str(path))
    private(path)
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (OSError, ValueError, RecursionError):
        raise CollectionError("collector_json_invalid") from None


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def fingerprint(config):
    # Host paths, key rotation and polling controls are operational settings.
    # A restore may relocate them without rewriting the registered study.
    identity = {key: config[key] for key in ("version", "mode", "live_trading",
                "generation_id", "research_start", "providers")}
    return hashlib.sha256(encoded(identity)).hexdigest()


def contract_fingerprint(config, registered_at):
    from atlas.research.data_contract_v1 import default_contract
    contract = default_contract(config["generation_id"], config["research_start"], registered_at)
    raw = (json.dumps(contract, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    return hashlib.sha256(raw).hexdigest()


def validate_config(value):
    check_environment()
    if type(value) is not dict or not REQUIRED <= value.keys() or value.keys() - REQUIRED - DEFAULTS.keys():
        raise CollectionError("collector_config_fields_invalid")
    config = {**DEFAULTS, **value}
    if (type(config["version"]) is not int or config["version"] != 1
            or config["mode"] != "data_collection" or config["live_trading"] is not False):
        raise CollectionError("collector_execution_configuration_refused")
    generation = config["generation_id"]
    if type(generation) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", generation):
        raise CollectionError("collector_generation_invalid")
    state, archive = (external_path(config[k]) for k in ("state_dir", "archive_dir"))
    if archive != state / "archives" / generation:
        raise CollectionError("collector_archive_layout_invalid")
    config["state_dir"], config["archive_dir"] = str(state), str(archive)
    if (type(config["research_start"]) is not int or not 0 < config["research_start"] < 32503680000
            or config["research_start"] % 300):
        raise CollectionError("collector_research_start_invalid")
    providers = config["providers"]
    if (type(providers) is not list or not providers
            or any(type(item) is not str or item not in ("alpaca", "coinbase") for item in providers)
            or len(providers) != len(set(providers))):
        raise CollectionError("collector_providers_invalid")
    config["providers"] = sorted(providers)
    key_file = config["alpaca_key_file"]
    if key_file is not None:
        path = external_path(key_file)
        if state == path or state in path.parents:
            raise CollectionError("collector_secret_inside_state_refused")
        config["alpaca_key_file"] = str(path)
    if ("alpaca" in providers) != (key_file is not None):
        raise CollectionError("collector_explicit_alpaca_data_key_required")
    for key, low, high in (("poll_seconds", 30, 300), ("overlap_seconds", 900, 3600),
                           ("max_backoff_seconds", 60, 3600),
                           ("min_free_bytes", 16 * 1024**2, 100 * 1024**3)):
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise CollectionError("collector_configuration_bound_invalid")
    if config["overlap_seconds"] % 300 or config["max_backoff_seconds"] < config["poll_seconds"]:
        raise CollectionError("collector_configuration_bound_invalid")
    return config


def load_config(path):
    return validate_config(read_json(path))
