"""S5 probe: full LP discovery recipe against Richard's wallet, all raws dumped."""
import json, sys, time, urllib.request
sys.path.insert(0, "src")
from dexpaprika._keccak import keccak256

RPCS = ["https://base-rpc.publicnode.com", "https://base.llamarpc.com"]
WALLET = "0xC155A616e39D7B83E37e8FD9d2106E1BC056d7Fe"
SICKLE_FACTORY = "0x71D234A3e1dfC161cc1d081E6496e76627baAc31"
NFPM_CANONICAL = "0x827922686190790b37229fd06084350E74485b72"
NFPM_SECOND = "0xa990c6a764b73bf43cee5bb40339c3322fb9d55f"
VOTER = "0x16613524e02ad97eDfeF371bC883F2F5d6C480A5"
POOL = "0x56aeaf4af2df4bdfd9d865830fefdd278b25e7ef"

def sel(sig): return "0x" + keccak256(sig.encode()).hex()[:8]
def addr_arg(a): return f"{int(a,16):064x}"
def uint_arg(v): return f"{v:064x}"

calls_log = {}
def rpc(method, params):
    last = None
    for url in RPCS:
        try:
            req = urllib.request.Request(url, data=json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode(),
                headers={"Content-Type":"application/json","User-Agent":"dexpaprika/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                out = json.loads(r.read())
            if "error" in out: last = out["error"]; continue
            return out["result"]
        except Exception as e:
            last = str(e); time.sleep(0.5)
    raise SystemExit(f"ring failed {method}: {last}")

def call(to, data, block):
    result = rpc("eth_call", [{"to": to, "data": data}, block])
    calls_log[f"{to.lower()}|{data}"] = result
    return result

pin = int(rpc("eth_blockNumber", []), 16) - 3
block = hex(pin)
out = {"pin": pin, "wallet": WALLET}

# 1. Sickle
sickle_raw = call(SICKLE_FACTORY, sel("sickles(address)") + addr_arg(WALLET), block)
sickle = "0x" + sickle_raw[-40:]
out["sickle"] = sickle
owner_raw = call(sickle, sel("owner()"), block)
out["sickle_owner_ok"] = owner_raw[-40:].lower() == WALLET[2:].lower()

# 2. NFPM registry enumeration for {wallet, sickle}
positions = []
for nfpm, label in [(NFPM_CANONICAL, "canonical"), (NFPM_SECOND, "second")]:
    factory_raw = call(nfpm, sel("factory()"), block)
    out[f"factory_{label}"] = "0x" + factory_raw[-40:]
    for owner_label, owner in [("wallet", WALLET), ("sickle", sickle)]:
        bal = int(call(nfpm, sel("balanceOf(address)") + addr_arg(owner), block), 16)
        out[f"balance_{label}_{owner_label}"] = bal
        for i in range(bal):
            tid_raw = call(nfpm, sel("tokenOfOwnerByIndex(address,uint256)") + addr_arg(owner) + uint_arg(i), block)
            token_id = int(tid_raw, 16)
            pos_raw = call(nfpm, sel("positions(uint256)") + uint_arg(token_id), block)
            w = [pos_raw[2:][k:k+64] for k in range(0, len(pos_raw)-2, 64)]
            def sint(h):
                v = int(h, 16)
                return v - (1 << 256) if v >= (1 << 255) else v
            positions.append({
                "nfpm": nfpm, "deployment": label, "owner": owner_label, "token_id": token_id,
                "token0": "0x"+w[2][24:], "token1": "0x"+w[3][24:],
                "tick_spacing": int(w[4],16), "tick_lower": sint(w[5]), "tick_upper": sint(w[6]),
                "liquidity": int(w[7],16),
                "tokens_owed0": int(w[10],16), "tokens_owed1": int(w[11],16),
            })
out["positions"] = positions

# 3. Pool resolution via factory getPool(token0, token1, tickSpacing)
p = positions[0]
factory = out["factory_second"]
pool_raw = call(factory, sel("getPool(address,address,int24)") + addr_arg(p["token0"]) + addr_arg(p["token1"]) + uint_arg(p["tick_spacing"]), block)
out["resolved_pool"] = "0x" + pool_raw[-40:]
out["pool_matches_known"] = out["resolved_pool"].lower() == POOL.lower()

# 4. Pool state
slot0 = call(POOL, sel("slot0()"), block)
out["sqrt_price_x96"] = int(slot0[2:66], 16)
tick_raw = int(slot0[66:130], 16)
out["pool_tick"] = tick_raw - (1<<256) if tick_raw >= (1<<255) else tick_raw

# 5. Gauge path (for completeness — expect empty: NFT is Sickle-held, not gauge-staked)
gauge_raw = call(VOTER, sel("gauges(address)") + addr_arg(POOL), block)
gauge = "0x" + gauge_raw[-40:]
out["gauge"] = gauge
if int(gauge, 16) != 0:
    for owner_label, owner in [("wallet", WALLET), ("sickle", sickle)]:
        try:
            staked = call(gauge, sel("stakedValues(address)") + addr_arg(owner), block)
            n = int(staked[66:130], 16) if len(staked) > 66 else 0
            out[f"gauge_staked_{owner_label}"] = n
        except SystemExit:
            out[f"gauge_staked_{owner_label}"] = "revert"

# 6. CL math sanity: amounts at current tick
from decimal import Decimal, localcontext
with localcontext() as ctx:
    ctx.prec = 60
    L = Decimal(p["liquidity"])
    sp = Decimal(out["sqrt_price_x96"]) / Decimal(2**96)
    sa = Decimal("1.0001") ** (Decimal(p["tick_lower"]) / 2)
    sb = Decimal("1.0001") ** (Decimal(p["tick_upper"]) / 2)
    amount0 = L * (sb - sp) / (sp * sb) / Decimal(10**18)
    amount1 = L * (sp - sa) / Decimal(10**6)
    price = Decimal("1.0001") ** Decimal(out["pool_tick"]) * Decimal(10**12)
out["computed"] = {"weth": str(amount0.quantize(Decimal("0.000001"))),
                   "usdc": str(amount1.quantize(Decimal("0.01"))),
                   "price_usd": str(price.quantize(Decimal("0.01")))}
out["raw_calls"] = calls_log
json.dump(out, open("probes/out/s5/discovery.json", "w"), indent=1)
print(json.dumps({k: v for k, v in out.items() if k != "raw_calls"}, indent=1))
