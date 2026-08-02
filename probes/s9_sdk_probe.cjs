// S9 probe: READ-ONLY — official SDK against the real account. No key, no orders.
const { GmxApiSdk } = require("@gmx-io/sdk/v2");

(async () => {
  const account = "0xC155A616e39D7B83E37e8FD9d2106E1BC056d7Fe";
  const sdk = new GmxApiSdk({ chainId: 42161 });
  const orders = await sdk.fetchOrders({ address: account });
  console.log(JSON.stringify({
    count: orders.length,
    orders: orders.map(o => ({
      key: o.key, orderType: o.orderType, isLong: o.isLong,
      triggerPrice: String(o.triggerPrice ?? ""), sizeDeltaUsd: String(o.sizeDeltaUsd ?? ""),
    })),
  }, null, 2));
})();
