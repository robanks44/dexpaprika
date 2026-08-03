// Decrypt a GMX v2 One-Click subaccount private key (Approach A).
//
// GMX stores the subaccount key in browser Local Storage as
// CryptoJS.AES.encrypt(rawKey, mainWalletAddress) — the passphrase is your
// MAIN wallet address, checksummed (EIP-55). No signature or private key is
// needed to decrypt; only the public address + the encrypted blob.
// (Verified against gmx-io/gmx-interface + @gmx-io/sdk@1.6.4 source.)
//
// Usage (PowerShell):
//   $env:GMX_SUBACCOUNT_BLOB   = "U2FsdGVkX1..."   # the "privateKey" value from Local Storage
//   $env:GMX_MAIN_ADDRESS      = "0xd944...47e4"   # your main/test wallet address
//   $env:GMX_EXPECTED_SUBACCOUNT = "0xc552...E87c" # optional: the subaccount address to verify
//   node executor/subaccount_decrypt.cjs
//
// It prints the raw subaccount private key and the address it derives, and
// tells you whether that matches the expected subaccount. Keep the key private;
// set it into DEXPAPRIKA_SECRET_GMX_SUBACCOUNT_KEY for the executor.

const CryptoJS = require("crypto-js");
const { getAddress, isHex } = require("viem");
const { privateKeyToAccount } = require("viem/accounts");

function decrypt(blob, passphrase) {
  return CryptoJS.AES.decrypt(blob, passphrase).toString(CryptoJS.enc.Utf8);
}

function main() {
  const blob = process.env.GMX_SUBACCOUNT_BLOB;
  const rawMain = process.env.GMX_MAIN_ADDRESS;
  const expected = process.env.GMX_EXPECTED_SUBACCOUNT;
  if (!blob || !rawMain) {
    console.error("Set GMX_SUBACCOUNT_BLOB and GMX_MAIN_ADDRESS (see header).");
    process.exit(2);
  }

  // Passphrase = checksummed main address. Fall back to the raw string exactly
  // as stored, in case the app serialized a different casing.
  const candidates = [];
  try {
    candidates.push(getAddress(rawMain));
  } catch (_e) {
    /* not a valid address for checksumming; still try raw below */
  }
  if (!candidates.includes(rawMain)) candidates.push(rawMain);

  let pk = "";
  let used = null;
  for (const pass of candidates) {
    try {
      const out = decrypt(blob, pass);
      if (out && isHex(out)) {
        pk = out;
        used = pass;
        break;
      }
    } catch (_e) {
      /* try next candidate */
    }
  }

  if (!pk || !isHex(pk)) {
    console.error(
      "Decrypt failed. Check the blob is the full 'privateKey' value and the" +
        " main address is correct. Casing matters — use the checksummed address."
    );
    process.exit(1);
  }

  const acct = privateKeyToAccount(pk);
  const matches = expected
    ? acct.address.toLowerCase() === expected.toLowerCase()
    : null;

  console.log(JSON.stringify({
    ok: true,
    subaccount_private_key: pk,
    derived_address: acct.address,
    passphrase_used: used === getAddressSafe(rawMain) ? "checksummed-address" : "raw-string",
    matches_expected: matches,
  }, null, 2));
}

function getAddressSafe(a) {
  try {
    return getAddress(a);
  } catch (_e) {
    return null;
  }
}

main();
