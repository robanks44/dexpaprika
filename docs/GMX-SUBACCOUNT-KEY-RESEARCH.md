# Research brief — obtaining a usable GMX v2 subaccount signing key

Paste everything below into a fresh research chat. It's self-contained.

---

## What I'm trying to do

I'm building a Python + Node tool that programmatically **edits an existing
GMX v2 order** (specifically, moves the trigger price of a stop-loss order) on
**Arbitrum One (chainId 42161)**, using the official GMX SDK
(`@gmx-io/sdk`, package `@gmx-io/sdk/v2`) from a Node script.

To sign order actions without a wallet popup per action, GMX v2 uses
**One-Click Trading**, which creates a **subaccount** (a separate keypair the
main account authorizes on-chain with an action-count cap + expiry). My Node
script needs the **raw subaccount private key** so it can construct a
`PrivateKeySigner` and call the SDK's `prepareEditOrder` → `signOrder` →
`submitOrder` (express mode) flow.

I enabled One-Click Trading in the GMX web app (app.gmx.io) with a test wallet.
The subaccount is authorized on-chain. But the subaccount private key that GMX
stores in the browser is **encrypted**, so I can't just lift it. I need a
reliable, documented way to obtain the raw hex private key (or an alternative
way to sign subaccount actions programmatically).

## Exactly what I observed in browser Local Storage (app.gmx.io)

Under `https://app.gmx.io` Local Storage there are entries keyed like
`[42161,"0x<mainWalletAddress>"]`. Two are relevant:

1. A subaccount **credential** entry whose value is JSON:
   ```json
   {"privateKey":"U2FsdGVkX1<...base64...>","address":"0xc552ba2FbF87E5631968f968C2b0896Dd491E87c"}
   ```
   The `privateKey` string begins with `U2FsdGVkX1`, i.e. base64 of
   `Salted__` — the hallmark of **CryptoJS AES** (`CryptoJS.AES.encrypt`)
   output. So the subaccount private key is AES-encrypted with some passphrase,
   not stored in plaintext.

2. A subaccount **authorization** entry whose value is JSON like:
   ```json
   {
     "subaccount": "0xc552ba2FbF87E5631968f968C2b0896Dd491E87c",
     "subaccountRouterAddress": "0xAb3EDf0f3eed6804BAe1bD9bF90109ccadFD262e",
     "shouldAdd": false,
     "expiresAt": "1786353875",
     "maxAllowedCount": "95",
     "desChainId": "42161",
     "actionType": "0x2a0791687fd34f2095c484a9fa4e25057d3ef79a97fcd8c61436047a7bdf4cbe",
     "deadline": "1786353875",
     "integrationId": "0x0000...0000",
     "nonce": "0",
     "signature": "0x<eip712 signature>",
     "signatureChainId": 42161,
     "signedAt": 1785749082492
   }
   ```

So: subaccount address = `0xc552ba2FbF87E5631968f968C2b0896Dd491E87c`,
SubaccountRouter = `0xAb3EDf0f3eed6804BAe1bD9bF90109ccadFD262e`,
maxAllowedCount 95, and the private key is CryptoJS-AES-encrypted.

## The two approaches I want researched — please dig into BOTH

### Approach A — Decrypt the browser-stored key (no private-key export)

The subaccount private key in Local Storage is `CryptoJS.AES.encrypt(...)`
output (`U2FsdGVkX1…` = `Salted__`). GMX's frontend is open source
(GitHub: **gmx-io/gmx-interface**). Questions:

- **How does gmx-interface encrypt/decrypt the one-click subaccount private
  key?** Find the exact code. What library and mode (CryptoJS AES-CBC with an
  OpenSSL-style `Salted__` KDF — MD5-based EVP_BytesToKey — presumably)?
- **What is the passphrase / encryption key?** Is it derived from a wallet
  **signature** over a specific fixed message (personal_sign / EIP-712)? If so,
  give the **exact message** signed and how the signature maps to the CryptoJS
  passphrase. If the user re-signs that same message in their wallet, can they
  reproduce the passphrase and decrypt the blob offline?
- Provide a **standalone Node/JS snippet** (using `crypto-js` or Node `crypto`)
  that, given the encrypted blob + the reproducible passphrase, outputs the raw
  hex private key (`0x…`). Cite the gmx-interface source files
  (path + function names).

### Approach B — Re-derive the subaccount key via the official SDK

The subaccount key appears to be **deterministically derived from the main
wallet's signature** (GMX's own code does this). Questions:

- In **`@gmx-io/sdk` v2**, what do `generateSubaccount(signer)` /
  `activateSubaccount(signer, {...})` actually do, and **do they expose the RAW
  subaccount private key**, or only the address? Read the SDK source
  (GitHub: **gmx-io/gmx-sdk** or the `@gmx-io/sdk` package's `build`/`src`).
- Is the derivation **deterministic and identical to the web app's**, i.e. if I
  run `generateSubaccount(new PrivateKeySigner(<mainKey>))` on the same wallet,
  do I get the **same** subaccount address
  (`0xc552ba2FbF87E5631968f968C2b0896Dd491E87c`) — and can I get its raw key?
- What is the **exact derivation** (message signed by the main wallet, then
  e.g. `keccak256(signature)` → private key)? Give a standalone Node snippet
  that reproduces the subaccount key from the main wallet key.

### Approach C — Can the SDK sign a subaccount order edit WITHOUT me holding the raw key?

- Does `@gmx-io/sdk` v2 provide a subaccount signing path where I pass the
  encrypted blob + a decrypt callback, or a subaccount-signer object, rather
  than a raw hex key — such that `prepareEditOrder`/`signOrder`/`submitOrder`
  (express mode) can be driven headless? If so, document the exact call
  sequence.

## What a good answer gives me

A concrete, tested procedure that ends with **either**:
- a raw hex subaccount private key (`0x…`) for the authorized subaccount
  `0xc552ba2FbF87E5631968f968C2b0896Dd491E87c`, that a Node
  `new PrivateKeySigner(key)` can use to sign GMX order edits; **or**
- a documented headless signing path in `@gmx-io/sdk` v2 that doesn't require
  the raw key.

Please cite specific source files / commits in **gmx-io/gmx-interface** and the
**@gmx-io/sdk** package, and prefer answers grounded in that source over
guesses. Note the SDK ESM build has extensionless imports that break under
Node's ESM resolver — I'm using the CJS entry (`require("@gmx-io/sdk/v2")`).

## Reference addresses / facts

- Chain: Arbitrum One, chainId 42161.
- SubaccountRouter: `0xAb3EDf0f3eed6804BAe1bD9bF90109ccadFD262e`.
- Subaccount (authorized): `0xc552ba2FbF87E5631968f968C2b0896Dd491E87c`.
- SDK: `@gmx-io/sdk` (import `@gmx-io/sdk/v2`), classes `GmxApiSdk`,
  `PrivateKeySigner`; methods used: `fetchOrders`, `prepareEditOrder`,
  `signOrder`, `submitOrder`, `fetchOrderStatus`; subaccount:
  `generateSubaccount`, `activateSubaccount`.
- Encrypted value marker: `U2FsdGVkX1…` = base64(`Salted__`) = CryptoJS AES.
