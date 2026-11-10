from __future__ import annotations

import re

from .bridge import ALLOWED_NETWORKS

MNEMONIC_RE = re.compile(r"^[a-z]+(?: [a-z]+){11,23}$")


def is_mnemonic(value: str | None) -> bool:
    if not value:
        return False
    words = " ".join(value.strip().lower().split())
    count = len(words.split(" "))
    return bool(MNEMONIC_RE.match(words)) and count in {12, 15, 18, 21, 24}


def normalize_network(value: str | None) -> str:
    network = (value or "MAINNET").strip().upper()
    return network if network in ALLOWED_NETWORKS else "MAINNET"


def parse_account_number(value: str | int | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None
