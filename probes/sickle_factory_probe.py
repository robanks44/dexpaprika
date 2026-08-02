import json, time, urllib.request
from Crypto.Hash import keccak
def sel(sig):
    k = keccak.new(digest_bits=256); k.update(sig.encode()); return "0x"+k.hexdigest()[:8]
RPCS = ["https://base-rpc.publicnode.com","https://base.llamarpc.com"]
FACTORY = "0x71D234A3e1dfC161cc1d081E6496e76627baAc31"
WALLET  = "0xC155A616e39D7B83E37e8FD9d2106E1BC056d7Fe"
arg = WALLET[2:].lower().rjust(64,"0")
def call(to, data):
    for url in RPCS:
        try:
            req = urllib.request.Request(url, data=json.dumps({"jsonrpc":"2.0","id":1,"method":"eth_call","params":[{"to":to,"data":data},"latest"]}).encode(), headers={"Content-Type":"application/json","User-Agent":"dexpaprika-probe/0.1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                out = json.loads(r.read())
            if "error" in out: return {"error": out["error"]["message"]}
            return out["result"]
        except Exception as e:
            err = str(e)
    return {"error": err}
res = {}
for sig in ["sickles(address)","sickleOf(address)","predict(address)","getSickle(address)","deployedSickles(address)","getOrDeploy(address)"]:
    r = call(FACTORY, sel(sig)+arg)
    res[sig] = r
    time.sleep(0.7)
json.dump(res, open("out/sickle_factory_probe.json","w"), indent=1)
print(json.dumps(res, indent=1))
