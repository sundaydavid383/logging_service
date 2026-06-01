"""
fdq_commons/utils/ip_validator.py
----------------------------------
IP address validation utilities for the FDQ platform.

The spec requires every actor_ip_address field to be validated server-side
before storage (§3.3.1, §5.3.1). PostgreSQL stores them as INET type which
enforces valid addresses at the DB level too — but we validate early so we
can return a proper 422 with field-level detail rather than a raw DB error.

Also provides a Pydantic-compatible type annotation so schemas can declare:
    actor_ip_address: IPvAnyAddressStr

Usage:
    from fdq_commons.utils.ip_validator import validate_ip_address, IPvAnyAddressStr

    # Standalone validation:
    ip = validate_ip_address("192.168.1.1")   # returns str
    ip = validate_ip_address("not-an-ip")      # raises ValueError

    # In a Pydantic model:
    class MyModel(BaseModel):
        actor_ip_address: IPvAnyAddressStr
"""

from __future__ import annotations

import ipaddress
from typing import Annotated, Any

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, PydanticCustomError, core_schema


# ---------------------------------------------------------------------------
# Core validation function
# ---------------------------------------------------------------------------

def validate_ip_address(value: str | None) -> str:
    """
    Validate that *value* is a well-formed IPv4 or IPv6 address.

    Returns the normalised string representation (compressed IPv6).
    Raises ValueError with a descriptive message on failure.
    The caller (Pydantic or route handler) converts this to a 422 response.
    """
    if value is None:
        raise ValueError("IP address is required and cannot be null.")

    raw = str(value).strip()

    if not raw:
        raise ValueError("IP address must not be blank.")

    try:
        parsed = ipaddress.ip_address(raw)
    except ValueError:
        raise ValueError(
            f"'{raw}' is not a valid IPv4 or IPv6 address. "
            "Expected formats: '192.168.1.1' or '2001:db8::1'."
        )

    return str(parsed)   # normalised (e.g., "::1" not "0:0:0:0:0:0:0:1")


def is_valid_ip(value: str) -> bool:
    """Non-raising convenience check — returns True/False."""
    try:
        validate_ip_address(value)
        return True
    except ValueError:
        return False


def is_private_ip(value: str) -> bool:
    """Return True if the IP is in a private/reserved range."""
    try:
        return ipaddress.ip_address(value).is_private
    except ValueError:
        return False


def is_loopback_ip(value: str) -> bool:
    """Return True if the IP is a loopback address (127.x.x.x / ::1)."""
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Pydantic v2 custom type
# ---------------------------------------------------------------------------

class _IPvAnyAddressType:
    """
    Custom Pydantic v2 type that validates and normalises IP addresses.
    Use as a type annotation in Pydantic models:

        actor_ip_address: IPvAnyAddressStr

    This ensures that even if Pydantic v2's built-in IPvAnyAddress is not
    directly used, our validation logic runs consistently everywhere.
    """

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        return core_schema.no_info_plain_validator_function(
            cls._validate,
            serialization=core_schema.to_string_ser_schema(),
        )

    @classmethod
    def _validate(cls, value: Any) -> str:
        # Accept ipaddress objects directly (e.g., from psycopg2 INET adapter)
        if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
            return str(value)
        
        # Coerce safe string variations (including bytes/bytearrays from proxy protocols)
        if isinstance(value, (str, bytes, bytearray)):
            if isinstance(value, (bytes, bytearray)):
                value = value.decode("utf-8", errors="ignore")
            try:
                return validate_ip_address(value)
            except ValueError as e:
                raise PydanticCustomError("ip_address_invalid", str(e))
                
        raise PydanticCustomError(
            "ip_address_type_error",
            f"Expected a string or network IP address object, got {type(value).__name__}."
        )


# The canonical annotation to use uniformly across your Pydantic schemas
IPvAnyAddressStr = Annotated[str, _IPvAnyAddressType]