# Lightning Spark

This Bitcart plugin which is build with the official Spark SDK ([API reference](https://docs.spark.money/api-reference/overview)) adds **BTC Lightning via Spark** as a payment method. With this plugin you don't need to run a Lightning node. Customers pay a [BOLT11 Lightning invoice](https://docs.spark.money/wallets/deposit-from-lightning). Funds settle in your [Spark](https://spark.money) wallet.

## Features

* New wallet currency: **Lightning Spark**
* Checkout QR is a short Lightning invoice (no channels or Electrum Lightning node)
* Spark-in-BOLT11 is off: that extra payload made Bitcart’s QR too dense to scan. Spark wallets can still pay the Lightning invoice.
* Background worker marks the Bitcart invoice complete only when Spark reports `TRANSFER_COMPLETED`
* Superuser page to generate a mnemonic and set network (`MAINNET` / `REGTEST`)
* Automatic **cooperative sweep** to on-chain Bitcoin after a satoshi threshold (same idea as [Flint](https://github.com/sethforprivacy/flint))

## Install on Docker / Configurator

Package the plugin, then upload it on your Bitcart instance.

```bash
bitcart-cli plugin validate .
bitcart-cli plugin package .
```

That creates the plugin called `Lightning Spark.bitcart` (or similar). In the admin panel go to **Plugins**, upload the plugin and then click **Reload Plugins**.

The first reload **builds a `spark` container** from the plugin Dockerfile. That can take several minutes. If leftover compose container-name errors appear, remove those containers first, then run `./start.sh`.

After reload:

1. Confirm `spark` is running (`docker compose ps`).
2. Open **Server management → Lightning Spark**.
3. Set **Spark network** to `MAINNET` (or `REGTEST` for testing).
4. Optionally click **Generate mnemonic**. Copy it somewhere safe.
5. Open **Wallets → New wallet**, choose **Lightning Spark**, paste the mnemonic, save.
6. Attach that wallet to your store.
7. On **Lightning Spark**, turn on **Sweep Spark to on-chain Bitcoin**, set a threshold, and paste a `bc1` address (or leave it empty to use a BTC wallet owned by the same user as the Spark wallet).

Checkout then includes Lightning Spark as a payment method. The QR is the Lightning invoice; Bitcart completes the invoice when Spark confirms the receive. When the Spark balance crosses the threshold, the worker asks Spark for a [cooperative L1 withdrawal](https://docs.spark.money/wallets/withdraw-to-l1).

## Local development

The Bitcart API image does not include Node. Run the bridge yourself, then point the API at it:

```bash
export SPARK_BRIDGE_SECRET=$(openssl rand -hex 32)
export SPARK_BRIDGE_URL=http://127.0.0.1:5055
cd src/docker/spark
npm install --legacy-peer-deps
node server.mjs
```

In another shell, with the same `SPARK_BRIDGE_SECRET`:

```bash
export SPARK_BRIDGE_SECRET=...   # same value as the bridge
export SPARK_BRIDGE_URL=http://127.0.0.1:5055
bitcart-cli plugin install . --dev --save
```

Docker installs generate a shared secret in a volume (`/auth/secret`) unless you set `SPARK_BRIDGE_SECRET` yourself. The bridge refuses to start without a 16+ character secret.

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| Spark network | MAINNET | `MAINNET` or `REGTEST`. Must match the mnemonic’s network. |
| Accept Spark wallets | off | Leave off. Embedding a Spark invoice makes the checkout QR unscannable. |
| Account number | empty | Spark account index. Empty uses the SDK default (1 on mainnet, 0 on regtest). Always set this if you reuse one mnemonic across networks. |
| Sweep Spark to on-chain Bitcoin | off | Cooperative exit to Bitcoin when the balance crosses the threshold. |
| Sweep threshold (sats) | 200000 | Automatic sweep starts at this spendable balance. Raise it on mainnet so fees stay a small percent. |
| Minimum sweep (sats) | 100000 | Never sweep below this, including **Sweep now**. |
| Leave behind (sats) | 0 | Amount to keep on Spark. |
| Max exit fee percent | 3 | Refuse the sweep if the quote is above this percent (hard cap 50%). |
| Exit speed | MEDIUM | `FAST`, `MEDIUM`, or `SLOW`. |
| On-chain Bitcoin address | empty | Sweep destination. Empty uses a fresh address from a BTC wallet owned by the same user as the Spark wallet. |

The mnemonic is stored as the Bitcart wallet key (same trust model as an xprv). Do not put it in plugin settings, logs, or backups you do not control.

## How sweeping works

The Sweep process follows [Flint’s sweep model](https://github.com/sethforprivacy/flint/blob/main/docs/sweeping.md): Spark is a 2-of-3 statechain, and the plugin performs **cooperative exits only**. Sweeping is what moves funds into a Bitcoin address you control.

1. The worker checks Spark balances about every two minutes.
2. If spendable sats (balance minus “leave behind”) are at or above the threshold and the minimum, it asks Spark for a withdrawal fee quote.
3. If the fee is above the max percent (or half the sweep), it **refuses** and records why instead of overpaying.
4. Otherwise it calls `withdraw` to the on-chain address. **Sweep now** skips the automatic switch and the threshold, but still applies the minimum and fee guards.

On mainnet, exit fees are much higher than the 200000-sat default (that default matches Flint’s regtest-oriented starting point). Raise the threshold into the millions of sats if you want the fee to stay around a few percent.

## How payment detection works

1. Bitcart creates an invoice for the Spark wallet.
2. The plugin asks Spark for a Lightning invoice (`createLightningInvoice`) without embedding a Spark invoice, then puts an uppercase BOLT11 in the checkout QR. Checkout does not show a Node Info tab.
3. The worker polls `getLightningReceiveRequest` about every two seconds.
4. When status is `TRANSFER_COMPLETED`, Bitcart marks the invoice complete. Earlier Spark statuses such as `LIGHTNING_PAYMENT_RECEIVED` are not treated as paid.

## Caveats

* This is **not** Bitcart’s built-in Electrum Lightning. You do not open channels on the Bitcart BTC daemon.
* Existing invoices keep the old checkout layout. Reload the plugin and create a **new** invoice after upgrading.
* Keep a backup of the Spark mnemonic. Losing it means losing the funds.
* Mainnet and regtest account defaults differ. Set **Account number** explicitly if you test on regtest and later use the same mnemonic on mainnet.
* The Spark bridge listens only on the Docker network (`expose`, not `ports`) and requires a shared secret on every `/rpc` call. Do not publish port 5055 to the internet.
* After `openWallet`, later RPC calls use a session id instead of the mnemonic. The mnemonic still lives in the Bitcart wallet record and in the Spark process.
* Sweeps are cooperative exits. There is no unilateral-exit path in this plugin. A balance sitting on Spark depends on Spark operators until it is swept.
# bitcart-ln-spark
