from __future__ import annotations

import asyncio
import hashlib
import os
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from aiohttp import ClientSession, ClientTimeout, ContentTypeError

ALLOWED_NETWORKS = frozenset({"MAINNET", "REGTEST"})
COMPLETED_STATUSES = frozenset({"TRANSFER_COMPLETED"})
_SECRET_RETRY_ATTEMPTS = 30
_SECRET_RETRY_DELAY = 0.5

_sessions: dict[str, ClientSession] = {}
_wallet_sessions: dict[str, str] = {}
_secret_lock: asyncio.Lock | None = None
_wallet_session_lock: asyncio.Lock | None = None
_cached_secret = ""


class SparkBridgeError(Exception):
    pass


def default_bridge_url() -> str:
    return os.environ.get("SPARK_BRIDGE_URL", "http://spark:5055").rstrip("/")


def _validate_bridge_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise SparkBridgeError("Invalid Spark bridge URL")
    if not parsed.hostname:
        raise SparkBridgeError("Invalid Spark bridge URL")
    return url.rstrip("/")


def _session_key(mnemonic: str, network: str, account_number: int | None) -> str:
    return hashlib.sha256(f"{mnemonic}|{network}|{account_number or ''}".encode()).hexdigest()


def _lock(slot: str) -> asyncio.Lock:
    global _secret_lock, _wallet_session_lock
    if slot == "secret":
        if _secret_lock is None:
            _secret_lock = asyncio.Lock()
        return _secret_lock
    if _wallet_session_lock is None:
        _wallet_session_lock = asyncio.Lock()
    return _wallet_session_lock


def _read_secret_file(path: str) -> str:
    try:
        stored = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return stored if len(stored) >= 16 else ""


async def _bridge_secret() -> str:
    global _cached_secret
    if _cached_secret:
        return _cached_secret
    async with _lock("secret"):
        if _cached_secret:
            return _cached_secret
        env = os.environ.get("SPARK_BRIDGE_SECRET", "").strip()
        if len(env) >= 16:
            _cached_secret = env
            return _cached_secret
        path = os.environ.get("SPARK_BRIDGE_AUTH_FILE", "").strip()
        if path:
            for _ in range(_SECRET_RETRY_ATTEMPTS):
                stored = _read_secret_file(path)
                if stored:
                    _cached_secret = stored
                    return _cached_secret
                await asyncio.sleep(_SECRET_RETRY_DELAY)
        raise SparkBridgeError("Spark bridge secret is not configured")


class SparkBridgeClient:
    def __init__(self, url: str | None = None) -> None:
        self.url = _validate_bridge_url(url or default_bridge_url())

    async def session(self) -> ClientSession:
        session = _sessions.get(self.url)
        if session is None or session.closed:
            session = ClientSession(timeout=ClientTimeout(total=60))
            _sessions[self.url] = session
        return session

    async def close(self) -> None:
        session = _sessions.pop(self.url, None)
        if session is not None and not session.closed:
            await session.close()

    async def _headers(self) -> dict[str, str]:
        secret = await _bridge_secret()
        return {
            "X-Spark-Bridge-Secret": secret,
            "Authorization": f"Bearer {secret}",
        }

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        session = await self.session()
        try:
            async with session.post(
                f"{self.url}/rpc",
                json={"method": method, "params": params},
                headers=await self._headers(),
            ) as response:
                try:
                    data = await response.json()
                except ContentTypeError as exc:
                    raise SparkBridgeError("Spark bridge returned a non-JSON response") from exc
                status = response.status
        except SparkBridgeError:
            raise
        except Exception as exc:
            raise SparkBridgeError("Spark bridge is unreachable") from exc
        if not isinstance(data, dict):
            raise SparkBridgeError("Spark bridge returned invalid data")
        if status == 401:
            raise SparkBridgeError("Spark bridge rejected the shared secret")
        if status >= 400 or data.get("error"):
            raise SparkBridgeError(str(data.get("error") or "Spark bridge request failed"))
        result = data.get("result")
        if not isinstance(result, dict):
            raise SparkBridgeError("Spark bridge returned invalid data")
        return result

    async def _ensure_session(
        self, mnemonic: str, network: str, account_number: int | None
    ) -> str:
        key = _session_key(mnemonic, network, account_number)
        existing = _wallet_sessions.get(key)
        if existing:
            return existing
        async with _lock("session"):
            existing = _wallet_sessions.get(key)
            if existing:
                return existing
            result = await self._rpc(
                "openWallet",
                {
                    "mnemonic": mnemonic,
                    "network": network,
                    "accountNumber": account_number,
                },
            )
            session_id = result.get("sessionId")
            if not isinstance(session_id, str) or not session_id:
                raise SparkBridgeError("Spark bridge did not return a session")
            _wallet_sessions[key] = session_id
            return session_id

    def _forget_session(self, mnemonic: str, network: str, account_number: int | None) -> None:
        _wallet_sessions.pop(_session_key(mnemonic, network, account_number), None)

    async def _wallet_rpc(
        self,
        method: str,
        mnemonic: str,
        network: str,
        account_number: int | None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = extra or {}

        async def call() -> dict[str, Any]:
            session_id = await self._ensure_session(mnemonic, network, account_number)
            return await self._rpc(method, {"sessionId": session_id, **payload})

        try:
            return await call()
        except SparkBridgeError as exc:
            message = str(exc).lower()
            if "secret" in message or "unauthorized" in message:
                raise
            if "session" not in message:
                raise
            self._forget_session(mnemonic, network, account_number)
            return await call()

    async def validate(self, mnemonic: str, network: str, account_number: int | None) -> bool:
        try:
            result = await self._wallet_rpc("validate", mnemonic, network, account_number)
        except SparkBridgeError:
            return False
        return bool(result.get("ok"))

    async def address(self, mnemonic: str, network: str, account_number: int | None) -> str:
        result = await self._wallet_rpc("address", mnemonic, network, account_number)
        address = result.get("address")
        if not isinstance(address, str) or not address:
            raise SparkBridgeError("Spark bridge did not return an address")
        return address

    async def balance(self, mnemonic: str, network: str, account_number: int | None) -> Decimal:
        result = await self._wallet_rpc("balance", mnemonic, network, account_number)
        sats = Decimal(str(result.get("sats") or 0))
        return sats / Decimal(10**8)

    async def create_lightning_invoice(
        self,
        mnemonic: str,
        network: str,
        account_number: int | None,
        amount_sats: int,
        memo: str,
        expiry_seconds: int,
        include_spark_invoice: bool,
    ) -> dict[str, Any]:
        return await self._wallet_rpc(
            "createLightningInvoice",
            mnemonic,
            network,
            account_number,
            {
                "amountSats": amount_sats,
                "memo": memo,
                "expirySeconds": expiry_seconds,
                "includeSparkInvoice": include_spark_invoice,
            },
        )

    async def check_payment(
        self,
        mnemonic: str,
        network: str,
        account_number: int | None,
        request_id: str,
    ) -> bool:
        result = await self._wallet_rpc(
            "getLightningReceiveRequest",
            mnemonic,
            network,
            account_number,
            {"id": request_id},
        )
        if result.get("paid") is True:
            return True
        return str(result.get("status") or "") in COMPLETED_STATUSES

    async def generate_wallet(self, network: str, account_number: int | None) -> dict[str, Any]:
        return await self._rpc(
            "generateWallet",
            {
                "network": network,
                "accountNumber": account_number,
            },
        )

    async def balance_sats(self, mnemonic: str, network: str, account_number: int | None) -> int:
        result = await self._wallet_rpc("balance", mnemonic, network, account_number)
        return int(Decimal(str(result.get("sats") or 0)))

    async def get_withdrawal_fee_quote(
        self,
        mnemonic: str,
        network: str,
        account_number: int | None,
        amount_sats: int,
        withdrawal_address: str,
    ) -> dict[str, Any]:
        return await self._wallet_rpc(
            "getWithdrawalFeeQuote",
            mnemonic,
            network,
            account_number,
            {
                "amountSats": amount_sats,
                "withdrawalAddress": withdrawal_address,
            },
        )

    async def withdraw(
        self,
        mnemonic: str,
        network: str,
        account_number: int | None,
        onchain_address: str,
        amount_sats: int,
        exit_speed: str,
        fee_quote_id: str,
        fee_amount_sats: int,
    ) -> dict[str, Any]:
        return await self._wallet_rpc(
            "withdraw",
            mnemonic,
            network,
            account_number,
            {
                "onchainAddress": onchain_address,
                "amountSats": amount_sats,
                "exitSpeed": exit_speed,
                "feeQuoteId": fee_quote_id,
                "feeAmountSats": fee_amount_sats,
                "deductFeeFromWithdrawalAmount": True,
            },
        )

    async def get_coop_exit_request(
        self,
        mnemonic: str,
        network: str,
        account_number: int | None,
        request_id: str,
    ) -> dict[str, Any]:
        return await self._wallet_rpc(
            "getCoopExitRequest",
            mnemonic,
            network,
            account_number,
            {"id": request_id},
        )
