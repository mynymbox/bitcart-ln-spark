from typing import Any

from fastapi import APIRouter, Security

from api import models, utils
from api.constants import AuthScopes
from api.plugins import DIRoute

from .bridge import SparkBridgeError
from .schemas import SparkSettings

router = APIRouter(route_class=DIRoute)


@router.get("/status")
async def spark_status(
    user: models.User = Security(
        utils.authorization.auth_dependency, scopes=[AuthScopes.SERVER_MANAGEMENT]
    ),
) -> dict[str, Any]:
    from .plugin import get_instance

    plugin = get_instance()
    if plugin is None:
        return {"ready": False, "error": "Plugin is not loaded"}
    settings = await plugin.settings()
    return {
        "ready": True,
        "network": settings.network,
        "include_spark_invoice": settings.include_spark_invoice,
    }


@router.post("/generate-wallet")
async def generate_wallet(
    user: models.User = Security(
        utils.authorization.auth_dependency, scopes=[AuthScopes.SERVER_MANAGEMENT]
    ),
) -> dict[str, Any]:
    from .bridge import SparkBridgeClient
    from .plugin import get_instance

    plugin = get_instance()
    if plugin is None:
        return {"error": "Plugin is not loaded"}
    network, account_number = await plugin.wallet_options()
    try:
        created = await SparkBridgeClient().generate_wallet(network, account_number)
    except SparkBridgeError as exc:
        return {"error": str(exc)}
    mnemonic = created.get("mnemonic")
    address = created.get("address")
    if not isinstance(mnemonic, str) or not isinstance(address, str):
        return {"error": "Spark bridge did not return a wallet"}
    return {"mnemonic": mnemonic, "address": address, "network": network}


@router.get("/sweep-status")
async def sweep_status(
    user: models.User = Security(
        utils.authorization.auth_dependency, scopes=[AuthScopes.SERVER_MANAGEMENT]
    ),
) -> dict[str, Any]:
    from .plugin import get_instance

    plugin = get_instance()
    if plugin is None:
        return {"error": "Plugin is not loaded"}
    settings = await plugin.settings()
    status = plugin.sweep_status()
    return {
        "enabled": settings.sweep_enabled,
        "threshold_sats": settings.sweep_threshold_sats,
        "address": settings.sweep_address,
        **status,
    }


@router.post("/sweep-now")
async def sweep_now(
    user: models.User = Security(
        utils.authorization.auth_dependency, scopes=[AuthScopes.SERVER_MANAGEMENT]
    ),
) -> dict[str, Any]:
    from .plugin import get_instance
    from .sweeper import SweepError

    plugin = get_instance()
    if plugin is None:
        return {"error": "Plugin is not loaded"}
    try:
        return await plugin.sweep_now()
    except SweepError as exc:
        return {"error": str(exc)}
    except SparkBridgeError as exc:
        return {"error": str(exc)}


@router.get("/checkout-config")
async def checkout_config(
    user: models.User = Security(
        utils.authorization.auth_dependency, scopes=[AuthScopes.SERVER_MANAGEMENT]
    ),
) -> dict[str, Any]:
    from .plugin import get_instance

    plugin = get_instance()
    if plugin is None:
        defaults = SparkSettings()
        return {
            "network": defaults.network,
            "include_spark_invoice": defaults.include_spark_invoice,
        }
    settings = await plugin.settings()
    return {
        "network": settings.network,
        "include_spark_invoice": settings.include_spark_invoice,
    }
