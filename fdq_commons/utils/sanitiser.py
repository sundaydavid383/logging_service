"""
fdq_commons/utils/sanitiser.py
--------------------------------
Input sanitisation and PII masking utilities for the FDQ platform.

Two concerns from the spec are handled here:

1. Log Injection Prevention (§11.3)
   Free-text fields (error_message, action, failure_reason, resolution_note

2. PII Field Masking (§11.1)
   GET endpoints must not expose raw BVN, NIN, account numbers, or phone
   numbers to callers with logs:read scope. Masking is applied at serialisation
   time by Pydantic validators, not in route logic, so it cannot be bypassed.
"""

from __future__ import annotations

import html
import re
from typing import Annotated, Any, Callable

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_INJECTION_CHARS: tuple[str, ...] = ("\n", "\r", "\x00", "\x1b")
_STRUCTURED_FIELD_RE = re.compile(r"[^a-zA-Z0-9_\-\.\s]")
_FREE_TEXT_MAX_LEN = 10_000


# ---------------------------------------------------------------------------
# Core Sanitisation Engines
# ---------------------------------------------------------------------------

def sanitise_free_text(
    value: str | None,
    *,
    max_len: int = _FREE_TEXT_MAX_LEN,
    html_escape: bool = True,
) -> str | None:
    if value is None:
        return None

    cleaned = str(value)
    for char in _INJECTION_CHARS:
        cleaned = cleaned.replace(char, " ")

    if html_escape:
        cleaned = html.escape(cleaned, quote=True)

    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len]

    return cleaned.strip()


def sanitise_structured_field(value: str | None) -> str | None:
    if value is None:
        return None
    return _STRUCTURED_FIELD_RE.sub("", str(value)).strip()


# ---------------------------------------------------------------------------
# PII Masking Algorithms (Safe-Guarded for length anomalies)
# ---------------------------------------------------------------------------

def mask_bvn(value: str | None) -> str | None:
    if not value:
        return value
    s = str(value).strip()
    if len(s) < 11:
        return "***"
    return s[:3] + "****" + s[-4:]


def mask_nin(value: str | None) -> str | None:
    return mask_bvn(value)


def mask_account_number(value: str | None) -> str | None:
    if not value:
        return value
    s = str(value).strip()
    if len(s) < 10:
        return "***"
    return s[:3] + "****" + s[-3:]


def mask_phone_number(value: str | None) -> str | None:
    if not value:
        return value
    s = str(value).strip()
    if len(s) < 7:
        return "***"
    return s[:4] + "*" * (max(1, len(s) - 7)) + s[-3:]


def mask_email(value: str | None) -> str | None:
    if not value:
        return value
    s = str(value).strip()
    if "@" not in s:
        return "****"
    parts = s.split("@", 1)
    if len(parts) != 2:
        return "***"
    local, domain = parts
    if len(local) <= 2:
        return "**@" + domain
    visible = max(2, len(local) // 3)
    return local[:visible] + "***" + "@" + domain


# ---------------------------------------------------------------------------
# Recursive Metadata / Payload Cleaner
# ---------------------------------------------------------------------------

_PII_MASK_MAP: dict[str, Callable[[str | None], str | None]] = {
    "bvn": mask_bvn,
    "nin": mask_nin,
    "account_number": mask_account_number,
    "account_no": mask_account_number,
    "phone": mask_phone_number,
    "phone_number": mask_phone_number,
    "mobile": mask_phone_number,
    "email": mask_email,
    "email_address": mask_email,
}


def apply_pii_mask(data: dict[str, Any] | None) -> dict[str, Any] | None:
    if data is None:
        return None

    result: dict[str, Any] = {}
    for key, value in data.items():
        lower_key = key.lower()
        if lower_key in _PII_MASK_MAP:
            result[key] = _PII_MASK_MAP[lower_key](str(value)) if value is not None else None
        elif isinstance(value, dict):
            result[key] = apply_pii_mask(value)
        elif isinstance(value, list):
            result[key] = [
                apply_pii_mask(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# Pydantic v2 Serialization Type Factory
# ---------------------------------------------------------------------------

class _PiiMaskedSerializationType:
    """
    Pydantic V2 wrapper that leaves database input modifications untouched,
    but dynamically masks strings during outgoing JSON serialization based on 
    the configured masking function. Fully handles nullable values.
    """
    def __init__(self, masker: Callable[[str | None], str | None]):
        self.masker = masker

    def __get_pydantic_core_schema__(
        self, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        # Construct base schema that accepts strings or safely coerces inputs (like numbers)
        base_schema = core_schema.chain_schema([
            core_schema.str_schema(),
            core_schema.plain_validator_function_schema(lambda v: str(v).strip())
        ])
        
        return core_schema.nullable_schema(
            core_schema.json_or_python_schema(
                json_schema=base_schema,
                python_schema=core_schema.union_schema([
                    core_schema.is_instance_schema(str),
                    core_schema.is_instance_schema(int),
                ]),
                serialization=core_schema.plain_serializer_function_schema(
                    lambda v: self.masker(v) if v is not None else None
                )
            )
        )


class _SanitisedFreeTextType:
    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return core_schema.nullable_schema(
            core_schema.no_info_plain_validator_function(
                lambda v: sanitise_free_text(str(v)) if v is not None else None
            )
        )


class _SanitisedStructuredType:
    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return core_schema.nullable_schema(
            core_schema.no_info_plain_validator_function(
                lambda v: sanitise_structured_field(str(v)) if v is not None else None
            )
        )


# ---------------------------------------------------------------------------
# Canonical Type Aliases for Schemas
# ---------------------------------------------------------------------------

SanitisedFreeText = Annotated[str | None, _SanitisedFreeTextType]
SanitisedStructuredField = Annotated[str | None, _SanitisedStructuredType]

# Precise specialized outgoing serialization types instantiated correctly
MaskedBVN = Annotated[str | None, _PiiMaskedSerializationType(mask_bvn)]
MaskedNIN = Annotated[str | None, _PiiMaskedSerializationType(mask_nin)]
MaskedAccount = Annotated[str | None, _PiiMaskedSerializationType(mask_account_number)]
MaskedPhone = Annotated[str | None, _PiiMaskedSerializationType(mask_phone_number)]
MaskedEmail = Annotated[str | None, _PiiMaskedSerializationType(mask_email)]