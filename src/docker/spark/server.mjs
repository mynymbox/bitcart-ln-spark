import { createHash, randomBytes, timingSafeEqual } from "node:crypto"
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs"
import { createServer } from "node:http"
import { dirname } from "node:path"
import { SparkWallet } from "@buildonspark/spark-sdk"

const PORT = Number(process.env.SPARK_BRIDGE_PORT || 5055)
const HOST = process.env.SPARK_BRIDGE_HOST || "0.0.0.0"
const MAX_BODY = 64 * 1024
const MAX_SESSIONS = 32
const ALLOWED_NETWORKS = new Set(["MAINNET", "REGTEST"])
const PAID_STATUSES = new Set(["TRANSFER_COMPLETED"])
const EXIT_SPEEDS = new Set(["FAST", "MEDIUM", "SLOW"])

const sessions = new Map()

function loadSecret() {
  const fromEnv = String(process.env.SPARK_BRIDGE_SECRET || "").trim()
  if (fromEnv.length >= 16) return fromEnv
  const authFile = String(process.env.SPARK_BRIDGE_AUTH_FILE || "").trim()
  if (!authFile) return ""
  try {
    if (existsSync(authFile)) {
      const stored = readFileSync(authFile, "utf8").trim()
      if (stored.length >= 16) return stored
    }
    mkdirSync(dirname(authFile), { recursive: true })
    const generated = randomBytes(32).toString("hex")
    writeFileSync(authFile, generated, { encoding: "utf8", mode: 0o600 })
    return generated
  } catch {
    return ""
  }
}

const BRIDGE_SECRET = loadSecret()

function json(res, status, body) {
  const payload = JSON.stringify(body, (_key, value) =>
    typeof value === "bigint" ? value.toString() : value
  )
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(payload),
  })
  res.end(payload)
}

function publicError(error) {
  const message = error && error.message ? String(error.message) : "Spark request failed"
  return message.replace(/[a-z]+(?: [a-z]+){11,23}/gi, "[mnemonic]")
}

function secretsEqual(got, expected) {
  const a = Buffer.from(String(got || ""), "utf8")
  const b = Buffer.from(String(expected || ""), "utf8")
  if (a.length !== b.length) {
    timingSafeEqual(b, b)
    return false
  }
  return timingSafeEqual(a, b)
}

function authorized(req) {
  if (!BRIDGE_SECRET || BRIDGE_SECRET.length < 16) return false
  const header = req.headers["x-spark-bridge-secret"]
  const bearer = String(req.headers.authorization || "")
  const token = header
    ? String(header)
    : bearer.toLowerCase().startsWith("bearer ")
      ? bearer.slice(7).trim()
      : ""
  return secretsEqual(token, BRIDGE_SECRET)
}

function normalizeNetwork(value) {
  const network = String(value || "MAINNET").trim().toUpperCase()
  return ALLOWED_NETWORKS.has(network) ? network : "MAINNET"
}

function normalizeMnemonic(value) {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/\s+/g, " ")
}

function parseAccountNumber(value) {
  if (value === undefined || value === null || value === "") return undefined
  const number = Number(value)
  if (!Number.isInteger(number) || number < 0) return undefined
  return number
}

function walletCacheKey(mnemonic, network, accountNumber) {
  return createHash("sha256")
    .update(`${mnemonic}|${network}|${accountNumber ?? ""}`)
    .digest("hex")
}

function evictOldestSession() {
  if (sessions.size < MAX_SESSIONS) return
  const oldest = sessions.keys().next().value
  if (oldest) sessions.delete(oldest)
}

async function createWallet(params) {
  const mnemonic = normalizeMnemonic(params.mnemonic)
  if (!mnemonic) throw new Error("Mnemonic is required")
  const network = normalizeNetwork(params.network)
  const accountNumber = parseAccountNumber(params.accountNumber)
  const init = {
    mnemonicOrSeed: mnemonic,
    options: { network },
  }
  if (accountNumber !== undefined) init.accountNumber = accountNumber
  const { wallet } = await SparkWallet.initialize(init)
  return { wallet, network, cacheKey: walletCacheKey(mnemonic, network, accountNumber) }
}

function getSession(params) {
  const sessionId = String(params.sessionId || "")
  if (!sessionId) throw new Error("sessionId is required")
  const session = sessions.get(sessionId)
  if (!session) throw new Error("Unknown or expired session")
  sessions.delete(sessionId)
  sessions.set(sessionId, session)
  return session
}

async function readBody(req) {
  const chunks = []
  let size = 0
  for await (const chunk of req) {
    size += chunk.length
    if (size > MAX_BODY) {
      throw new Error("Request too large")
    }
    chunks.push(chunk)
  }
  if (!chunks.length) return {}
  return JSON.parse(Buffer.concat(chunks).toString("utf8"))
}

function encodedInvoice(request) {
  return (
    request?.invoice?.encodedInvoice ||
    request?.invoice?.encoded_invoice ||
    request?.encodedInvoice ||
    ""
  )
}

function currencySats(amount) {
  if (amount == null) return 0
  const value = amount.originalValue ?? amount.original_value ?? amount
  const number = Number(value)
  return Number.isFinite(number) ? number : 0
}

function feeForSpeed(quote, userKey, l1Key) {
  const userFeeSats = currencySats(quote?.[userKey])
  const l1FeeSats = currencySats(quote?.[l1Key])
  return { userFeeSats, l1FeeSats, totalSats: userFeeSats + l1FeeSats }
}

function serializeQuote(quote) {
  if (!quote) return null
  return {
    id: quote.id,
    expiresAt: quote.expiresAt || quote.expires_at || null,
    fast: feeForSpeed(quote, "userFeeFast", "l1BroadcastFeeFast"),
    medium: feeForSpeed(quote, "userFeeMedium", "l1BroadcastFeeMedium"),
    slow: feeForSpeed(quote, "userFeeSlow", "l1BroadcastFeeSlow"),
  }
}

function serializeExit(request) {
  if (!request) return null
  return {
    id: request.id || null,
    status: request.status || null,
    txid: request.coopExitTxid || request.coop_exit_txid || request.txid || null,
  }
}

function normalizeExitSpeed(value) {
  const speed = String(value || "MEDIUM").trim().toUpperCase()
  return EXIT_SPEEDS.has(speed) ? speed : "MEDIUM"
}

function requireBtcAddress(value, network) {
  const address = String(value || "").trim()
  const lower = address.toLowerCase()
  const bech32 = /^(bc1|tb1|bcrt1)[02-9ac-hj-np-z]{8,87}$/i.test(address)
  const base58 = /^[13][a-km-zA-HJ-NP-Z1-9]{25,34}$/.test(address)
  if (network === "REGTEST") {
    if (!lower.startsWith("bcrt1") || !bech32) {
      throw new Error("Withdrawal address must be a regtest Bitcoin address")
    }
    return address
  }
  if (lower.startsWith("tb1") || lower.startsWith("bcrt1")) {
    throw new Error("Withdrawal address is not valid for mainnet")
  }
  if (!bech32 && !base58) {
    throw new Error("Withdrawal address is not a Bitcoin address")
  }
  return address
}

async function dispatch(method, params) {
  switch (method) {
    case "openWallet": {
      const mnemonic = normalizeMnemonic(params.mnemonic)
      if (!mnemonic) throw new Error("Mnemonic is required")
      const network = normalizeNetwork(params.network)
      const accountNumber = parseAccountNumber(params.accountNumber)
      const cacheKey = walletCacheKey(mnemonic, network, accountNumber)
      for (const session of sessions.values()) {
        if (session.cacheKey === cacheKey) {
          evictOldestSession()
          const sessionId = randomBytes(32).toString("hex")
          sessions.set(sessionId, session)
          return { sessionId, network: session.network }
        }
      }
      const opened = await createWallet(params)
      evictOldestSession()
      const sessionId = randomBytes(32).toString("hex")
      sessions.set(sessionId, {
        wallet: opened.wallet,
        network: opened.network,
        cacheKey: opened.cacheKey,
      })
      return { sessionId, network: opened.network }
    }
    case "validate": {
      getSession(params)
      return { ok: true }
    }
    case "address": {
      const session = getSession(params)
      return { address: await session.wallet.getSparkAddress() }
    }
    case "balance": {
      const session = getSession(params)
      const balance = await session.wallet.getBalance()
      const sats =
        balance?.satsBalance?.available ??
        balance?.balance ??
        0n
      return { sats: String(sats) }
    }
    case "createLightningInvoice": {
      const session = getSession(params)
      const amountSats = Number(params.amountSats)
      if (!Number.isInteger(amountSats) || amountSats < 1 || amountSats >= 2 ** 53) {
        throw new Error("amountSats must be a positive integer")
      }
      const expirySeconds = Number(params.expirySeconds || 3600)
      const request = await session.wallet.createLightningInvoice({
        amountSats,
        memo: String(params.memo || "").slice(0, 120),
        expirySeconds: Number.isFinite(expirySeconds)
          ? Math.max(60, Math.min(expirySeconds, 30 * 24 * 3600))
          : 3600,
        includeSparkInvoice: params.includeSparkInvoice === true,
      })
      return {
        id: request.id,
        invoice: encodedInvoice(request),
        paymentHash: request.invoice?.paymentHash || request.invoice?.payment_hash || null,
        sparkAddress: await session.wallet.getSparkAddress(),
        status: request.status,
      }
    }
    case "getLightningReceiveRequest": {
      const session = getSession(params)
      const id = String(params.id || "")
      if (!id) throw new Error("Invoice id is required")
      const request = await session.wallet.getLightningReceiveRequest(id)
      const status = request?.status || "INVOICE_CREATED"
      return {
        id: request?.id || id,
        status,
        paid: PAID_STATUSES.has(status),
      }
    }
    case "generateWallet": {
      const network = normalizeNetwork(params.network)
      const accountNumber = parseAccountNumber(params.accountNumber)
      const init = { options: { network } }
      if (accountNumber !== undefined) init.accountNumber = accountNumber
      const { wallet, mnemonic } = await SparkWallet.initialize(init)
      const address = await wallet.getSparkAddress()
      try {
        await wallet.cleanupConnections()
      } catch {
        // Wallet is returned to the caller; a failed cleanup is not fatal.
      }
      if (!mnemonic) throw new Error("Spark SDK did not return a mnemonic")
      return { mnemonic, address, network }
    }
    case "getWithdrawalFeeQuote": {
      const session = getSession(params)
      const amountSats = Number(params.amountSats)
      if (!Number.isInteger(amountSats) || amountSats < 1 || amountSats >= 2 ** 53) {
        throw new Error("amountSats must be a positive integer")
      }
      const withdrawalAddress = requireBtcAddress(params.withdrawalAddress, session.network)
      const quote = await session.wallet.getWithdrawalFeeQuote({
        amountSats,
        withdrawalAddress,
      })
      const serialized = serializeQuote(quote)
      if (!serialized) throw new Error("Spark did not return a withdrawal fee quote")
      return serialized
    }
    case "withdraw": {
      const session = getSession(params)
      const amountSats = Number(params.amountSats)
      if (!Number.isInteger(amountSats) || amountSats < 1 || amountSats >= 2 ** 53) {
        throw new Error("amountSats must be a positive integer")
      }
      const feeAmountSats = Number(params.feeAmountSats)
      if (!Number.isInteger(feeAmountSats) || feeAmountSats < 0) {
        throw new Error("feeAmountSats must be a non-negative integer")
      }
      const onchainAddress = requireBtcAddress(params.onchainAddress, session.network)
      const feeQuoteId = String(params.feeQuoteId || "")
      if (!feeQuoteId) throw new Error("feeQuoteId is required")
      let exitSpeed = normalizeExitSpeed(params.exitSpeed)
      try {
        const sdk = await import("@buildonspark/spark-sdk")
        if (sdk.ExitSpeed && sdk.ExitSpeed[exitSpeed] != null) {
          exitSpeed = sdk.ExitSpeed[exitSpeed]
        }
      } catch {
        // String speeds are accepted by current SDK builds.
      }
      const result = await session.wallet.withdraw({
        onchainAddress,
        exitSpeed,
        amountSats,
        feeQuoteId,
        feeAmountSats,
        deductFeeFromWithdrawalAmount: params.deductFeeFromWithdrawalAmount !== false,
      })
      const serialized = serializeExit(result)
      if (!serialized || !serialized.id) throw new Error("Spark withdrawal did not return an id")
      return serialized
    }
    case "getCoopExitRequest": {
      const session = getSession(params)
      const id = String(params.id || "")
      if (!id) throw new Error("Withdrawal id is required")
      const request = await session.wallet.getCoopExitRequest(id)
      const serialized = serializeExit(request)
      if (!serialized) return { id, status: "UNKNOWN", txid: null }
      return serialized
    }
    default:
      throw new Error("Unknown method")
  }
}

const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`)
    if (req.method === "GET" && url.pathname === "/health") {
      json(res, 200, { ok: true, auth: Boolean(BRIDGE_SECRET) })
      return
    }
    if (req.method !== "POST" || url.pathname !== "/rpc") {
      json(res, 404, { error: "Not found" })
      return
    }
    if (!authorized(req)) {
      json(res, 401, { error: "Unauthorized" })
      return
    }
    const body = await readBody(req)
    const method = String(body.method || "")
    const params = body.params && typeof body.params === "object" ? body.params : {}
    if (method !== "openWallet" && Object.prototype.hasOwnProperty.call(params, "mnemonic")) {
      throw new Error("mnemonic is only accepted by openWallet")
    }
    const result = await dispatch(method, params)
    json(res, 200, { result })
  } catch (error) {
    json(res, 400, { error: publicError(error) })
  }
})

if (!BRIDGE_SECRET) {
  console.error("Spark bridge refused to start: set SPARK_BRIDGE_SECRET (16+ chars) or SPARK_BRIDGE_AUTH_FILE")
  process.exit(1)
}

server.listen(PORT, HOST, () => {
  console.log(`Spark bridge listening on ${HOST}:${PORT}`)
})
