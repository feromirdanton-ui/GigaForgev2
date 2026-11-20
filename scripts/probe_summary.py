import json
import sys

d = json.load(open("reports/live_probe.json"))
print("credentials_present:", d.get("credentials_present"))
print("model:", d.get("model"))
for p in d["probes"]:
    status = "OK  " if p["ok"] else "FAIL"
    print(f"{status} {p['name']:22} {p['latency_ms']:>6}ms  {p.get('error', '')[:70]}")
print("---")
print("passed:", sum(1 for p in d["probes"] if p["ok"]), "/", len(d["probes"]))
