"""
Common utilities for SLIM v0.6.0 examples.

This module provides shared functionality for both server and client:
- Identity management (shared secret and JWT)
- SLIM application initialization
- Configuration helpers
- Logging utilities

Based on SLIM v0.6.0 documentation and examples.
"""

import base64
import json
import logging
from typing import Tuple, Optional, List, Dict, Any

import slim_bindings

logger = logging.getLogger(__name__)


def split_identity(identity: str) -> slim_bindings.PyName:
    """
    Split identity string into PyName components.

    Args:
        identity: Identity in format "org/namespace/app"

    Returns:
        PyName object

    Raises:
        ValueError: If identity format is invalid
    """
    try:
        org, namespace, app = identity.split("/")
        return slim_bindings.PyName(org, namespace, app)
    except ValueError as e:
        raise ValueError(
            f"Identity must be in format 'org/namespace/app', got: {identity}"
        ) from e


def create_shared_secret_identity(
    identity: str, secret: str
) -> Tuple[slim_bindings.PyIdentityProvider, slim_bindings.PyIdentityVerifier]:
    """
    Create identity provider and verifier for shared secret authentication.

    Args:
        identity: Identity string
        secret: Shared secret

    Returns:
        Tuple of (provider, verifier)
    """
    provider = slim_bindings.PyIdentityProvider.SharedSecret(
        identity=identity, shared_secret=secret
    )
    verifier = slim_bindings.PyIdentityVerifier.SharedSecret(
        identity=identity, shared_secret=secret
    )
    return provider, verifier


def create_jwt_identity(
    jwt_path: str,
    spire_bundle_path: str,
    issuer: Optional[str] = None,
    subject: Optional[str] = None,
    audience: Optional[List[str]] = None,
) -> Tuple[slim_bindings.PyIdentityProvider, slim_bindings.PyIdentityVerifier]:
    """
    Create JWT-based identity provider and verifier.

    Args:
        jwt_path: Path to JWT token file
        spire_bundle_path: Path to SPIRE trust bundle
        issuer: Optional issuer claim to enforce
        subject: Optional subject claim to enforce
        audience: Optional audience list

    Returns:
        Tuple of (provider, verifier)
    """
    logger.info(f"Using JWT from: {jwt_path}")
    logger.info(f"Using SPIRE bundle: {spire_bundle_path}")

    # Read and parse SPIRE bundle
    with open(spire_bundle_path) as f:
        spire_bundle = json.load(f)

    # Extract and combine all JWKS from trust domains
    all_keys = []
    for trust_domain, encoded_jwks in spire_bundle.items():
        logger.info(f"Processing trust domain: {trust_domain}")
        try:
            decoded_jwks = base64.b64decode(encoded_jwks)
            jwks_json = json.loads(decoded_jwks)

            if "keys" in jwks_json:
                all_keys.extend(jwks_json["keys"])
                logger.info(
                    f"  Added {len(jwks_json['keys'])} keys from {trust_domain}"
                )
            else:
                logger.warning(f"  No 'keys' found in JWKS for {trust_domain}")

        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
            raise RuntimeError(
                f"Failed to process trust domain {trust_domain}: {e}"
            ) from e

    # Create combined JWKS
    combined_jwks = json.dumps({"keys": all_keys})
    logger.info(
        f"Combined JWKS contains {len(all_keys)} total keys from {len(spire_bundle)} trust domains"
    )

    # Create provider (static JWT)
    provider = slim_bindings.PyIdentityProvider.StaticJwt(path=jwt_path)

    # Create public key for verifier
    public_key = slim_bindings.PyKey(
        algorithm=slim_bindings.PyAlgorithm.RS256,
        format=slim_bindings.PyKeyFormat.Jwks,
        key=slim_bindings.PyKeyData.Content(content=combined_jwks),
    )

    # Create verifier
    verifier = slim_bindings.PyIdentityVerifier.Jwt(
        public_key=public_key,
        issuer=issuer,
        audience=[audience] if audience else None,
        subject=subject,
    )

    return provider, verifier


async def initialize_tracing(
    enable_opentelemetry: bool = False, log_level: str = "info"
):
    """
    Initialize SLIM tracing.

    Args:
        enable_opentelemetry: Enable OpenTelemetry export
        log_level: Logging level
    """
    tracing_config = {
        "log_level": log_level,
        "opentelemetry": {
            "enabled": enable_opentelemetry,
            "grpc": {"endpoint": "http://localhost:4317"},
        },
    }

    await slim_bindings.init_tracing(tracing_config)

    if enable_opentelemetry:
        logger.info("OpenTelemetry tracing enabled")
    else:
        logger.info("OpenTelemetry tracing disabled")


async def create_slim_app(
    local_identity: str,
    shared_secret: Optional[str] = None,
    jwt_path: Optional[str] = None,
    spire_bundle_path: Optional[str] = None,
    audience: Optional[List[str]] = None,
    enable_opentelemetry: bool = False,
) -> slim_bindings.Slim:
    """
    Create and initialize a SLIM application.

    Args:
        local_identity: Local identity string
        shared_secret: Shared secret for auth (if using shared secret mode)
        jwt_path: Path to JWT token (if using JWT mode)
        spire_bundle_path: Path to SPIRE bundle (if using JWT mode)
        audience: Audience list for JWT verification
        enable_opentelemetry: Enable OpenTelemetry tracing

    Returns:
        Initialized SLIM application
    """
    # Initialize tracing
    await initialize_tracing(enable_opentelemetry)

    # Determine authentication method
    if jwt_path and spire_bundle_path and audience:
        logger.info("Using JWT authentication")
        provider, verifier = create_jwt_identity(
            jwt_path, spire_bundle_path, aud=audience
        )
    elif shared_secret:
        logger.info("Using shared secret authentication (development only)")
        provider, verifier = create_shared_secret_identity(
            local_identity, shared_secret
        )
    else:
        raise ValueError(
            "Must provide either (jwt_path + spire_bundle_path + audience) or shared_secret"
        )

    # Convert identity to PyName
    local_name = split_identity(local_identity)

    # Create SLIM application
    slim_app = await slim_bindings.Slim.new(local_name, provider, verifier)

    logger.info(f"Created SLIM app with ID: {slim_app.id_str}")
    return slim_app


def format_message(prefix: str, message: str = "") -> str:
    """Format a message with consistent styling."""
    return f"{prefix:<45} {message}"


def print_formatted(prefix: str, message: str = ""):
    """Print a formatted message."""
    print(format_message(prefix, message))


class SlimConfig:
    """Configuration helper for SLIM applications."""

    def __init__(self, config_dict: Optional[Dict[str, Any]] = None):
        self.config = config_dict or {}

    @property
    def endpoint(self) -> str:
        return self.config.get("endpoint", "127.0.0.1:46357")

    @property
    def tls_insecure(self) -> bool:
        return self.config.get("tls", {}).get("insecure", True)

    def to_connection_config(self) -> Dict[str, Any]:
        """Convert to SLIM connection configuration."""
        return {
            "endpoint": f"http://{self.endpoint}"
            if not self.endpoint.startswith("http")
            else self.endpoint,
            "tls": {"insecure": self.tls_insecure},
        }

    def to_server_config(self) -> Dict[str, Any]:
        """Convert to SLIM server configuration."""
        return {"endpoint": self.endpoint, "tls": {"insecure": self.tls_insecure}}


# Color codes for terminal output
class Colors:
    """ANSI color codes for terminal styling."""

    PURPLE = "\033[95m"
    CYAN = "\033[96m"
    DARKCYAN = "\033[36m"
    BLUE = "\033[94m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"
    END = "\033[0m"


def colorize(text: str, color: str) -> str:
    """Add color to text."""
    return f"{color}{text}{Colors.END}"


def print_status(status: str, message: str = "", color: str = Colors.CYAN):
    """Print a status message with color."""
    colored_status = colorize(f"{status}:", color + Colors.BOLD)
    print(f"{colored_status:<55} {message}")
