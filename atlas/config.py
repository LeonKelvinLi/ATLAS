"""Explicit external configuration. Importing this module reads no environment/files."""

from dataclasses import dataclass, field, fields
import json
import os
from pathlib import Path
import stat
import time

from atlas.domain import BrokerCapabilities, Quote, UnsupportedCapability, decimal
from atlas.risk import RiskPolicy

PROJECT = Path(__file__).resolve().parents[1]


class ConfigError(ValueError):
    pass


def external_path(value, *, project=PROJECT) -> Path:
    if not isinstance(value, (str, Path)):
        raise ConfigError("explicit external path required")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ConfigError("configuration and state paths must be absolute")
    resolved = path.resolve()
    if resolved.is_relative_to(Path(project).resolve()):
        raise ConfigError("configuration, secrets, and operational state must be outside the repository")
    return resolved


def read_secret_file(value) -> bytes:
    original = Path(value).expanduser()
    external_path(original)
    try:
        before = original.lstat()
        if stat.S_ISLNK(before.st_mode):
            raise ConfigError("secret files must not be symlinks")
        fd = os.open(original, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ConfigError("secret files must be owned by this user with mode 0600")
            value = os.read(fd, 65537)
        finally:
            os.close(fd)
        if not value or len(value) > 65536:
            raise ConfigError("secret file size is invalid")
        return value
    except OSError:
        raise ConfigError("secret file cannot be read securely") from None


def _json_file(path, *, max_bytes=20_000_000):
    try:
        with external_path(path).open("rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.getuid()}
                    or stat.S_IMODE(info.st_mode) & 0o022):
                raise ConfigError("configuration data must be trusted-owner regular files without group/other writes")
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ConfigError("configuration data exceeds size limit")
        def reject_constant(_):
            raise ConfigError("nonfinite JSON is not allowed")
        def unique(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ConfigError("duplicate JSON configuration key")
                result[key] = value
            return result
        return json.loads(data, parse_constant=reject_constant, object_pairs_hook=unique)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ConfigError("external JSON could not be read") from None


def _flag(environ, name):
    value = environ.get(name, "false")
    if value not in {"true", "false"}:
        raise ConfigError("enable flags must be exactly true or false")
    return value == "true"


def _secret_path(value):
    # Validate containment without erasing the path the secure reader must
    # inspect. Resolving a supplied symlink here would bypass O_NOFOLLOW.
    external_path(value)
    return Path(value).expanduser()


@dataclass(frozen=True)
class PlatformConfig:
    mode: str
    state_dir: Path
    broker: str
    policy: RiskPolicy = field(repr=False)
    paper: dict = field(default_factory=dict, repr=False)
    datasets_file: Path | None = None
    quotes_file: Path | None = None
    approval_key_file: Path | None = field(default=None, repr=False)
    http_bearer_file: Path | None = field(default=None, repr=False)
    host: str = "127.0.0.1"
    port: int = 8765
    execution_tools: bool = False

    @property
    def scope(self):
        return "live" if self.mode.startswith("live-") else self.mode

    @property
    def ledger_path(self):
        return self.state_dir / self.scope / "execution.db"


def load_config(path, *, environ=None) -> PlatformConfig:
    env = os.environ if environ is None else environ
    data = _json_file(path, max_bytes=1_000_000)
    allowed = {"mode", "state_dir", "broker", "risk", "paper", "datasets_file", "quotes_file", "http"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise ConfigError("unknown configuration fields")
    mode = data.get("mode", "research")
    if mode not in {"research", "paper", "live-assisted", "live-auto"}:
        raise ConfigError("unsupported execution mode")
    broker = data.get("broker", "unavailable" if mode == "research" else "paper")
    if broker not in {"unavailable", "paper", "robinhood_crypto", "robinhood_agentic"}:
        raise ConfigError("unknown broker adapter")
    raw_risk = data.get("risk", {})
    protected = {"mode", "expected_account_id", "live_enabled", "auto_enabled"}
    if not isinstance(raw_risk, dict) or set(raw_risk) - {f.name for f in fields(RiskPolicy)} or set(raw_risk) & protected:
        raise ConfigError("invalid or protected risk configuration fields")
    paper = data.get("paper", {})
    if not isinstance(paper, dict) or set(paper) - {"account_id", "cash", "fee_bps"}:
        raise ConfigError("invalid paper simulator configuration")
    expected = paper.get("account_id") if broker == "paper" else None
    if broker == "paper":
        if set(paper) != {"account_id", "cash", "fee_bps"} or not isinstance(expected, str) or not expected:
            raise ConfigError("paper account, synthetic cash, and fee assumption must be explicit")
        try:
            decimal(paper["cash"], nonnegative=True)
            decimal(paper["fee_bps"], nonnegative=True)
        except ValueError:
            raise ConfigError("invalid paper financial configuration") from None
    live = _flag(env, "ATLAS_LIVE_TRADING_ENABLED")
    auto = _flag(env, "ATLAS_LIVE_AUTO_ENABLED")
    if mode.startswith("live-"):
        if broker in {"paper", "unavailable"}:
            raise ConfigError("live mode requires a real broker adapter")
        account_file = env.get("ATLAS_EXPECTED_ACCOUNT_FILE")
        if not account_file:
            raise ConfigError("live mode requires an external expected account file")
        try:
            expected = read_secret_file(account_file).decode("utf-8").strip()
        except UnicodeError:
            raise ConfigError("expected account file is invalid") from None
        if not expected or len(expected) > 512:
            raise ConfigError("expected account file is invalid")
    try:
        policy = RiskPolicy(mode=mode, expected_account_id=expected, live_enabled=live, auto_enabled=auto, **raw_risk)
    except (ValueError, TypeError):
        raise ConfigError("risk policy validation failed") from None
    http = data.get("http", {})
    if not isinstance(http, dict) or set(http) - {"host", "port", "execution_tools"}:
        raise ConfigError("invalid HTTP configuration")
    host, port, execution = http.get("host", "127.0.0.1"), http.get("port", 8765), http.get("execution_tools", False)
    if host not in {"127.0.0.1", "::1"} or type(port) is not int or not 1 <= port <= 65535 or type(execution) is not bool:
        raise ConfigError("HTTP requires a loopback host, valid port, and boolean execution_tools")
    if execution and not (mode == "paper" or (mode.startswith("live-") and live and (mode != "live-auto" or auto))):
        raise ConfigError("execution tool registration requires explicit mode and enable flags")
    paths = {name: external_path(data[name]) if data.get(name) is not None else None
             for name in ("datasets_file", "quotes_file")}
    return PlatformConfig(
        mode, external_path(data.get("state_dir")), broker, policy, paper=paper, **paths,
        approval_key_file=_secret_path(env["ATLAS_APPROVAL_KEY_FILE"]) if env.get("ATLAS_APPROVAL_KEY_FILE") else None,
        http_bearer_file=_secret_path(env["ATLAS_HTTP_BEARER_FILE"]) if env.get("ATLAS_HTTP_BEARER_FILE") else None,
        host=host, port=port, execution_tools=execution,
    )


class UnavailableBroker:
    capabilities = BrokerCapabilities(
        broker="unconfigured", asset_classes=(), account_modes=(), order_types=(),
        cancellation=False, client_order_lookup=False, fractional_quantities=False,
        limitations=("No broker or account is configured; balances and holdings are unavailable.",),
    )

    def get_account(self):
        raise UnsupportedCapability("broker_not_configured")

    def get_quote(self, *args, **kwargs):
        raise UnsupportedCapability("broker_not_configured")

    def get_open_orders(self):
        raise UnsupportedCapability("broker_not_configured")

    def submit_order(self, *args, **kwargs):
        raise UnsupportedCapability("execution_unavailable")

    def cancel_order(self, *args, **kwargs):
        raise UnsupportedCapability("execution_unavailable")

    def get_order_status(self, *args, **kwargs):
        raise UnsupportedCapability("broker_not_configured")


def build_service(config: PlatformConfig, *, clock=time.time):
    """Explicit CLI factory. Real network-adapter assembly is not implemented."""
    from atlas.brokers.paper import PaperBroker
    from atlas.evidence import EvidenceStore
    from atlas.ledger import Ledger
    from atlas.service import AtlasService
    from atlas.strategies import validate_bars

    if config.mode.startswith("live-") or config.broker not in {"paper", "unavailable"}:
        raise UnsupportedCapability("real_broker_deployment_integration_unavailable")
    datasets = _json_file(config.datasets_file) if config.datasets_file is not None else {}
    if not isinstance(datasets, dict) or len(datasets) > 100:
        raise ConfigError("invalid configured datasets")
    for dataset in datasets.values():
        if not isinstance(dataset, dict) or dataset.get("asset_class") not in {"equity", "crypto", "fx_binary"}:
            raise ConfigError("invalid dataset contract")
        if not isinstance(dataset.get("bars"), list) or len(dataset["bars"]) > 100_000:
            raise ConfigError("invalid dataset size")
        validate_bars(dataset["bars"], now=int(clock()))
    quotes = {}
    if config.broker == "paper":
        if config.quotes_file is None:
            raise ConfigError("paper mode requires an explicit external quote fixture file")
        raw_quotes = _json_file(config.quotes_file)
        if not isinstance(raw_quotes, dict) or len(raw_quotes) > 100:
            raise ConfigError("invalid quote fixtures")
        try:
            for symbol, raw in raw_quotes.items():
                values = dict(raw)
                if values.get("symbol") != symbol:
                    raise ValueError
                for key in ("bid", "ask", "available_quantity", "fee_per_unit", "quantity_increment", "minimum_quantity"):
                    if values.get(key) is not None:
                        values[key] = decimal(values[key], nonnegative=True)
                values["source"] = "operator_supplied_paper_fixture"
                values["timestamp_basis"] = "synthetic"
                quotes[symbol] = Quote(**values)
        except (ValueError, TypeError):
            raise ConfigError("invalid synthetic quote fields") from None
    ledger = Ledger(config.ledger_path, config.scope, clock=clock)
    try:
        broker = (PaperBroker(quotes, cash=decimal(config.paper["cash"]),
                              account_id=config.paper["account_id"], fee_bps=decimal(config.paper["fee_bps"]),
                              path=config.state_dir / "paper" / "simulator.db", clock=clock)
                  if config.broker == "paper" else UnavailableBroker())
        return AtlasService(broker, ledger, config.policy, datasets=datasets,
                            evidence=EvidenceStore(PROJECT, config.state_dir),
                            kill_switch_path=config.state_dir / "KILL_SWITCH", clock=clock)
    except Exception:
        ledger.close()
        raise
