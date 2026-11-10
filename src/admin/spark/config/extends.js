import HideNodeInfo from "@Spark/components/HideNodeInfo.vue"

export default {
  extendComponents: {
    checkout_payment_qr: [HideNodeInfo],
  },
  dictionaries: {
    server_management_nav: [{ text: "Lightning Spark", to: "/spark" }],
    plugin_fields: {
      spark: {
        network: {
          title: "Spark network",
          description:
            "MAINNET for live Bitcoin, or REGTEST for Spark development.",
          type: "string",
        },
        include_spark_invoice: {
          title: "Accept Spark wallets",
          description:
            "Leave off. Embedding a Spark invoice in the BOLT11 makes the checkout QR too dense to scan. Spark wallets can still pay the Lightning invoice.",
          type: "checkbox",
        },
        account_number: {
          title: "Account number",
          description:
            "Optional Spark account index. Leave empty to use the SDK default (1 on mainnet, 0 on regtest).",
          type: "string",
        },
        sweep_enabled: {
          title: "Sweep Spark to on-chain Bitcoin",
          description:
            "When the Spark balance crosses the threshold, cooperatively exit to a Bitcoin address. Off until you turn it on.",
          type: "checkbox",
        },
        sweep_threshold_sats: {
          title: "Sweep threshold (sats)",
          description:
            "Start an automatic sweep when spendable Spark balance reaches this many sats. Default 200000. Raise this on mainnet so exit fees stay a small percentage.",
          type: "string",
        },
        sweep_min_sats: {
          title: "Minimum sweep (sats)",
          description:
            "Never sweep below this amount, including Sweep now. Default 100000.",
          type: "string",
        },
        sweep_reserve_sats: {
          title: "Leave behind (sats)",
          description: "Keep this many sats on Spark after a sweep. Default 0.",
          type: "string",
        },
        sweep_max_fee_percent: {
          title: "Max exit fee percent",
          description:
            "Refuse a sweep if the cooperative-exit fee is above this percent of the amount. Default 3. Cannot exceed 50.",
          type: "string",
        },
        sweep_exit_speed: {
          title: "Exit speed",
          description: "FAST, MEDIUM, or SLOW cooperative exit. Default MEDIUM.",
          type: "string",
        },
        sweep_address: {
          title: "On-chain Bitcoin address",
          description:
            "Destination for sweeps (bc1… on mainnet). Leave empty to use a fresh address from a BTC wallet owned by the same user as the Spark wallet.",
          type: "string",
        },
      },
    },
  },
}
