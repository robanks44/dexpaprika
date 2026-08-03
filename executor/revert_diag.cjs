// READ-ONLY revert diagnostic. Replays the exact calldata Gelato tried to
// execute (from revert_calldata.txt) via eth_call impersonating the Gelato
// relayer, and decodes the revert reason with GMX's CustomErrors ABI. Also
// checks the likely economic causes: the account's USDC balance + allowance for
// the express relay fee, and the subaccount's status per GMX's API.
// Nothing is signed or submitted; state cannot change.
//
// Usage (from executor\):
//   $env:GMX_ACCOUNT    = "0xd944...47e4"
//   $env:GMX_SUBACCOUNT = "0xc552...E87c"
//   node revert_diag.cjs

const fs = require("fs");
const path = require("path");
const { decodeErrorResult, getAddress } = require("viem");
const { abis } = require("@gmx-io/sdk/abis/index");
const { GmxApiSdk } = require("@gmx-io/sdk/v2");

const ACCOUNT = process.env.GMX_ACCOUNT;
const SUBACCOUNT = process.env.GMX_SUBACCOUNT;
const CHAIN_ID = Number(process.env.GMX_CHAIN_ID || "42161");

const GELATO_RELAY = "0xaBcC9b596420A9E9172FD5938620E265a0f9Df92"; // SDK config
const ROUTER = "0xfD0596f708d9D950E0eF7b5d191e5F8e55b8a67f"; // tx `to` from the revert
const USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831";
const SYNTHETICS_ROUTER = "0x7452c558d45f8afC8c83dAe62C3f8A5BE19c71f6"; // approval spender
const FEE_NEEDED = 0x1f365; // 127845 = 0.127845 USDC (from the reverted relayParams)

const RPCS = [
  "https://arb1.arbitrum.io/rpc",
  "https://arbitrum-one.publicnode.com",
  "https://1rpc.io/arb",
];

async function rpc(method, params) {
  let lastErr;
  for (const url of RPCS) {
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
      });
      const j = await res.json();
      return { url, ...j };
    } catch (e) {
      lastErr = e;
    }
  }
  throw lastErr;
}

function decodeRevert(dataHex) {
  if (!dataHex || dataHex === "0x") return { note: "no revert data (plain revert)" };
  const candidates = [
    ["CustomErrors", abis.CustomErrors],
    ["SubaccountGelatoRelayRouter", abis.SubaccountGelatoRelayRouter],
    ["GelatoRelayRouter", abis.GelatoRelayRouter],
  ];
  for (const [label, abi] of candidates) {
    try {
      const d = decodeErrorResult({ abi, data: dataHex });
      return {
        abi: label,
        errorName: d.errorName,
        args: (d.args ?? []).map((a) => (typeof a === "bigint" ? a.toString() : a)),
      };
    } catch (_e) {
      /* try next abi */
    }
  }
  return { note: "unknown selector", selector: dataHex.slice(0, 10), raw: dataHex.slice(0, 200) };
}

function pad32(addr) {
  return addr.toLowerCase().replace("0x", "").padStart(64, "0");
}

async function erc20Call(selectorAndArgs) {
  const r = await rpc("eth_call", [{ to: USDC, data: selectorAndArgs }, "latest"]);
  return r.result ? BigInt(r.result) : null;
}

(async () => {
  if (!ACCOUNT || !SUBACCOUNT) {
    console.error("Set GMX_ACCOUNT and GMX_SUBACCOUNT (see header).");
    process.exit(2);
  }
  const out = {};

  // 1) Replay the reverted relay transaction.
  const calldata = fs
    .readFileSync(path.join(__dirname, "revert_calldata.txt"), "utf8")
    .trim();
  const sim = await rpc("eth_call", [
    { from: GELATO_RELAY, to: ROUTER, data: calldata },
    "latest",
  ]);
  out.rpc_used = sim.url;
  if (sim.error) {
    out.simulation = {
      reverted: true,
      message: sim.error.message,
      decoded: decodeRevert(sim.error.data),
    };
  } else {
    out.simulation = { reverted: false, note: "call succeeded now (?)", result: sim.result };
  }

  // 2) Fee economics: USDC balance + allowance of the MAIN account.
  const bal = await erc20Call("0x70a08231" + pad32(ACCOUNT)); // balanceOf(account)
  const allow = await erc20Call(
    "0xdd62ed3e" + pad32(ACCOUNT) + pad32(SYNTHETICS_ROUTER) // allowance(account, router)
  );
  out.fee_check = {
    fee_needed_usdc: FEE_NEEDED / 1e6,
    usdc_balance: bal === null ? "read-failed" : Number(bal) / 1e6,
    usdc_allowance_to_router: allow === null ? "read-failed" : Number(allow) / 1e6,
    balance_sufficient: bal === null ? null : bal >= BigInt(FEE_NEEDED),
    allowance_sufficient: allow === null ? null : allow >= BigInt(FEE_NEEDED),
  };

  // 3) Subaccount status per GMX API.
  try {
    const sdk = new GmxApiSdk({ chainId: CHAIN_ID });
    out.subaccount_status = await sdk.fetchSubaccountStatus({
      account: getAddress(ACCOUNT),
      subaccountAddress: getAddress(SUBACCOUNT),
    });
  } catch (e) {
    out.subaccount_status = { error: String((e && e.message) || e).slice(0, 300) };
  }

  console.log(
    JSON.stringify(out, (_k, v) => (typeof v === "bigint" ? v.toString() : v), 2)
  );
  process.exit(0);
})();
