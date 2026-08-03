// READ-ONLY on-chain diagnostic (GmxSdk / Classic path). No wallet key, no
// transaction — connects to Arbitrum via public RPC + GMX oracle/subsquid and
// dumps the target order's exact fields and price scales, so the on-chain
// create+cancel logic is written against real data, not assumptions. Also
// proves the SDK can reach Arbitrum from this machine.
//
// Usage (from executor\):
//   $env:GMX_ACCOUNT = "0xd944...47e4"          # the position/order owner
//   $env:ORDER_KEY   = "0xe6c4...0c8e"          # the SL order to inspect
//   node onchain_read.cjs
//
// Optional overrides: GMX_RPC_URL, GMX_ORACLE_URL, GMX_SUBSQUID_URL, GMX_CHAIN_ID.

const { GmxSdk } = require("@gmx-io/sdk");

const CHAIN_ID = Number(process.env.GMX_CHAIN_ID || "42161");
const ACCOUNT = process.env.GMX_ACCOUNT;
const ORDER_KEY = (process.env.ORDER_KEY || "").toLowerCase();
const RPC = process.env.GMX_RPC_URL || "https://arb1.arbitrum.io/rpc";
const ORACLE = process.env.GMX_ORACLE_URL || "https://arbitrum-api.gmxinfra.io";
const SUBSQUID =
  process.env.GMX_SUBSQUID_URL ||
  "https://gmx.squids.live/gmx-synthetics-arbitrum:prod/api/graphql";

const j = (v) => JSON.stringify(v, (_k, x) => (typeof x === "bigint" ? x.toString() : x), 2);

function scales(raw, indexDecimals) {
  // Interpret a price integer under the two plausible GMX scales.
  if (raw === undefined || raw === null) return null;
  const b = BigInt(raw);
  const asContract = Number(b) / 10 ** (30 - indexDecimals); // on-chain price scale
  const as1e30 = Number(b) / 1e30; // SDK internal USD scale
  return { raw: b.toString(), as_1e30_usd: as1e30, as_contract_usd: asContract };
}

(async () => {
  if (!ACCOUNT) {
    console.error("Set GMX_ACCOUNT (and ideally ORDER_KEY).");
    process.exit(2);
  }
  const out = { config: { chainId: CHAIN_ID, rpc: RPC, oracle: ORACLE } };
  try {
    const sdk = new GmxSdk({
      chainId: CHAIN_ID,
      rpcUrl: RPC,
      oracleUrl: ORACLE,
      subsquidUrl: SUBSQUID,
      account: ACCOUNT,
    });

    const { marketsInfoData, tokensData } = await sdk.markets.getMarketsInfo();
    out.reachable = true;
    out.market_count = marketsInfoData ? Object.keys(marketsInfoData).length : 0;
    out.token_count = tokensData ? Object.keys(tokensData).length : 0;

    const { ordersInfoData } = await sdk.orders.getOrders({ marketsInfoData, tokensData });
    const orders = Object.values(ordersInfoData || {});
    out.total_orders = orders.length;
    out.order_summaries = orders.map((o) => ({
      key: o.key,
      orderType: o.orderType,
      isLong: o.isLong,
      market: o.marketAddress || o.marketInfo?.marketTokenAddress,
      sizeDeltaUsd: String(o.sizeDeltaUsd ?? ""),
      triggerPrice: String(o.triggerPrice ?? o.contractTriggerPrice ?? ""),
    }));

    const target =
      orders.find((o) => (o.key || "").toLowerCase() === ORDER_KEY) ||
      (orders.length === 1 ? orders[0] : null);

    if (target) {
      const idxDec =
        target.indexToken?.decimals ??
        target.marketInfo?.indexToken?.decimals ??
        18;
      out.target = {
        note: "This is the SL order we will clone (new trigger, cancel old).",
        indexToken_symbol: target.indexToken?.symbol ?? target.marketInfo?.indexToken?.symbol,
        indexToken_decimals: idxDec,
        collateralToken_symbol:
          target.initialCollateralToken?.symbol ?? target.targetCollateralToken?.symbol,
        collateralToken_decimals:
          target.initialCollateralToken?.decimals ?? target.targetCollateralToken?.decimals,
        orderType: target.orderType,
        isLong: target.isLong,
        marketAddress: target.marketAddress || target.marketInfo?.marketTokenAddress,
        initialCollateralTokenAddress:
          target.initialCollateralTokenAddress || target.initialCollateralToken?.address,
        // Price fields interpreted under both scales so we can see which is real:
        triggerPrice_scaled: scales(target.triggerPrice, idxDec),
        acceptablePrice_scaled: scales(target.acceptablePrice, idxDec),
        // Raw amounts we would clone:
        sizeDeltaUsd_raw: String(target.sizeDeltaUsd ?? ""),
        sizeDeltaUsd_as_usd: target.sizeDeltaUsd ? Number(BigInt(target.sizeDeltaUsd)) / 1e30 : null,
        sizeDeltaInTokens_raw: String(target.sizeDeltaInTokens ?? "(absent)"),
        initialCollateralDeltaAmount_raw: String(target.initialCollateralDeltaAmount ?? ""),
        decreasePositionSwapType: target.decreasePositionSwapType ?? target.decreaseSwapType,
        swapPath: target.swapPath ?? [],
        autoCancel: target.autoCancel,
      };
      out.target_all_keys = Object.keys(target);
    } else {
      out.target = ORDER_KEY
        ? `ORDER_KEY ${ORDER_KEY} not found among ${orders.length} orders`
        : "no ORDER_KEY set and more than one order present";
    }

    // Position snapshot (best-effort; shape may vary by SDK version).
    try {
      const pos = await sdk.positions.getPositions({
        marketsInfoData,
        tokensData,
        start: 0,
        end: 1000,
      });
      const list = Object.values(pos?.positionsData || pos || {});
      out.positions = list.map((p) => ({
        market: p.marketAddress,
        isLong: p.isLong,
        sizeInUsd: String(p.sizeInUsd ?? ""),
        collateralAmount: String(p.collateralAmount ?? ""),
      }));
    } catch (e) {
      out.positions = { note: "position fetch skipped", error: String(e.message).slice(0, 120) };
    }
  } catch (e) {
    out.reachable = false;
    out.error = String((e && e.stack) || e).slice(0, 600);
  }
  console.log(j(out));
  process.exit(0);
})();
