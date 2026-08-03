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

// Current GMX relay-router addresses (verified against @gmx-io/sdk 2.0.0-alpha-1).
// GMX rotated these after 1.6.4 was published, so 1.6.4's bundled validator
// rejects the live SubaccountGelatoRelayRouter domain that GMX's prepare API now
// returns. We sign the domain GMX gives us, but keep the SDK's safety checks with
// an up-to-date allowlist (both current + 1.6.4 addresses, for tolerance).
const RELAY_ROUTERS = {
  42161: [ // Arbitrum One
    "0x517602BaC704B72993997820981603f5E4901273", // SubaccountGelatoRelayRouter (current)
    "0xa9090E2fd6cD8Ee397cF3106189A7E1CFAE6C59C", // GelatoRelayRouter (current)
    "0xfD0596f708d9D950E0eF7b5d191e5F8e55b8a67f", // SubaccountGelatoRelayRouter (1.6.4)
    "0x5503b99308dB6923758F9A22d118207D633c4e87", // GelatoRelayRouter (1.6.4)
  ],
  421614: [ // Arbitrum Sepolia (current)
    "0x43947140EEE26b82155baA18FDB746A05C700DCE",
    "0xD2f52a70224d3453ea17944ABC12772793987FA6",
  ],
  43114: [ // Avalanche (current)
    "0xfaBEb65bB877600be3A2C2a03aA56a95F9f845B9",
    "0xEE2d3339CbcE7A42573C96ACc1298A79a5C996Df",
  ],
};

// Sign the express typed-data GMX returned, replicating the SDK's
// validateOrderTypedData (domain name/version/chainId + receiver anti-spoof) but
// with a current relay-router allowlist instead of 1.6.4's stale hardcoded set.
async function signWithCurrentAllowlist(prepared, signer, chainId, accountAddress) {
  const { getAddress } = require("viem");
  if (prepared.payloadType !== "typed-data") {
    throw new Error(`cannot sign payloadType "${prepared.payloadType}"`);
  }
  const td = prepared.payload && prepared.payload.typedData;
  if (!td) throw new Error("missing typedData in prepare response");
  const { domain, types, message } = td;
  if (domain.name !== "GmxBaseGelatoRelayRouter")
    throw new Error(`unexpected EIP-712 domain name "${domain.name}"`);
  if (String(domain.version) !== "1")
    throw new Error(`unexpected EIP-712 domain version "${domain.version}"`);
  if (Number(domain.chainId) !== chainId)
    throw new Error(`EIP-712 chainId ${domain.chainId} != ${chainId}`);
  const allow = new Set((RELAY_ROUTERS[chainId] || []).map((a) => getAddress(a)));
  const vc = domain.verifyingContract ? getAddress(domain.verifyingContract) : undefined;
  if (!vc || !allow.has(vc))
    throw new Error(
      `verifyingContract "${domain.verifyingContract}" not an allowed relay router for chain ${chainId}`
    );
  // Receiver anti-spoof: any order receiver must be the subaccount signer or the
  // main account (mirrors the SDK). Best-effort across create/update param lists.
  const allowed = new Set([getAddress(signer.address)]);
  if (accountAddress) allowed.add(getAddress(accountAddress));
  const ZERO = "0x0000000000000000000000000000000000000000";
  const lists = [message.createOrderParamsList, message.updateOrderParamsList].filter(Array.isArray);
  for (const list of lists) {
    for (const o of list) {
      for (const field of ["receiver", "cancellationReceiver"]) {
        const r = o && o.addresses && o.addresses[field];
        if (r && getAddress(r) !== ZERO && !allowed.has(getAddress(r)))
          throw new Error(`order ${field} "${r}" not the signer or account — refusing to sign`);
      }
    }
  }
  return signer.signTypedData(domain, types, message);
}

async function submit(sdk, action, params, idempotencyKey) {
  const keyHex = process.env.GMX_SUBACCOUNT_KEY;
  if (!keyHex) return { ok: false, error: "GMX_SUBACCOUNT_KEY not provided" };
  // Subaccount-only signing path (no main key): the prepare request carries just
  // subaccountAddress (NO approval — see below); the SUBACCOUNT key signs, and the
  // signature is validated against the MAIN account address. The real
  // authorization is the already-active on-chain subaccount, which GMX's relay
  // reads directly.
  const sub = new PrivateKeySigner(keyHex.startsWith("0x") ? keyHex : `0x${keyHex}`);
  // Active on-chain subaccount: send NO subaccountApproval. The SDK omits it when
  // the subaccount is already usable on-chain (sdkClient.js: approval=undefined ->
  // not included in prepare or submit); GMX's relay reads the on-chain
  // authorization. getEmptySubaccountApproval is a gas-estimation stub only —
  // submitting it (shouldAdd:true, zero count/expiry, sig "0x") makes the router
  // try to RE-REGISTER the subaccount and reverts on simulation.

  let preparedResult;
  if (action === "set-sl-trigger") {
    preparedResult = await sdk.prepareEditOrder({
      orderIds: [params.order_key],
      newTriggerPrice: usdToTrigger1e30(params.trigger_price),
      mode: "express",
      from: ACCOUNT,
      subaccountAddress: sub.address,
    });
  } else if (action === "cancel-order") {
    preparedResult = await sdk.prepareCancelOrder({
      orderIds: [params.order_key],
      mode: "express",
      from: ACCOUNT,
      subaccountAddress: sub.address,
    });
  } else {
    return { ok: false, error: `submit not enabled for ${action}` };
  }

  // SUBACCOUNT key signs; accountAddress = MAIN (SDK's signOrderWithSubaccount).
  // Uses our current-allowlist signer because 1.6.4's validator rejects GMX's
  // rotated relay router (see signWithCurrentAllowlist / RELAY_ROUTERS above).
  const signature = await signWithCurrentAllowlist(preparedResult, sub, CHAIN_ID, ACCOUNT);
  // Build the submit request exactly like the SDK's own subaccount path. Do NOT
  // inject our internal audit idempotency_key — GMX's live submit schema no longer
  // accepts an idempotencyKey field, so only forward one if GMX's prepare response
  // actually returned it (mirrors executeExpressOrderWithSubaccount; else omitted).
  const submitReq = {
    mode: preparedResult.mode,
    requestId: preparedResult.requestId,
    signature,
    from: ACCOUNT,
    eip712Data: {
      batchParams: preparedResult.payload.batchParams,
      relayParams: preparedResult.payload.relayParams,
    },
  };
  if (preparedResult.idempotencyKey) submitReq.idempotencyKey = preparedResult.idempotencyKey;
  const submitted = await sdk.submitOrder(submitReq);

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
