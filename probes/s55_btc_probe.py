"""S5.5 probe: live Esplora + mempool.space address stats for the real BTC wallet."""

import json
import pathlib
import urllib.request

ADDRESS = "bc1qwkuxaap3h4vklr3x5gncm2at9zak7qvnvdh7ff"
PEERS = {
    "blockstream": "https://blockstream.info/api",
    "mempool": "https://mempool.space/api",
}

out: dict[str, object] = {"address": ADDRESS}
for name, base in PEERS.items():
    req = urllib.request.Request(
        f"{base}/address/{ADDRESS}", headers={"User-Agent": "dexpaprika/1.0"}
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        out[name] = {"status": r.status, "payload": json.loads(r.read())}

path = pathlib.Path("probes/out/s55")
path.mkdir(parents=True, exist_ok=True)
(path / "address_stats.json").write_text(json.dumps(out, indent=2) + "\n")
print(json.dumps(out, indent=2))
