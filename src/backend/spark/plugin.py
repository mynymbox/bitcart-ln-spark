from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, cast

from fastapi import FastAPI

from api import models, utils
from api.logging import get_logger
from api.plugins import SKIP_PAYMENT_METHOD, BaseCoin, BasePlugin, CoinServer, DIContainer
from api.services.crud.invoices import InvoiceService
from api.services.payment_processor import PaymentProcessor

from .bridge import SparkBridgeClient, SparkBridgeError
from .schemas import SparkSettings
from .util import is_mnemonic, normalize_network, parse_account_number
from .views import router

logger = get_logger(__name__)

_instance: Plugin | None = None


def bolt11_for_qr(invoice: str) -> str:
    text = invoice.strip()
    if text.lower().startswith("lightning:"):
        text = text.split(":", 1)[1].strip()
    # BIP-173: Bech32 in QR codes must be uppercase (alphanumeric mode).
    return text.upper()


def get_instance() -> Plugin | None:
    return _instance


class SparkCoinServer(CoinServer):
    client: SparkBridgeClient

    def __init__(self, currency: str, xpub: str | None, **additional_data: Any) -> None:
        super().__init__(currency, xpub, **additional_data)
        self.client = SparkBridgeClient()
        self.mnemonic = (xpub or "").strip()


class SparkCoin(BaseCoin):
    server: SparkCoinServer

    coin_name = "spark"
    friendly_name = "Lightning Spark"
    xpub_name = "Spark mnemonic"
    server_cls = SparkCoinServer

    rate_rules = "SPARK_X = BTC_X"

    async def validate_key(self, key: str, *args: Any, **kwargs: Any) -> bool:
        if not is_mnemonic(key):
            return False
        plugin = get_instance()
        network, account_number = await plugin.wallet_options() if plugin else ("MAINNET", None)
        try:
            return await self.server.client.validate(key.strip(), network, account_number)
        except SparkBridgeError:
            return False

    async def balance(self) -> dict[str, Decimal]:
        plugin = get_instance()
        network, account_number = await plugin.wallet_options() if plugin else ("MAINNET", None)
        confirmed = Decimal(0)
        if self.server.mnemonic:
            try:
                confirmed = await self.server.client.balance(
                    self.server.mnemonic, network, account_number
                )
            except SparkBridgeError:
                confirmed = Decimal(0)
        return {
            "confirmed": confirmed,
            "unconfirmed": Decimal(0),
            "unmatured": Decimal(0),
            "lightning": Decimal(0),
        }

    @property
    async def node_id(self) -> str | None:
        plugin = get_instance()
        network, account_number = await plugin.wallet_options() if plugin else ("MAINNET", None)
        if not self.server.mnemonic:
            return None
        try:
            return await self.server.client.address(
                self.server.mnemonic, network, account_number
            )
        except SparkBridgeError:
            return None

    async def get_request(self, request_id: str) -> dict[str, Any]:
        plugin = get_instance()
        network, account_number = await plugin.wallet_options() if plugin else ("MAINNET", None)
        paid = False
        if self.server.mnemonic:
            try:
                paid = await self.server.client.check_payment(
                    self.server.mnemonic, network, account_number, request_id
                )
            except SparkBridgeError:
                paid = False
        return {
            "status": "complete" if paid else "pending",
            "tx_hashes": [],
            "tx_amounts": {},
            "sent_amount": Decimal(0),
        }

    get_invoice = get_request


class Plugin(BasePlugin):
    name = "spark"

    def setup_app(self, app: FastAPI) -> None:
        app.include_router(router, prefix="/spark", tags=["spark"])

    async def startup(self) -> None:
        global _instance
        _instance = self
        self.sweep_lock = asyncio.Lock()
        self.register_settings(SparkSettings)
        self.context.register_filter("get_cryptos", self.register_method)
        self.context.register_filter("get_coin", self.get_coin)
        self.context.register_filter("create_payment_method", self.create_payment_method)
        self.context.register_filter("get_coin_explorer", self.get_explorer)
        self.context.register_filter("get_divisibility", self.get_divisibility)
        self.context.register_filter("get_wallet_symbol", self.get_wallet_symbol)

    async def shutdown(self) -> None:
        global _instance
        _instance = None

    async def worker_setup(self) -> None:
        if not hasattr(self, "sweep_lock"):
            self.sweep_lock = asyncio.Lock()
        asyncio.ensure_future(utils.common.run_repeated(self.process_pending, 2, 0))
        asyncio.ensure_future(utils.common.run_repeated(self.process_sweeps, 120, 30))

    async def process_sweeps(self) -> None:
        if not hasattr(self, "sweep_lock"):
            self.sweep_lock = asyncio.Lock()
        try:
            async with self.sweep_lock:
                from .sweeper import Sweeper

                await Sweeper(self).process()
        except Exception:
            logger.exception("Spark sweep pass failed")

    async def sweep_now(self) -> dict[str, Any]:
        if not hasattr(self, "sweep_lock"):
            self.sweep_lock = asyncio.Lock()
        async with self.sweep_lock:
            from .sweeper import Sweeper

            return await Sweeper(self).process(ignore_threshold=True)

    def sweep_status(self) -> dict[str, Any]:
        from .sweeper import Sweeper

        return Sweeper(self).status()

    async def settings(self) -> SparkSettings:
        loaded = await self.get_plugin_settings()
        if loaded is None:
            return SparkSettings()
        return SparkSettings(**loaded.model_dump())

    async def wallet_options(self) -> tuple[str, int | None]:
        settings = await self.settings()
        return normalize_network(settings.network), parse_account_number(settings.account_number)

    async def register_method(self, cryptos: dict[str, BaseCoin]) -> dict[str, BaseCoin]:
        cryptos["spark"] = SparkCoin()
        return cryptos

    async def get_coin(self, coin: BaseCoin, currency: str, xpub: dict[str, Any] | str | None) -> BaseCoin:
        if currency == "spark":
            if isinstance(xpub, dict):
                return SparkCoin(**xpub)
            return SparkCoin(xpub=xpub)
        return coin

    async def get_explorer(self, explorer: str, coin: str) -> str:
        if coin == "spark":
            return "https://www.sparkscan.io/"
        return explorer

    async def get_divisibility(self, divisibility: int, wallet: models.Wallet, coin: BaseCoin) -> int:
        if wallet.currency == "spark":
            return 8
        return divisibility

    async def get_wallet_symbol(self, symbol: str, wallet: models.Wallet, coin: BaseCoin) -> str:
        if wallet.currency == "spark":
            return "btc"
        return symbol

    async def create_payment_method(
        self,
        method: dict[str, Any] | None,
        wallet: models.Wallet,
        original_coin: BaseCoin,
        amount: Decimal,
        invoice: models.Invoice,
        product: models.Product | None,
        store: models.Store,
        lightning: bool,
    ) -> dict[str, Any] | object | None:
        if wallet.currency != "spark":
            return method
        if lightning:
            return SKIP_PAYMENT_METHOD
        network, account_number = await self.wallet_options()
        coin = cast(SparkCoin, original_coin)
        mnemonic = (coin.server.mnemonic or "").strip()
        if not is_mnemonic(mnemonic):
            return SKIP_PAYMENT_METHOD
        sats = int((amount * Decimal(10**8)).quantize(Decimal("1")))
        if sats < 1:
            return SKIP_PAYMENT_METHOD
        expire_minutes = int(getattr(invoice, "expiration", 15) or 15)
        expiry_seconds = max(60, min(expire_minutes * 60, 30 * 24 * 3600))
        # Keep the BOLT11 short enough for Bitcart's checkout QR. Embedding a
        # Spark invoice (includeSparkInvoice) makes a payload phones cannot scan.
        try:
            created = await coin.server.client.create_lightning_invoice(
                mnemonic,
                network,
                account_number,
                sats,
                "",
                expiry_seconds,
                False,
            )
        except SparkBridgeError:
            return SKIP_PAYMENT_METHOD
        bolt11 = created.get("invoice")
        request_id = created.get("id")
        payment_hash = created.get("paymentHash")
        if not isinstance(bolt11, str) or not bolt11 or not isinstance(request_id, str):
            return SKIP_PAYMENT_METHOD
        qr_invoice = bolt11_for_qr(bolt11)
        return {
            "currency": "spark",
            "payment_address": qr_invoice,
            "payment_url": f"lightning:{qr_invoice}",
            "lookup_field": request_id,
            "rhash": payment_hash if isinstance(payment_hash, str) else None,
            "lightning": False,
            "metadata": {"spark_request_id": request_id},
        }

    async def process_pending(self) -> None:
        payment_processor = await self.container.get(PaymentProcessor)
        await payment_processor.check_pending("SPARK", process_func=self.process_payment)

    async def process_payment(
        self,
        invoice: models.Invoice,
        method: models.PaymentMethod,
        wallet: models.Wallet,
        status: str,
        tx_hashes: list[str],
        sent_amount: Decimal,
        tx_amounts: dict[str, str] | None,
        *,
        di_context: DIContainer,
    ) -> bool:
        invoice_service = await di_context.get(InvoiceService)
        paid_amount = method.amount if status == "complete" else Decimal(0)
        await invoice_service.update_status(
            invoice, status, method, tx_hashes, paid_amount, tx_amounts=tx_amounts
        )
        return True
