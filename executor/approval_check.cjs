// READ-ONLY: identify which router domain a stored One-Click subaccount
// approval was signed against. No key, no network, no state change — pure
// signature recovery (EIP-712), offline.
//
// Why: GMX is mid relay-router migration. The web app signs the approval for
// one router generation while the relay executes on another, so the app loops
// "approval is invalid". If the stored signature recovers under the OLD router
// domain (where execution currently happens), our executor can carry it and
// refresh the subaccount on-chain ourselves.
//
// Usage:
//   1. In the browser (app.gmx.io -> DevTools -> Application -> Local Storage),
//      find the one-click AUTHORIZATION entry for your account (the JSON with
//      "subaccount", "shouldAdd", "expiresAt", "signature", ...). Copy the JSON
//      VALUE verbatim into executor\subaccount_approval.json  (re-copy it fresh
//      — after your re-signs, the latest signature is what matters).
//   2. $env:GMX_ACCOUNT = "0xd944...47e4"   # main/test wallet
//   3. node approval_check.cjs

const fs = require("fs");
const path = require("path");
const { recoverTypedDataAddress, getAddress } = require("viem");

const ACCOUNT = process.env.GMX_ACCOUNT;

const CANDIDATE_ROUTERS = {
  "SubaccountGelatoRelayRouter (OLD, executing now)":
    "0xfD0596f708d9D950E0eF7b5d191e5F8e55b8a67f",
  "SubaccountGelatoRelayRouter (NEW, per docs)":
    "0x517602BaC704B72993997820981603f5E4901273",
  "SubaccountRouter (stored in entry)":
    "0xAb3EDf0f3eed6804BAe1bD9bF90109ccadFD262e",
  "GelatoRelayRouter (OLD)": "0x5503b99308dB6923758F9A22d118207D633c4e87",
  "GelatoRelayRouter (NEW)": "0xa9090E2fd6cD8Ee397cF3106189A7E1CFAE6C59C",
};

const TYPES = {
  SubaccountApproval: [
    { name: "subaccount", type: "address" },
    { name: "shouldAdd", type: "bool" },
    { name: "expiresAt", type: "uint256" },
    { name: "maxAllowedCount", type: "uint256" },
    { name: "actionType", type: "bytes32" },
    { name: "nonce", type: "uint256" },
    { name: "desChainId", type: "uint256" },
    { name: "deadline", type: "uint256" },
    { name: "integrationId", type: "bytes32" },
  ],
};

(async () => {
  const file = path.join(__dirname, "subaccount_approval.json");
  if (!fs.existsSync(file)) {
    console.error("Missing subaccount_approval.json — see header for how to create it.");
    process.exit(2);
  }
  const entry = JSON.parse(fs.readFileSync(file, "utf8"));
  if (!ACCOUNT) {
    console.error("Set GMX_ACCOUNT to your main/test wallet address.");
    process.exit(2);
  }

  // Sign-time message, reconstructed verbatim from the stored entry.
  const message = {
    subaccount: entry.subaccount,
    shouldAdd: Boolean(entry.shouldAdd),
    expiresAt: BigInt(entry.expiresAt),
    maxAllowedCount: BigInt(entry.maxAllowedCount),
    actionType: entry.actionType,
    nonce: BigInt(entry.nonce),
    desChainId: BigInt(entry.desChainId),
    deadline: BigInt(entry.deadline),
    integrationId:
      entry.integrationId ||
      "0x0000000000000000000000000000000000000000000000000000000000000000",
  };
  const chainIds = [
    Number(entry.signatureChainId || 42161),
    Number(entry.desChainId || 42161),
  ].filter((v, i, a) => a.indexOf(v) === i);

  const expected = getAddress(ACCOUNT);
  const results = [];
  for (const [label, router] of Object.entries(CANDIDATE_ROUTERS)) {
    for (const cid of chainIds) {
      try {
        const recovered = await recoverTypedDataAddress({
          domain: {
            name: "GmxBaseGelatoRelayRouter",
            version: "1",
            chainId: cid,
            verifyingContract: getAddress(router),
          },
          types: TYPES,
          primaryType: "SubaccountApproval",
          message,
          signature: entry.signature,
        });
        results.push({
          router: label,
          chainId: cid,
          recovered,
          MATCH: recovered.toLowerCase() === expected.toLowerCase(),
        });
      } catch (e) {
        results.push({ router: label, chainId: cid, error: String(e.message).slice(0, 100) });
      }
    }
  }
  const match = results.find((r) => r.MATCH);
  console.log(JSON.stringify({
    account: expected,
    stored_entry: {
      subaccount: entry.subaccount,
      shouldAdd: entry.shouldAdd,
      expiresAt: entry.expiresAt,
      maxAllowedCount: entry.maxAllowedCount,
      nonce: entry.nonce,
      deadline: entry.deadline,
      signedAt: entry.signedAt ?? null,
    },
    verdict: match
      ? `signed for: ${match.router} (chainId ${match.chainId})`
      : "NO candidate domain matches — unexpected; paste this output back",
    results,
  }, (_k, v) => (typeof v === "bigint" ? v.toString() : v), 2));
})();
