# Not executed by the app; see the header of requirements.lock.
"""Build requirements.lock (pip --require-hashes format) from a pip freeze, with sha256 of every file on PyPI."""
import json, sys, urllib.request, concurrent.futures as cf
freeze = [l.strip() for l in open(sys.argv[1]) if "==" in l]
def hashes(line):
    name, ver = line.split("==")
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{ver}/json", timeout=30) as r:
        data = json.load(r)
    return line, sorted({u["digests"]["sha256"] for u in data["urls"]})
with cf.ThreadPoolExecutor(8) as ex:
    rows = sorted(ex.map(hashes, freeze), key=lambda x: x[0].lower())
out = ["# Fully pinned runtime dependencies with hashes (pip install --require-hashes -r requirements.lock).",
       "# Generated from requirements.txt in a clean Python 3.12 environment; regenerate when requirements.txt changes:",
       "#   python3.12 -m venv /tmp/lock && /tmp/lock/bin/pip install -r requirements.txt && /tmp/lock/bin/pip freeze > /tmp/freeze.txt",
       "#   python scripts/make_lock.py /tmp/freeze.txt > requirements.lock", ""]
for line, hs in rows:
    out.append(line + " \\")
    out += [f"    --hash=sha256:{h}" + (" \\" if i < len(hs) - 1 else "") for i, h in enumerate(hs)]
print("\n".join(out))
