"""S6 probe: Aave v3 account data + native/ERC20 holdings, raws dumped."""
import json, sys, urllib.request
sys.path.insert(0, "src")
from dexpaprika._keccak import keccak256

RPC = "https://base-rpc.publicnode.com"
WALLET = "0xC155A616e39D7B83E37e8FD9d2106E1BC056d7Fe"
AAVE_POOL = "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5"  # Aave v3 Pool (Base) — verify by probe
TOKENS = {
    "WETH": "0x4200000000000000000000000000000000000006",
    "USDC": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "AERO": "0x940181a94A35A4569E4529A3CDfB74e38FD98631",
}

def sel(sig): return "0x" + keccak256(sig.encode()).hex()[:8]
def addr_arg(a): return f"{int(a,16):064x}"

calls = {}
def rpc(method, params):
    req = urllib.request.Request(RPC, data=json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode(),
        headers={"Content-Type":"application/json","User-Agent":"dexpaprika/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        out = json.loads(r.read())
    if "error" in out: raise SystemExit(f"{method}: {out['error']}")
    return out["result"]

def call(to, data, block):
    result = rpc("eth_call", [{"to": to, "data": data}, block])
    calls[f"{to.lower()}|{data}"] = result
    return result

pin = int(rpc("eth_blockNumber", []), 16) - 3
block = hex(pin)
out = {"pin": pin, "wallet": WALLET, "aave_pool": AAVE_POOL}

# Aave v3 getUserAccountData: (totalCollateralBase, totalDebtBase, availableBorrowsBase,
# currentLiquidationThreshold, ltv, healthFactor) — base currency = USD 1e8; HF 1e18.
raw = call(AAVE_POOL, sel("getUserAccountData(address)") + addr_arg(WALLET), block)
w = [raw[2:][i:i+64] for i in range(0, len(raw)-2, 64)]
out["aave"] = {
    "total_collateral_usd": int(w[0],16)/1e8,
    "total_debt_usd": int(w[1],16)/1e8,
    "available_borrows_usd": int(w[2],16)/1e8,
    "liq_threshold_bps": int(w[3],16),
    "ltv_bps": int(w[4],16),
    "health_factor": int(w[5],16)/1e18,
}

# Native balance + ERC20 balances
bal = rpc("eth_getBalance", [WALLET, block])
calls[f"native|{WALLET.lower()}"] = bal
out["native_eth"] = int(bal,16)/1e18
for symbol, token in TOKENS.items():
    raw = call(token, sel("balanceOf(address)") + addr_arg(WALLET), block)
    out[f"balance_{symbol}"] = int(raw,16)
json.dump({**out, "raw_calls": calls}, open("probes/out/s6/portfolio.json","w"), indent=1)
print(json.dumps(out, indent=1))
