"""Verified official discovery metadata; no guessed authenticated tool mapping.

Schema fingerprints must come from a separately reviewed, authenticated
tools/list response. Validation here neither authenticates nor enables writes.
"""

from dataclasses import dataclass
import hashlib
import json
import re

from atlas.domain import BrokerCapabilities, BrokerError, UnsupportedCapability

MCP_ENDPOINT = "https://agent.robinhood.com/mcp/trading"
AUTHORIZATION_METADATA_URL = "https://agent.robinhood.com/.well-known/oauth-authorization-server"
PROTECTED_RESOURCE_METADATA_URL = "https://agent.robinhood.com/.well-known/oauth-protected-resource/mcp/trading"
OAUTH_METADATA = {
    "issuer": MCP_ENDPOINT,
    "authorization_endpoint": "https://robinhood.com/oauth",
    "registration_endpoint": "https://agent.robinhood.com/oauth/trading/register",
    "token_endpoint": "https://api.robinhood.com/oauth2/token/",
    "grant_types_supported": ("authorization_code", "refresh_token"),
    "response_types_supported": ("code",),
    "code_challenge_methods_supported": ("S256",),
    "token_endpoint_auth_methods_supported": ("none",),
    "scopes_supported": ("internal",),
}
DOCUMENTED_TOOLS = frozenset({
    "get_accounts", "get_portfolio", "get_realized_pnl", "get_pnl_trade_history",
    "get_equity_positions", "get_option_positions", "get_crypto_positions",
    "get_equity_quotes", "get_option_quotes", "get_crypto_quotes",
    "get_equity_orders", "get_option_orders", "get_crypto_orders",
    "review_equity_order", "review_option_order", "preview_crypto_order",
    "place_equity_order", "place_option_order", "place_crypto_order",
    "cancel_equity_order", "cancel_option_order", "cancel_crypto_order",
})


@dataclass(frozen=True)
class VerifiedToolSchema:
    name: str
    sha256: str


def schema_fingerprint(schema: dict) -> str:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise BrokerError("agentic_schema_invalid")
    try:
        serialized = json.dumps(schema, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise BrokerError("agentic_schema_invalid") from None
    return hashlib.sha256(serialized.encode()).hexdigest()


def validate_tool_discovery(*, endpoint: str, response: dict,
                            expected_schema_hashes: dict[str, str]) -> tuple[VerifiedToolSchema, ...]:
    if endpoint != MCP_ENDPOINT:
        raise BrokerError("agentic_endpoint_mismatch")
    if not isinstance(expected_schema_hashes, dict) or not expected_schema_hashes:
        raise UnsupportedCapability("agentic_schema_mapping_unverified")
    if any(name not in DOCUMENTED_TOOLS or not isinstance(digest, str)
           or not re.fullmatch(r"[0-9a-f]{64}", digest)
           for name, digest in expected_schema_hashes.items()):
        raise BrokerError("agentic_schema_pin_invalid")
    if not isinstance(response, dict) or not isinstance(response.get("tools"), list):
        raise BrokerError("agentic_discovery_invalid")
    found, seen = {}, set()
    for tool in response["tools"]:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or tool["name"] in seen:
            raise BrokerError("agentic_discovery_invalid")
        name = tool["name"]
        seen.add(name)
        if name in expected_schema_hashes:
            fingerprint = schema_fingerprint(tool.get("inputSchema"))
            if fingerprint != expected_schema_hashes[name]:
                raise BrokerError("agentic_schema_changed")
            found[name] = VerifiedToolSchema(name, fingerprint)
    if found.keys() != expected_schema_hashes.keys():
        raise BrokerError("agentic_tool_missing")
    return tuple(found[name] for name in sorted(found))


class RobinhoodAgentic:
    capabilities = BrokerCapabilities(
        broker="robinhood_agentic", asset_classes=("equity", "crypto"), account_modes=("real",),
        order_types=(), cancellation=False, client_order_lookup=False,
        official=True, network_enabled=False,
        limitations=("Authenticated schema mapping unverified; all adapter operations unavailable.",
                     "Official service also supports options; ATLAS has no options contract mapping.",
                     "No login, credential storage, remote tool proxy, or execution is implemented."),
    )

    def get_account(self):
        raise UnsupportedCapability("agentic_schema_mapping_unverified")

    def get_quote(self, symbol, quantity=None, side="buy"):
        raise UnsupportedCapability("agentic_schema_mapping_unverified")

    def get_open_orders(self):
        raise UnsupportedCapability("agentic_schema_mapping_unverified")

    def submit_order(self, order):
        raise UnsupportedCapability("agentic_execution_unavailable")

    def cancel_order(self, broker_order_id):
        raise UnsupportedCapability("agentic_execution_unavailable")

    def get_order_status(self, broker_order_id=None, client_order_id=None):
        raise UnsupportedCapability("agentic_schema_mapping_unverified")
