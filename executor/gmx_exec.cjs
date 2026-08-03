// dexpaprika S9 executor sidecar — a DUMB tool by design (spec: docs/specs/S9-execution.md).
// One JSON payload on stdin -> one JSON result on stdout. No policy, no retries,
// no schedule; every safeguard lives in the Python caller. The subaccount key is
// read from GMX_SUBACCOUNT_KEY and ONLY submit mode receives it from the caller.
//
// Payload: { mode: "read"|"prepare"|"submit", action, params, idempotency_key? }
// Official SDK, CJS entry (the ESM build has extensionless imports — probe-verified).

const { GmxApiSdk, PrivateKeySigner } = require("@gmx-io/sdk/v2");

// Execution target (S9.5) — supplied by the Python runner. Defaults keep the
// standalone read-only probe working on Arbitrum One mainnet.
const ACCOUNT = process.env.GMX_ACCOUNT || "0xc155a616e39d7b83e37e8fd9d2106e1bc056d7fe";
const CHAIN_ID = Number(process.env.GMX_CHAIN_ID || "42161");
const E12 = 10n ** 12n; // ETH trigger scaling (VERIFIED_FINDINGS §2.1)
const E30 = 10n ** 30n;

function usdToTrigger1e30(priceStr) {
  // Decimal string dollars -> 1e30 fixed point (integer cents precision is enough
  // for triggers; refuse deeper fractions rather than round silently).
  const [whole, frac = ""] = String(priceStr).split(".");
  if (frac.length > 2) throw new Error(`trigger price ${priceStr}: max 2 decimals`);
  const cents = BigInt(whole) * 100n + BigInt((frac + "00").slice(0, 2));
  return (cents * E30) / 100n;
}

async function readOrders(sdk) {
  const orders = await sdk.fetchOrders({ address: ACCOUNT });
  return {
    ok: true,
    orders: orders.map((o) => ({
      key: o.key,
      orderType: o.orderType,
      isLong: o.isLong,
      triggerPrice: String(o.triggerPrice ?? ""),
      sizeDeltaUsd: String(o.sizeDeltaUsd ?? ""),
    })),
  };
}

async function prepare(sdk, action, params) {
  if (action === "set-sl-trigger") {
    const prepared = await sdk.prepareEditOrder({
      orderIds: [params.order_key],
      newTriggerPrice: usdToTrigger1e30(params.trigger_price),
      mode: "express",
      from: ACCOUNT,
    });
    return {
      ok: true,
      plan: {
        kind: "edit-order",
        order_key: params.order_key,
        new_trigger_usd: String(params.trigger_price),
        new_trigger_1e30: String(usdToTrigger1e30(params.trigger_price)),
        request_id: prepared.requestId ?? null,
        sdk_idempotency_key: prepared.idempotencyKey ?? null,
      },
      prepared: { requestId: prepared.requestId, idempotencyKey: prepared.idempotencyKey },
    };
  }
  if (action === "cancel-order") {
    const prepared = await sdk.prepareCancelOrder({
      orderIds: [params.order_key],
      mode: "express",
      from: ACCOUNT,
    });
    return {
      ok: true,
      plan: { kind: "cancel-order", order_key: params.order_key,
              request_id: prepared.requestId ?? null },
      prepared: { requestId: prepared.requestId, idempotencyKey: prepared.idempotencyKey },
    };
  }
  if (action === "resize-short") {
    // Prepared fully at the supervised session (market decrease/increase with
    // collateral handling); refuse rather than half-implement a funds path.
    return { ok: false, error: "resize-short prepare: enabled at the supervised session" };
  }
  return { ok: false, error: `unknown action ${action}` };
}

async function submit(sdk, action, params, idempotencyKey) {
  const keyHex = process.env.GMX_SUBACCOUNT_KEY;
  if (!keyHex) return { ok: false, error: "GMX_SUBACCOUNT_KEY not provided" };
  const signer = new PrivateKeySigner(keyHex.startsWith("0x") ? keyHex : `0x${keyHex}`);

  let preparedResult;
  if (action === "set-sl-trigger") {
    preparedResult = await sdk.prepareEditOrder({
      orderIds: [params.order_key],
      newTriggerPrice: usdToTrigger1e30(params.trigger_price),
      mode: "express",
      from: ACCOUNT,
    });
  } else if (action === "cancel-order") {
    preparedResult = await sdk.prepareCancelOrder({
      orderIds: [params.order_key],
      mode: "express",
      from: ACCOUNT,
    });
  } else {
    return { ok: false, error: `submit not enabled for ${action}` };
  }

  const signature = await sdk.signOrder(preparedResult, signer);
  const submitted = await sdk.submitOrder({
    mode: preparedResult.mode,
    requestId: preparedResult.requestId,
    signature,
    from: ACCOUNT,
    idempotencyKey: idempotencyKey ?? preparedResult.idempotencyKey,
    eip712Data: {
      batchParams: preparedResult.payload.batchParams,
      relayParams: preparedResult.payload.relayParams,
    },
  });

  // Poll relay status to a terminal state (bounded).
  let status = await sdk.fetchOrderStatus({ requestId: preparedResult.requestId });
  for (let i = 0; i < 30 && !["created", "executed", "cancelled", "relay_failed"].includes(status.status); i++) {
    await new Promise((r) => setTimeout(r, 2000));
    status = await sdk.fetchOrderStatus({ requestId: preparedResult.requestId });
  }
  const okStates = ["created", "executed", "cancelled"];
  return {
    ok: okStates.includes(status.status),
    request_id: preparedResult.requestId,
    relay_status: status.status,
    submitted: submitted ?? null,
  };
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
  const sdk = new GmxApiSdk({ chainId: CHAIN_ID });
  try {
    let result;
    if (payload.mode === "read") result = await readOrders(sdk);
    else if (payload.mode === "prepare") result = await prepare(sdk, payload.action, payload.params);
    else if (payload.mode === "submit")
      result = await submit(sdk, payload.action, payload.params, payload.idempotency_key);
    else result = { ok: false, error: `unknown mode ${payload.mode}` };
    process.stdout.write(JSON.stringify(result));
  } catch (e) {
    process.stdout.write(JSON.stringify({ ok: false, error: String(e && e.message || e) }));
  }
  process.exit(0);
})();
