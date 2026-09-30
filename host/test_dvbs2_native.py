"""Compile firmware/src/dvbs2.c natively and compare every QPSK PLFRAME with reference/dvbs2."""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "reference"))
sys.path.insert(0, str(ROOT / "reference" / "dvbs2"))
import dvbs2 as ref  # noqa: E402
import iqlut  # noqa: E402

src, exe = ROOT / "firmware" / "src", ROOT / "host" / "build" / "dvbs2_test"
exe.parent.mkdir(exist_ok=True)
subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", f"-I{src}", str(src / "dvbs2.c"),
                str(src / "iqgen.c"), str(ROOT / "host" / "native" / "dvbs2_test.c"), "-o", str(exe)],
               check=True)
args = sys.argv[1:2]
fail = 0
for line in subprocess.run([str(exe), *args], check=True, capture_output=True, text=True).stdout.splitlines():
    r = json.loads(line)
    frame, rate = r["code"].split()
    short = frame == "short"
    c = ref.code(rate, short)
    kbch = c["kbch"] if isinstance(c, dict) else c.kbch
    words = iqlut.xorshift32(r["seed"], (kbch + 31) // 32)
    bits = ((words[:, None] >> np.arange(31, -1, -1, dtype=np.uint32)) & 1).astype(np.uint8).ravel()[:kbch]
    bI, bQ = ref.plframe(ref.fecframe(bits, rate, short), rate, short, pilots=bool(r["pilots"]))
    n = len(bI)
    pad = (-n) % 16
    bI, bQ = np.concatenate([bI, np.zeros(pad, np.uint8)]), np.concatenate([bQ, np.zeros(pad, np.uint8)])
    w = (bI.reshape(-1, 16).astype(np.uint32) << np.arange(16, dtype=np.uint32)).sum(1) | \
        ((bQ.reshape(-1, 16).astype(np.uint32) << np.arange(16, dtype=np.uint32)).sum(1) << 16)
    got = np.array(r["words"], dtype=np.uint32)
    ok = r["bch_ok"] and r["ldpc_ok"] and n == r["syms"] and np.array_equal(got, w.astype(np.uint32))
    bad = int(np.count_nonzero(got != w)) if len(got) == len(w) else -1
    fail += not ok
    print(f"{'ok ' if ok else 'FAIL'} {r['code']:<11} pilots={r['pilots']} syms={r['syms']:>5} "
          f"(ref {n}) bch={r['bch_ok']} ldpc={r['ldpc_ok']} word mismatches={bad}")
sys.exit(1 if fail else 0)
