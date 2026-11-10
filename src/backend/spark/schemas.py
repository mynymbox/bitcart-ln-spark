from api.schemas.base import Schema


class SparkSettings(Schema):
    network: str = "MAINNET"
    include_spark_invoice: bool = False
    account_number: str = ""
    sweep_enabled: bool = False
    sweep_threshold_sats: str = "200000"
    sweep_min_sats: str = "100000"
    sweep_reserve_sats: str = "0"
    sweep_max_fee_percent: str = "3"
    sweep_exit_speed: str = "MEDIUM"
    sweep_address: str = ""
