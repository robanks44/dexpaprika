"""S8 probe: one live low-priority ntfy publish; receipt dumped (topic redacted).

Run: DEXPAPRIKA_NTFY_TOPIC=<topic> python probes/s8_ntfy_probe.py
"""

import json
import os
import pathlib
import urllib.request

topic = os.environ["DEXPAPRIKA_NTFY_TOPIC"]
url = f"https://ntfy.sh/{topic}"

req = urllib.request.Request(
    url,
    data=b"dexpaprika S8 probe \xe2\x80\x94 alert channel verification (min priority)",
    headers={
        "Title": "dexpaprika probe",
        "Priority": "min",
        "Tags": "white_check_mark",
        "User-Agent": "dexpaprika/1.0",
    },
    method="POST",
)
with urllib.request.urlopen(req, timeout=20) as r:
    receipt = json.loads(r.read())
    status = r.status

# Redact the topic before dumping — knowledge-of-name secret.
if receipt.get("topic") == topic:
    receipt["topic"] = "REDACTED"

out = pathlib.Path("probes/out/s8")
out.mkdir(parents=True, exist_ok=True)
(out / "publish_receipt.json").write_text(
    json.dumps({"http_status": status, "receipt": receipt}, indent=2) + "\n"
)
print(json.dumps({"http_status": status, "receipt_keys": sorted(receipt)}, indent=2))
