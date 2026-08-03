// READ-ONLY diagnostic. Runs GMX's express edit-order PREPARE against each API
// region and reports which relay-router address appears in the signed domain vs.
// the relayParams/batchParams. No signing, no submit, no state change — safe to
// run any number of times. It exists to confirm whether a region's prepare
// response is self-consistent (domain router == execution router) before we spend
// a live submit attempt.
//
// Env:
//   GMX_ACCOUNT      main/test wallet address (the order owner), e.g. 0xd944...47e4
//   GMX_SUBACCOUNT   subaccount address (public), e.g. 0xc552...E87c
//   GMX_CHAIN_ID     default 42161
//   ORDER_KEY        the order to edit, e.g. 0xe6c4...0c8e
//   TRIGGER_PRICE    dollars, default "1901"
// No private key is needed or read.

const { GmxApiSdk } = require("@gmx-io/sdk/v2");
const { getEmptySubaccountApproval } = require("@gmx-io/sdk/utils/subaccount");

// Apply the SAME current-router override the v5 executor uses, so this probe
// reflects what the executor will actually sign. Set PROBE_NO_OVERRIDE=1 to see
// the raw (stale) SDK behaviour instead.
if (!process.env.PROBE_NO_OVERRIDE) {
  try {
    const cfg = require("@gmx-io/sdk/configs/contracts");
    const cid = Number(process.env.GMX_CHAIN_ID || "42161");
    const chainC = cfg.CONTRACTS && cfg.CONTRACTS[cid];
    if (chainC && cid === 42161) {
      chainC.SubaccountGelatoRelayRouter = "0x517602BaC704B72993997820981603f5E4901273";
      chainC.GelatoRelayRouter = "0xa9090E2fd6cD8Ee397cF3106189A7E1CFAE6C59C";
    }
  } catch (_e) {
    /* ignore */
  }
}

const ACCOUNT = process.env.GMX_ACCOUNT;
const SUBACCOUNT = process.env.GMX_SUBACCOUNT;
const CHAIN_ID = Number(process.env.GMX_CHAIN_ID || "42161");
const ORDER_KEY = process.env.ORDER_KEY;
const TRIGGER = process.env.TRIGGER_PRICE || "1901";

const NEW_ROUTER = "0x517602bac704b72993997820981603f5e4901273"; // current (docs)
const OLD_ROUTER = "0xfd0596f708d9d950e0ef7b5d191e5f8e55b8a67f"; // 1.6.4 (deprecated)
const E30 = 10n ** 30n;

function usdToTrigger1e30(s) {
  const [w, f = ""] = String(s).split(".");
  const cents = BigInt(w) * 100n + BigInt((f + "00").slice(0, 2));
  return (cents * E30) / 100n;
}

// Regions to probe: SDK default (whatever 1.6.4 ships) + the .ai production peer.
const REGIONS = [
  { label: "default (sdk)", apiUrl: undefined },
  { label: "arbitrum.gmxapi.ai", apiUrl: "https://arbitrum.gmxapi.ai" },
  { label: "arbitrum.gmxapi.io", apiUrl: "https://arbitrum.gmxapi.io" },
];

function scan(obj) {
  const s = JSON.stringify(obj, (_k, v) => (typeof v === "bigint" ? v.toString() : v)).toLowerCase();
  return { hasNew: s.includes(NEW_ROUTER), hasOld: s.includes(OLD_ROUTER) };
}

(async () => {
  if (!ACCOUNT || !SUBACCOUNT || !ORDER_KEY) {
    console.error("Set GMX_ACCOUNT, GMX_SUBACCOUNT, ORDER_KEY (see header).");
    process.exit(2);
  }
  const approval = getEmptySubaccountApproval(CHAIN_ID, SUBACCOUNT);
  const out = [];
  for (const r of REGIONS) {
    const rec = { region: r.label, apiUrl: r.apiUrl || "(sdk default)" };
    try {
      const sdk = new GmxApiSdk(
        r.apiUrl ? { chainId: CHAIN_ID, apiUrl: r.apiUrl } : { chainId: CHAIN_ID }
      );
      const prepared = await sdk.prepareEditOrder({
        orderIds: [ORDER_KEY],
        newTriggerPrice: usdToTrigger1e30(TRIGGER),
        mode: "express",
        from: ACCOUNT,
        subaccountAddress: SUBACCOUNT,
        subaccountApproval: approval,
      });
      const domain = prepared?.payload?.typedData?.domain;
      rec.domain_verifyingContract = domain?.verifyingContract ?? null;
      const relay = scan(prepared?.payload?.relayParams ?? {});
      const batch = scan(prepared?.payload?.batchParams ?? {});
      rec.relayParams_router = relay.hasNew ? "NEW(0x5176)" : relay.hasOld ? "OLD(0xfD05)" : "none";
      rec.batchParams_router = batch.hasNew ? "NEW(0x5176)" : batch.hasOld ? "OLD(0xfD05)" : "none";
      const dvc = (rec.domain_verifyingContract || "").toLowerCase();
      const domainSide = dvc === NEW_ROUTER ? "NEW" : dvc === OLD_ROUTER ? "OLD" : "other";
      const execSide = relay.hasNew || batch.hasNew ? "NEW" : relay.hasOld || batch.hasOld ? "OLD" : "?";
      rec.consistent = domainSide !== "other" && domainSide === execSide;
    } catch (e) {
      rec.error = String((e && e.message) || e).slice(0, 200);
    }
    out.push(rec);
  }
  console.log(JSON.stringify(out, null, 2));
  process.exit(0);
})();
