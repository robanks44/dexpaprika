import json, urllib.request, sys

RPCS = ["https://base-rpc.publicnode.com", "https://base.llamarpc.com", "https://mainnet.base.org"]
POOL = "0x56aeaf4af2df4bdfd9d865830fefdd278b25e7ef"
NFPM2 = "0xa990c6a764b73bf43cee5bb40339c3322fb9d55f"   # second Slipstream deployment
PROXY = "0x6c1b20062970c886082687d8121d06aaace8886e"   # EIP-1167 custodian
WALLET = "0xC155A616e39D7B83E37e8FD9d2106E1BC056d7Fe"
TOKEN_ID = 5056427

def rpc(method, params):
    last = None
    for url in RPCS:
        try:
            req = urllib.request.Request(url, data=json.dumps(
                {"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode(),
                headers={"Content-Type":"application/json","User-Agent":"dexpaprika-probe/0.1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                out = json.loads(r.read())
            if "error" in out: last = out["error"]; continue
            return out["result"], url
        except Exception as e:
            last = str(e)
    return {"__error__": last}, None

def call(to, data, block="latest"):
    return rpc("eth_call", [{"to": to, "data": data}, block])

results = {}
bn, url = rpc("eth_blockNumber", [])
results["block_number"] = {"hex": bn, "int": int(bn,16) if isinstance(bn,str) else None, "rpc": url}
pin = hex(int(bn,16) - 3)  # small reorg margin
results["block_pin"] = pin

# pool slot0, tickSpacing, liquidity
s0,_ = call(POOL, "0x3850c7bd", pin)
results["pool_slot0_raw"] = s0
if isinstance(s0,str):
    w = [s0[2:][i:i+64] for i in range(0,len(s0)-2,64)]
    sqrtp = int(w[0],16); tick = int(w[1],16)
    if tick >= 2**255: tick -= 2**256
    # int24 sign-extension when packed as full word already handled by RPC abi (int24 -> signed)
    if tick > 2**23: tick -= 2**24
    price = (1.0001**tick)*1e12
    results["pool_decoded"] = {"sqrtPriceX96": sqrtp, "tick": tick, "price_usd_per_weth": round(price,2)}
ts,_ = call(POOL, "0xd0c93a7c", pin); results["tickSpacing"] = int(ts,16) if isinstance(ts,str) else ts
lq,_ = call(POOL, "0x1a686502", pin); results["pool_liquidity"] = int(lq,16) if isinstance(lq,str) else lq

# NFPM2.positions(tokenId) + ownerOf
arg = hex(TOKEN_ID)[2:].rjust(64,"0")
pos,_ = call(NFPM2, "0x99fbab88"+arg, pin)
results["nfpm2_positions_raw"] = pos
if isinstance(pos,str) and len(pos) > 2:
    w = [pos[2:][i:i+64] for i in range(0,len(pos)-2,64)]
    def sint(h, bits):
        v = int(h,16)
        if v >= 2**(bits-1) and v < 2**bits: v -= 2**bits
        if v > 2**255: v -= 2**256
        return v
    # slipstream positions: nonce, operator, token0, token1, tickSpacing, tickLower, tickUpper, liquidity, ...
    results["nfpm2_positions_decoded"] = {
        "token0": "0x"+w[2][24:], "token1": "0x"+w[3][24:],
        "tickSpacing": int(w[4],16),
        "tickLower": sint(w[5],256) if int(w[5],16)<2**255 else int(w[5],16)-2**256,
        "tickUpper": sint(w[6],256) if int(w[6],16)<2**255 else int(w[6],16)-2**256,
        "liquidity": int(w[7],16),
    }
own,_ = call(NFPM2, "0x6352211e"+arg, pin)
results["nfpm2_ownerOf_5056427"] = ("0x"+own[26:]) if isinstance(own,str) and len(own)>=66 else own

# proxy bytecode -> EIP-1167 target
code,_ = rpc("eth_getCode", [PROXY, pin])
results["proxy_code"] = code
if isinstance(code,str) and "363d3d373d3d3d363d73" in code:
    i = code.index("363d3d373d3d3d363d73") + len("363d3d373d3d3d363d73")
    impl = "0x"+code[i:i+40]
    results["eip1167_implementation"] = impl
    icode,_ = rpc("eth_getCode", [impl, pin])
    results["impl_code_len_bytes"] = (len(icode)-2)//2 if isinstance(icode,str) else None
    # try identifying: owner(), name(), symbol() on the PROXY (delegates to impl)
    for label, sel in [("owner","0x8da5cb5b"),("name","0x06fdde03"),("symbol","0x95d89b41"),
                       ("token0","0x0dfe1681"),("token1","0xd21220a7"),("pool","0x16f0115b"),
                       ("nft","0x47ccca02"),("tokenId","0x17d70f7c"),("gauge","0x51cff8d9")]:
        r,_ = call(PROXY, sel, pin)
        results[f"proxy_{label}"] = r
json.dump(results, open("out/onchain_base_probe.json","w"), indent=1)
print(json.dumps({k:v for k,v in results.items() if k not in ("proxy_code","nfpm2_positions_raw","pool_slot0_raw")}, indent=1)[:3500])
