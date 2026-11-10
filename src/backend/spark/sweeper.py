from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select

from api import models
from api.db import AsyncSession
from api.logging import get_logger
from api.plugins import get_plugin_logger
from api.services.coins import CoinService
from dishka import Scope

from .bridge import SparkBridgeClient, SparkBridgeError
from .schemas import SparkSettings
from .util import is_mnemonic, normalize_network, parse_account_number

try:
    logger = get_plugin_logger(__name__)
except Exception:
    logger = get_logger(__name__)

BECH32_RE = re.compile(r"^(bc|tb|bcrt)1[02-9ac-hj-np-z]{8,87}$", re.IGNORECASE)
BASE58_RE = re.compile(r"^[13][a-km-zA-HJ-NP-Z1-9]{25,34}$")
DONE_STATUSES = frozenset({"SUCCEEDED", "SUCCESS", "COMPLETE", "COMPLETED", "CONFIRMED"})
FAILED_STATUSES = frozenset({"FAILED", "FAILURE", "EXPIRED", "CANCELLED", "CANCELED"})
EXIT_SPEEDS = frozenset({"FAST", "MEDIUM", "SLOW"})
HARD_FEE_CAP = Decimal("50")
DEFAULT_FEE_PERCENT = Decimal("3")
PENDING_STALE_SECONDS = 30 * 60


class SweepError(Exception):
    pass


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_sats(value: Any, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def parse_percent(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        parsed = DEFAULT_FEE_PERCENT
    if parsed <= 0:
        parsed = DEFAULT_FEE_PERCENT
    return min(parsed, HARD_FEE_CAP)


def parse_exit_speed(value: Any) -> str:
    speed = str(value or "MEDIUM").strip().upper()
    return speed if speed in EXIT_SPEEDS else "MEDIUM"


def is_btc_address(address: str, network: str) -> bool:
    addr = address.strip()
    if not addr:
        return False
    if network == "REGTEST":
        return addr.lower().startswith("bcrt1") and bool(BECH32_RE.match(addr))
    if addr.lower().startswith("tb1") or addr.lower().startswith("bcrt1"):
        return False
    return bool(BECH32_RE.match(addr) or BASE58_RE.match(addr))


class Sweeper:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.client = SparkBridgeClient()

    def state_path(self) -> str:
        os.makedirs(self.plugin.data_dir(), exist_ok=True)
        return os.path.join(self.plugin.data_dir(), "sweep_state.json")

    def load_state(self) -> dict[str, Any]:
        path = self.state_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def save_state(self, state: dict[str, Any]) -> None:
        path = self.state_path()
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
        os.replace(tmp, path)

    def record_error(self, message: str, *, kind: str = "error") -> dict[str, Any]:
        state = self.load_state()
        last_error = state.get("last_error") if isinstance(state.get("last_error"), dict) else {}
        if last_error.get("kind") == kind:
            last_error["count"] = int(last_error.get("count") or 1) + 1
            last_error["updated_at"] = utcnow()
            last_error["message"] = message
        else:
            last_error = {
                "kind": kind,
                "message": message,
                "count": 1,
                "created_at": utcnow(),
                "updated_at": utcnow(),
            }
        state["last_error"] = last_error
        self.save_state(state)
        logger.warning("Spark sweep skipped: %s", message)
        return state

    def clear_error(self, state: dict[str, Any]) -> dict[str, Any]:
        state.pop("last_error", None)
        return state

    async def spark_wallets(self) -> list[models.Wallet]:
        async with self.plugin.container(scope=Scope.REQUEST) as container:
            session = await container.get(AsyncSession)
            result = await session.execute(select(models.Wallet).where(models.Wallet.currency == "spark"))
            return list(result.scalars().all())

    async def unused_btc_address(self, network: str, user_id: str | None) -> str | None:
        if not user_id:
            return None
        try:
            async with self.plugin.container(scope=Scope.REQUEST) as container:
                session = await container.get(AsyncSession)
                coin_service = await container.get(CoinService)
                result = await session.execute(
                    select(models.Wallet).where(
                        models.Wallet.currency == "btc",
                        models.Wallet.user_id == user_id,
                    )
                )
                wallet = result.scalars().first()
                if wallet is None:
                    return None
                coin = await coin_service.get_coin(
                    "btc",
                    {
                        "xpub": wallet.xpub,
                        "contract": wallet.contract,
                        **(wallet.additional_xpub_data or {}),
                    },
                )
                address = await coin.server.getunusedaddress()
        except Exception:
            return None
        if isinstance(address, str) and is_btc_address(address, network):
            return address.strip()
        return None

    async def resolve_address(
        self, settings: SparkSettings, network: str, user_id: str | None
    ) -> str:
        configured = (settings.sweep_address or "").strip()
        if configured:
            if not is_btc_address(configured, network):
                raise SweepError("Sweep address is not valid for the selected Spark network")
            return configured
        generated = await self.unused_btc_address(network, user_id)
        if generated:
            return generated
        raise SweepError(
            "Set an on-chain Bitcoin sweep address, or add a BTC wallet owned by the same user"
        )

    def sweep_amount(self, available: int, settings: SparkSettings, *, ignore_threshold: bool) -> int:
        threshold = parse_sats(settings.sweep_threshold_sats, 200000)
        minimum = parse_sats(settings.sweep_min_sats, 100000)
        reserve = parse_sats(settings.sweep_reserve_sats, 0)
        if minimum < 20000:
            minimum = 20000
        if threshold < minimum:
            threshold = minimum
        spendable = available - reserve
        if spendable < minimum:
            raise SweepError(
                f"Balance {available} sats is below the minimum sweep of {minimum} sats after reserve"
            )
        if not ignore_threshold and spendable < threshold:
            raise SweepError(
                f"Balance {available} sats is below the sweep threshold of {threshold} sats"
            )
        return spendable

    def fee_sats_for_speed(self, quote: dict[str, Any], speed: str) -> tuple[str, int]:
        key = speed.lower()
        bucket = quote.get(key) if isinstance(quote.get(key), dict) else {}
        total = parse_sats(bucket.get("totalSats"), 0)
        quote_id = str(quote.get("id") or "")
        if not quote_id or total < 0:
            raise SweepError("Spark fee quote was incomplete")
        return quote_id, total

    def enforce_fee_guard(self, amount_sats: int, fee_sats: int, max_percent: Decimal) -> None:
        if amount_sats <= 0:
            raise SweepError("Sweep amount must be positive")
        if fee_sats * 2 >= amount_sats:
            raise SweepError(
                f"Exit fee {fee_sats} sats is at least half of the sweep ({amount_sats} sats); refusing"
            )
        allowed = int((Decimal(amount_sats) * max_percent / Decimal(100)).quantize(Decimal("1")))
        if fee_sats > allowed:
            raise SweepError(
                f"Exit fee {fee_sats} sats exceeds {max_percent}% of {amount_sats} sats ({allowed} sats); "
                "raise the threshold or the max fee percent"
            )

    async def refresh_pending(
        self,
        state: dict[str, Any],
        mnemonic: str,
        network: str,
        account_number: int | None,
    ) -> dict[str, Any]:
        pending = state.get("pending")
        if not isinstance(pending, dict) or not pending.get("id"):
            return state
        try:
            status = await self.client.get_coop_exit_request(
                mnemonic, network, account_number, str(pending["id"])
            )
        except SparkBridgeError as exc:
            created = pending.get("created_at") or ""
            if _is_stale(created):
                pending["status"] = "unknown"
                pending["message"] = str(exc)
                state["last"] = pending
                state.pop("pending", None)
                self.save_state(state)
            return state
        pending["status"] = status.get("status")
        pending["txid"] = status.get("txid")
        pending["updated_at"] = utcnow()
        code = str(status.get("status") or "").upper()
        if code in DONE_STATUSES:
            pending["status"] = "succeeded"
            state["last"] = pending
            state.pop("pending", None)
            state = self.clear_error(state)
            self.save_state(state)
            logger.info(
                "Spark sweep completed: %s sats to %s txid=%s",
                pending.get("amount_sats"),
                pending.get("address"),
                pending.get("txid"),
            )
        elif code in FAILED_STATUSES:
            pending["status"] = "failed"
            state["last"] = pending
            state.pop("pending", None)
            self.record_error(f"Cooperative exit failed ({code})", kind="exit_failed")
        else:
            if _is_stale(pending.get("created_at") or ""):
                pending["status"] = "stale"
                state["last"] = pending
                state.pop("pending", None)
                self.record_error("Pending sweep went stale without a final Spark status", kind="stale")
            else:
                self.save_state(state)
        return self.load_state()

    async def sweep_wallet(
        self,
        wallet: models.Wallet,
        settings: SparkSettings,
        *,
        ignore_threshold: bool,
    ) -> dict[str, Any]:
        mnemonic = (wallet.xpub or "").strip()
        if not is_mnemonic(mnemonic):
            raise SweepError("Spark wallet mnemonic is missing or invalid")
        network = normalize_network(settings.network)
        account_number = parse_account_number(settings.account_number)
        state = self.load_state()
        state = await self.refresh_pending(state, mnemonic, network, account_number)
        if state.get("pending"):
            raise SweepError("A sweep is already in progress; wait for Spark to finish it")
        available = await self.client.balance_sats(mnemonic, network, account_number)
        amount_sats = self.sweep_amount(available, settings, ignore_threshold=ignore_threshold)
        address = await self.resolve_address(settings, network, wallet.user_id)
        speed = parse_exit_speed(settings.sweep_exit_speed)
        max_percent = parse_percent(settings.sweep_max_fee_percent)
        quote = await self.client.get_withdrawal_fee_quote(
            mnemonic, network, account_number, amount_sats, address
        )
        quote_id, fee_sats = self.fee_sats_for_speed(quote, speed)
        self.enforce_fee_guard(amount_sats, fee_sats, max_percent)
        withdrawn = await self.client.withdraw(
            mnemonic,
            network,
            account_number,
            address,
            amount_sats,
            speed,
            quote_id,
            fee_sats,
        )
        pending = {
            "id": withdrawn.get("id"),
            "status": withdrawn.get("status") or "pending",
            "txid": withdrawn.get("txid"),
            "amount_sats": amount_sats,
            "fee_sats": fee_sats,
            "address": address,
            "wallet_id": wallet.id,
            "created_at": utcnow(),
            "updated_at": utcnow(),
        }
        state = self.load_state()
        state["pending"] = pending
        state = self.clear_error(state)
        self.save_state(state)
        logger.info(
            "Spark sweep submitted: %s sats to %s fee=%s id=%s",
            amount_sats,
            address,
            fee_sats,
            pending.get("id"),
        )
        return pending

    async def process(self, *, ignore_threshold: bool = False) -> dict[str, Any]:
        settings = await self.plugin.settings()
        if not ignore_threshold and not settings.sweep_enabled:
            return {"ok": True, "skipped": True, "reason": "Sweeping is off"}
        wallets = await self.spark_wallets()
        if not wallets:
            if ignore_threshold:
                raise SweepError("Create a Lightning Spark wallet first")
            return {"ok": True, "skipped": True, "reason": "No Spark wallet"}
        last: dict[str, Any] | None = None
        errors: list[str] = []
        for wallet in wallets:
            try:
                last = await self.sweep_wallet(wallet, settings, ignore_threshold=ignore_threshold)
            except SweepError as exc:
                errors.append(str(exc))
                self.record_error(str(exc), kind=_error_kind(str(exc)))
            except SparkBridgeError as exc:
                errors.append(str(exc))
                self.record_error(str(exc), kind="bridge")
        state = self.load_state()
        if last:
            return {"ok": True, "pending": last, "state": public_state(state)}
        if ignore_threshold:
            raise SweepError(errors[0] if errors else "Sweep did not start")
        return {"ok": True, "skipped": True, "state": public_state(state)}

    def status(self) -> dict[str, Any]:
        return public_state(self.load_state())


def public_state(state: dict[str, Any]) -> dict[str, Any]:
    pending = state.get("pending") if isinstance(state.get("pending"), dict) else None
    last = state.get("last") if isinstance(state.get("last"), dict) else None
    last_error = state.get("last_error") if isinstance(state.get("last_error"), dict) else None
    return {
        "pending": _public_sweep(pending),
        "last": _public_sweep(last),
        "last_error": last_error,
    }


def _public_sweep(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not record:
        return None
    return {
        "id": record.get("id"),
        "status": record.get("status"),
        "txid": record.get("txid"),
        "amount_sats": record.get("amount_sats"),
        "fee_sats": record.get("fee_sats"),
        "address": record.get("address"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
    }


def _is_stale(created_at: str) -> bool:
    if not created_at:
        return False
    try:
        started = datetime.fromisoformat(created_at)
    except ValueError:
        return True
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - started).total_seconds() > PENDING_STALE_SECONDS


def _error_kind(message: str) -> str:
    lowered = message.lower()
    if "threshold" in lowered:
        return "below_threshold"
    if "minimum" in lowered:
        return "below_minimum"
    if "fee" in lowered:
        return "fee_guard"
    if "address" in lowered:
        return "address"
    if "progress" in lowered:
        return "in_progress"
    return "error"
