// S9.5 read-only smoke: does the sidecar reach GMX on Arbitrum Sepolia (421614)?
const { GmxApiSdk } = require("@gmx-io/sdk/v2");
(async () => {
  const chainId = Number(process.env.GMX_CHAIN_ID || "421614");
  const account = process.env.GMX_ACCOUNT || "0xC155A616e39D7B83E37e8FD9d2106E1BC056d7Fe";
  try {
    const sdk = new GmxApiSdk({ chainId });
    const orders = await sdk.fetchOrders({ address: account });
    console.log(JSON.stringify({ ok: true, chainId, account, order_count: orders.length }, null, 2));
  } catch (e) {
    console.log(JSON.stringify({ ok: false, chainId, error: String(e && e.message || e) }));
  }
})();
