"""S4.5 probe: pinned Multicall3.aggregate on Base + Arbitrum (raw JSON-RPC).

Verifies: hand-built ABI encoding for aggregate((address,bytes)[]), the
blockNumber tripwire, arb1 UA requirement, and pinned-read consistency.
"""
import json, urllib.request

MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
POOL = "0x56aeaf4af2df4bdfd9d865830fefdd278b25e7ef"
CHAINS = {
    "base": ["https://base-rpc.publicnode.com", "https://base.llamarpc.com"],
    "arbitrum": ["https://arb1.arbitrum.io/rpc", "https://arbitrum-one-rpc.publicnode.com"],
}
SLOT0, LIQ, GETBN, CHAINID = "3850c7bd", "1a686502", "42cbb15c", "3408e470"

def rpc(urls, method, params):
    last = None
    for url in urls:
        try:
            req = urllib.request.Request(url, data=json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode(),
                headers={"Content-Type":"application/json","User-Agent":"dexpaprika/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                out = json.loads(r.read())
            if "error" in out: last = out["error"]; continue
            return out["result"], url
        except Exception as e:
            last = str(e)
    raise SystemExit(f"ring failed: {last}")

def enc_aggregate(calls):
    # aggregate((address,bytes)[]) selector 252dba42
    head = "0000000000000000000000000000000000000000000000000000000000000020"
    arr = f"{len(calls):064x}"
    tuple_offsets, tuple_bodies = [], []
    running = 32 * len(calls)
    for to, data in calls:
        body = f"{int(to,16):064x}" + "0000000000000000000000000000000000000000000000000000000000000040"
        db = bytes.fromhex(data)
        padded = db.hex() + "00" * ((32 - len(db) % 32) % 32)
        body += f"{len(db):064x}" + padded
        tuple_offsets.append(f"{running:064x}")
        tuple_bodies.append(body)
        running += len(body) // 2
    return "0x252dba42" + head + arr + "".join(tuple_offsets) + "".join(tuple_bodies)

def dec_aggregate(hexstr):
    b = bytes.fromhex(hexstr[2:])
    block_number = int.from_bytes(b[0:32], "big")
    arr_off = int.from_bytes(b[32:64], "big")
    n = int.from_bytes(b[arr_off:arr_off+32], "big")
    outs = []
    for i in range(n):
        el_off = arr_off + 32 + int.from_bytes(b[arr_off+32+32*i:arr_off+64+32*i], "big")
        ln = int.from_bytes(b[el_off:el_off+32], "big")
        outs.append(b[el_off+32:el_off+32+ln])
    return block_number, outs

results = {}
for chain, urls in CHAINS.items():
    bn_hex, url = rpc(urls, "eth_blockNumber", [])
    pin = int(bn_hex, 16) - 3
    calls = [(MULTICALL3, GETBN), (MULTICALL3, CHAINID)]
    if chain == "base":
        calls += [(POOL, SLOT0), (POOL, LIQ)]
    calldata = enc_aggregate(calls)
    raw, url2 = rpc(urls, "eth_call", [{"to": MULTICALL3, "data": calldata}, hex(pin)])
    block_number, outs = dec_aggregate(raw)
    entry = {
        "rpc": url2, "pin": pin, "aggregate_block": block_number,
        "tripwire_ok": block_number == pin,
        "inner_getBlockNumber": int.from_bytes(outs[0], "big"),
        "chain_id": int.from_bytes(outs[1], "big"),
        "calldata": calldata, "raw_response": raw,
    }
    if chain == "base":
        tick_raw = int.from_bytes(outs[2][32:64], "big")
        tick = tick_raw - (1 << 256) if tick_raw >= (1 << 255) else tick_raw
        entry["pool_tick"] = tick
        entry["pool_liquidity"] = int.from_bytes(outs[3], "big")
    results[chain] = entry
json.dump(results, open("out/s45/pinned_multicall.json" if False else "probes/out/s45/pinned_multicall.json", "w"), indent=1)
print(json.dumps({k: {x: y for x, y in v.items() if x not in ("calldata", "raw_response")} for k, v in results.items()}, indent=1))
