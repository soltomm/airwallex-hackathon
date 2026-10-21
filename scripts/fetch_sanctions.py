"""Download the US Treasury OFAC SDN list to data/sanctions/sdn.csv (public data, about 6 MB)."""
import urllib.request

from _bootstrap import ROOT

URL = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.CSV"
out = ROOT / "data" / "sanctions" / "sdn.csv"
out.parent.mkdir(parents=True, exist_ok=True)
req = urllib.request.Request(URL, headers={"User-Agent": "agentbank/0.1"})
with urllib.request.urlopen(req) as r:
    out.write_bytes(r.read())
print(f"saved {out} ({out.stat().st_size:,} bytes, {sum(1 for _ in out.open(encoding='latin-1')):,} rows)")
