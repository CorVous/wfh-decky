"""Recognising and unwrapping DE inventory JSON (config/shared/inventoryPayload.ts).

inventory.php answers with the inventory object itself, but exports and other
tools wrap it in envelopes ({"InventoryJson": "<json text>"}, {"data": {...}}),
sometimes several levels deep and sometimes as a JSON string.
"""

from __future__ import annotations

import json
from typing import Any, Callable

DEFAULT_MAX_UNWRAP_DEPTH = 4
MAX_SAFE_UNWRAP_DEPTH = 12

INVENTORY_ARRAY_KEYS = ("Suits", "RawUpgrades", "Upgrades", "Arcanes", "LevelKeys", "MiscItems")
ENVELOPE_KEYS = ("InventoryJson", "inventoryJson", "inventory_json", "payload", "data")


class InventoryFormatError(ValueError):
    pass


def has_inventory_shape(value: Any) -> bool:
    return isinstance(value, dict) and any(isinstance(value.get(k), list) for k in INVENTORY_ARRAY_KEYS)


def _first_envelope_value(record: dict) -> Any:
    for key in ENVELOPE_KEYS:
        if key in record:  # JSON null counts as present, as in WFHelper
            return record[key]
    return None


def _normalize_depth(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_UNWRAP_DEPTH
    if n < 1:
        return DEFAULT_MAX_UNWRAP_DEPTH
    return min(n, MAX_SAFE_UNWRAP_DEPTH)


def unwrap_inventory_payload(value: Any, return_input_on_failure: bool = False,
                             on_parse_error: Callable[[Exception], None] | None = None,
                             max_depth: Any = DEFAULT_MAX_UNWRAP_DEPTH) -> Any:
    current = value
    for _ in range(_normalize_depth(max_depth)):
        if has_inventory_shape(current):
            return current
        if not isinstance(current, (dict, list)):
            return value if return_input_on_failure else current
        nxt = _first_envelope_value(current) if isinstance(current, dict) else None
        if isinstance(nxt, str):
            try:
                current = json.loads(nxt)
                continue
            except ValueError as e:
                if on_parse_error:
                    on_parse_error(e)
                return value if return_input_on_failure else current
        if isinstance(nxt, (dict, list)):
            current = nxt
            continue
        return current
    return current


def unwrap_inventory_text(text: str) -> str | None:
    """The inventory's own JSON text inside a payload (None when it sits under an
    object envelope rather than a string one)."""
    current = text
    for _ in range(MAX_SAFE_UNWRAP_DEPTH):
        value = json.loads(current)
        if has_inventory_shape(value):
            return current
        if not isinstance(value, dict):
            return None
        nxt = _first_envelope_value(value)
        if not isinstance(nxt, str):
            return None
        current = nxt
    return None


def parse_inventory_bytes(raw: bytes | str) -> dict:
    """Parse inventory JSON (any supported envelope); raises InventoryFormatError."""
    try:
        text = raw.decode("utf-8-sig") if isinstance(raw, (bytes, bytearray)) else raw
        data = unwrap_inventory_payload(json.loads(text))
    except (ValueError, UnicodeDecodeError) as e:
        raise InventoryFormatError(f"not JSON: {e}") from e
    if not has_inventory_shape(data):
        raise InventoryFormatError("Inventory JSON does not contain expected inventory arrays")
    return data
