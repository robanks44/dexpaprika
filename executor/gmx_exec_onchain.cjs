// dexpaprika S9 executor sidecar — ON-CHAIN path (GmxSdk / "Classic" mode).
// GMX exposes express/gasless subaccount orders to its FRONTEND ONLY (confirmed
// in GMX's own agent docs), so programmatic order changes go on-chain: the
// account's wallet signs each transaction and pays ETH gas + keeper fee.
//
// A DUMB tool by design: one JSON payload on stdin -> one JSON result on stdout.
// Every safeguard (dry-run default, arm, approval, kill-switch, limits, audit)
// lives in the Python caller. The wallet key is read from GMX_WALLET_KEY and
// ONLY submit mode receives it.
//
// Payload: { mode: "read"|"prepare"|"submit", action, params, idempotency_key? }
// Moving a stop-loss = create a new StopLossDecrease at the new trigger, then
// cancel the old order (new-before-old, so the position is never unprotected).

const { GmxSdk } = require("@gmx-io/sdk");
const { createWalletClient, http } = require("viem");
const { privateKeyToAccount } = require("viem/accounts");
const { arbitrum, avalanche } = require("viem/chains");

const CHAIN_ID = Number(process.env.GMX_CHAIN_ID || "42161");
const ACCOUNT = process.env.GMX_ACCOUNT;
const RPC = process.env.GMX_RPC_URL || "https://arb1.arbitrum.io/rpc";
const ORACLE = process.env.GMX_ORACLE_URL || "https://arbitrum-api.gmxinfra.io";
const SUBSQUID =
  process.env.GMX_SUBSQUID_URL ||
  "https://gmx.squids.live/gmx-synthetics-arbitrum:prod/api/graphql";

const E30 = 10n ** 30n;
const VIEM_CHAINS = { 42161: arbitrum, 43114: avalanche };

function usdToTrigger1e30(priceStr) {
  const [whole, frac = ""] = String(priceStr).split(".");
  if (frac.length > 2) throw new Error(`trigger price ${priceStr}: max 2 decimals`);
  const cents = BigInt(whole) * 100n + BigInt((frac + "00").slice(0, 2));
  return (cents * E30) / 100n;
}

function makeSdk({ withWallet }) {
  const cfg = { chainId: CHAIN_ID, rpcUrl: RPC, oracleUrl: ORACLE, subsquidUrl: SUBSQUID, account: ACCOUNT };
  if (withWallet) {
    const key = process.env.GMX_WALLET_KEY;
    if (!key) throw new Error("GMX_WALLET_KEY not provided");
    const acct = privateKeyToAccount(key.startsWith("0x") ? key : `0x${key}`);
    if (ACCOUNT && acct.address.toLowerCase() !== ACCOUNT.toLowerCase()) {
      throw new Error(`wallet key address ${acct.address} != GMX_ACCOUNT ${ACCOUNT}`);
    }
    cfg.account = acct.address;
    cfg.walletClient = createWalletClient({
      account: acct,
      chain: VIEM_CHAINS[CHAIN_ID],
      transport: http(RPC),
    });
  }
  return new GmxSdk(cfg);
}

async function loadOrders(sdk) {
  const { marketsInfoData, tokensData } = await sdk.markets.getMarketsInfo();
  const { ordersInfoData } = await sdk.orders.getOrders({ marketsInfoData, tokensData });
  return { marketsInfoData, tokensData, orders: Object.values(ordersInfoData || {}) };
}

function findOrder(orders, key) {
  const k = String(key || "").toLowerCase();
  return orders.find((o) => String(o.key || "").toLowerCase() === k) || null;
}

function summarize(o) {
  return {
    key: o.key,
    orderType: o.orderType,
    isLong: o.isLong,
    market: o.marketAddress,
    sizeDeltaUsd: String(o.sizeDeltaUsd ?? ""),
    trigger_usd: o.triggerPrice ? Number(BigInt(o.triggerPrice) * 1_000_000n / E30) / 1_000_000 : null,
  };
}

// Build the decreaseAmounts + args for createDecreaseOrder by CLONING the target
// SL order and changing only the trigger. sizeDeltaUsd/acceptablePrice are copied
// verbatim (already in SDK scale — verified against the live order), so a
// full-close "any price" stop stays exactly that.
function buildDecreasePlan(target, newPriceStr, marketsInfoData, tokensData) {
  const marketInfo = marketsInfoData[target.marketAddress];
  const collateralToken = tokensData[target.initialCollateralTokenAddress];
  if (!marketInfo) throw new Error(`market ${target.marketAddress} not in marketsInfoData`);
  if (!collateralToken) throw new Error(`collateral ${target.initialCollateralTokenAddress} not in tokensData`);
  const decreaseAmounts = {
    triggerOrderType: target.orderType, // 6 = StopLossDecrease
    triggerThresholdType: target.triggerThresholdType,
    collateralDeltaAmount: BigInt(target.initialCollateralDeltaAmount ?? 0n),
    triggerPrice: usdToTrigger1e30(newPriceStr), // new trigger, SDK 1e30 scale
    acceptablePrice: BigInt(target.acceptablePrice), // cloned verbatim
    sizeDeltaUsd: BigInt(target.sizeDeltaUsd), // cloned verbatim
    sizeDeltaInTokens: 0n, // not part of the on-chain create params
    decreaseSwapType: target.decreasePositionSwapType ?? 0,
  };
  return { marketInfo, collateralToken, decreaseAmounts };
}

function planPreview(target, newPriceStr, decreaseAmounts) {
  return {
    action: "move-stop-loss",
    old_order_key: target.key,
    old_trigger_usd: Number(BigInt(target.triggerPrice) * 1_000_000n / E30) / 1_000_000,
    new_trigger_usd: Number(newPriceStr),
    new_trigger_1e30: decreaseAmounts.triggerPrice.toString(),
    market: target.marketAddress,
    index_token: target.indexToken?.symbol,
    collateral_token: target.initialCollateralToken?.symbol,
    is_long: target.isLong,
    order_type: target.orderType,
    size_delta_usd_raw: decreaseAmounts.sizeDeltaUsd.toString(),
    size_is_full_close: decreaseAmounts.sizeDeltaUsd === 2n ** 256n - 1n,
    collateral_delta_amount_raw: decreaseAmounts.collateralDeltaAmount.toString(),
    sequence: "create new SL at new trigger, then cancel old order (new-before-old)",
  };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// After a write, wait until the account nonce advances so the next tx doesn't
// collide (Arbitrum: usually a couple of seconds). Returns true if it advanced.
async function waitNonceAdvance(sdk, addr, nonceBefore, tries = 30) {
  for (let i = 0; i < tries; i++) {
    const n = await sdk.publicClient.getTransactionCount({ address: addr });
    if (n > nonceBefore) return true;
    await sleep(2000);
  }
  return false;
}

async function handle(payload) {
  const action = payload.action;
  if (!["set-sl-trigger", "cancel-order"].includes(action) && payload.mode !== "read") {
    return { ok: false, error: `on-chain sidecar: action ${action} not enabled` };
  }

  if (payload.mode === "read") {
    const sdk = makeSdk({ withWallet: false });
    const { orders } = await loadOrders(sdk);
    return { ok: true, orders: orders.map(summarize) };
  }

  if (payload.mode === "prepare") {
    const sdk = makeSdk({ withWallet: false });
    const { marketsInfoData, tokensData, orders } = await loadOrders(sdk);
    const target = findOrder(orders, payload.params.order_key);
    if (!target) return { ok: false, error: `order ${payload.params.order_key} not found` };
    if (action === "cancel-order") {
      return {
        ok: true,
        plan: { action: "cancel-order", order_key: target.key, trigger_usd: summarize(target).trigger_usd },
      };
    }
    const { decreaseAmounts } = buildDecreasePlan(target, payload.params.trigger_price, marketsInfoData, tokensData);
    return { ok: true, plan: planPreview(target, payload.params.trigger_price, decreaseAmounts) };
  }

  if (payload.mode === "submit") {
    const sdk = makeSdk({ withWallet: true });
    const addr = sdk.account;
    const { marketsInfoData, tokensData, orders } = await loadOrders(sdk);
    const target = findOrder(orders, payload.params.order_key);
    if (!target) return { ok: false, error: `order ${payload.params.order_key} not found` };

    if (action === "cancel-order") {
      await sdk.orders.cancelOrders([target.key]);
      return { ok: true, cancelled: target.key, note: "cancel order submitted on-chain" };
    }

    const { marketInfo, collateralToken, decreaseAmounts } = buildDecreasePlan(
      target, payload.params.trigger_price, marketsInfoData, tokensData
    );

    // 1) Create the NEW stop-loss at the new trigger (position now has old+new).
    const nonceBefore = await sdk.publicClient.getTransactionCount({ address: addr });
    await sdk.orders.createDecreaseOrder({
      marketsInfoData,
      tokensData,
      marketInfo,
      collateralToken,
      decreaseAmounts,
      allowedSlippage: 0,
      isLong: target.isLong,
      referralCode: undefined,
      isTrigger: true,
    });

    // 2) Wait for the create to mine (nonce advances) BEFORE cancelling the old
    // order, so the two writes never share a nonce.
    const advanced = await waitNonceAdvance(sdk, addr, nonceBefore);
    let cancelled = false;
    let cancelError = null;
    if (!advanced) {
      cancelError = "create tx did not confirm within ~60s — old order left in place";
    } else {
      try {
        await sdk.orders.cancelOrders([target.key]);
        cancelled = true;
      } catch (e) {
        cancelError = String((e && e.message) || e).slice(0, 300);
      }
    }

    return {
      ok: true,
      created: "sent",
      old_order_cancelled: cancelled,
      cancel_error: cancelError,
      new_trigger_usd: Number(payload.params.trigger_price),
      note: cancelled
        ? "moved: new SL created, old SL cancelled"
        : "NEW SL created but OLD not cancelled — position has TWO stop-losses; run `execute cancel-order --key <old> --arm` or cancel in the GMX app.",
    };
  }

  return { ok: false, error: `unknown mode ${payload.mode}` };
}

(async () => {
  let input = "";
  for await (const chunk of process.stdin) input += chunk;
  let payload;
  try {
    payload = JSON.parse(input);
  } catch (e) {
    process.stdout.write(JSON.stringify({ ok: false, error: `bad payload: ${e.message}` }));
    process.exit(0);
  }
  try {
    const result = await handle(payload);
    process.stdout.write(JSON.stringify(result, (_k, v) => (typeof v === "bigint" ? v.toString() : v)));
  } catch (e) {
    process.stdout.write(JSON.stringify({ ok: false, error: String((e && e.message) || e) }));
  }
  process.exit(0);
})();
