<template>
  <v-container>
    <h1 class="text-h4 mb-4">Lightning Spark</h1>
    <v-alert type="info" class="mb-4">
      Create a wallet with currency <strong>Lightning Spark</strong>. Paste the
      12-word Spark mnemonic into the wallet field. Then attach that wallet to
      your store. Checkout will show a Lightning invoice that settles into Spark.
    </v-alert>
    <v-alert v-if="error" type="error" class="mb-4">
      {{ error }}
    </v-alert>
    <v-alert v-else-if="loading" type="info" class="mb-4">
      Loading Spark settings…
    </v-alert>
    <div v-else>
      <PolicySetting
        v-for="(meta, setting) in fields"
        :key="setting"
        :title="meta.title"
        :detail="meta.description"
        :type="meta.type || 'checkbox'"
        :what="setting"
        :policy-url="'/plugins/settings/spark'"
        :initial-value="settings[setting]"
      />
      <v-card class="mt-6" outlined>
        <v-card-title>Generate a Spark mnemonic</v-card-title>
        <v-card-text>
          <p>
            This creates a new Spark wallet on the selected network. Copy the
            mnemonic into a Lightning Spark wallet in Bitcart, and store a backup
            offline. The mnemonic is the spending key.
          </p>
          <v-btn
            color="primary"
            :loading="generating"
            :disabled="generating"
            @click="generateWallet"
          >
            Generate mnemonic
          </v-btn>
          <v-alert v-if="generateError" type="error" class="mt-4">
            {{ generateError }}
          </v-alert>
          <div v-if="generated" class="mt-4">
            <div class="mb-2">
              <strong>Network:</strong> {{ generated.network }}
            </div>
            <div class="mb-2">
              <strong>Spark address:</strong>
              <code>{{ generated.address }}</code>
            </div>
            <div>
              <strong>Mnemonic:</strong>
              <code class="d-block mt-1">{{ generated.mnemonic }}</code>
            </div>
          </div>
        </v-card-text>
      </v-card>
      <v-card class="mt-6" outlined>
        <v-card-title>On-chain sweep</v-card-title>
        <v-card-text>
          <p>
            Like Flint, this is a cooperative Spark exit to Bitcoin. It does not
            unilaterally exit. Turn sweeping on, set a threshold, and send to an
            on-chain address you control. The worker checks about every two minutes.
          </p>
          <v-btn
            color="primary"
            class="mb-4"
            :loading="sweeping"
            :disabled="sweeping"
            @click="sweepNow"
          >
            Sweep now
          </v-btn>
          <v-alert v-if="sweepError" type="error" class="mb-4">
            {{ sweepError }}
          </v-alert>
          <v-alert v-if="sweepMessage" type="success" class="mb-4">
            {{ sweepMessage }}
          </v-alert>
          <div v-if="sweepStatus">
            <div class="mb-2">
              <strong>Automatic sweeps:</strong>
              {{ sweepStatus.enabled ? "on" : "off" }}
            </div>
            <div v-if="sweepStatus.pending" class="mb-2">
              <strong>In progress:</strong>
              {{ sweepStatus.pending.amount_sats }} sats to
              <code>{{ sweepStatus.pending.address }}</code>
              ({{ sweepStatus.pending.status }})
            </div>
            <div v-if="sweepStatus.last" class="mb-2">
              <strong>Last sweep:</strong>
              {{ sweepStatus.last.status }}
              {{ sweepStatus.last.amount_sats }} sats
              <span v-if="sweepStatus.last.txid">
                txid <code>{{ sweepStatus.last.txid }}</code>
              </span>
            </div>
            <div v-if="sweepStatus.last_error">
              <strong>Last refusal:</strong>
              {{ sweepStatus.last_error.message }}
              <span v-if="sweepStatus.last_error.count > 1">
                ({{ sweepStatus.last_error.count }} times)
              </span>
            </div>
          </div>
        </v-card-text>
      </v-card>
    </div>
  </v-container>
</template>

<script>
import PolicySetting from "@/components/PolicySetting.vue"

export default {
  components: {
    PolicySetting,
  },
  layout: "admin",
  middleware: "superuserOnly",
  data() {
    return {
      loading: true,
      generating: false,
      sweeping: false,
      error: "",
      generateError: "",
      sweepError: "",
      sweepMessage: "",
      generated: null,
      sweepStatus: null,
      settings: {},
      fields: {
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
    }
  },
  async beforeMount() {
    try {
      const resp = await this.$axios.get("/plugins/settings/spark")
      this.settings = resp.data || {}
      await this.loadSweepStatus()
    } catch (e) {
      this.error =
        "Spark backend settings are not loaded. Confirm the plugin is installed, click Reload Plugins, and wait for Docker to rebuild (the Spark bridge image is built on first reload)."
    } finally {
      this.loading = false
    }
  },
  methods: {
    async loadSweepStatus() {
      try {
        const resp = await this.$axios.get("/spark/sweep-status")
        this.sweepStatus = resp.data || null
      } catch (e) {
        this.sweepStatus = null
      }
    },
    async generateWallet() {
      this.generating = true
      this.generateError = ""
      this.generated = null
      try {
        const resp = await this.$axios.post("/spark/generate-wallet")
        if (resp.data && resp.data.error) {
          this.generateError = resp.data.error
          return
        }
        this.generated = resp.data
      } catch (e) {
        this.generateError =
          "Could not generate a Spark wallet. After Reload Plugins, confirm the spark container is running (`docker compose ps`)."
      } finally {
        this.generating = false
      }
    },
    async sweepNow() {
      this.sweeping = true
      this.sweepError = ""
      this.sweepMessage = ""
      try {
        const resp = await this.$axios.post("/spark/sweep-now")
        if (resp.data && resp.data.error) {
          this.sweepError = resp.data.error
          await this.loadSweepStatus()
          return
        }
        this.sweepMessage = "Sweep submitted. Spark will broadcast a cooperative on-chain exit."
        await this.loadSweepStatus()
      } catch (e) {
        this.sweepError =
          "Could not start a sweep. Confirm the Spark wallet, bridge, and on-chain address are set."
      } finally {
        this.sweeping = false
      }
    },
  },
}
</script>
