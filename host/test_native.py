"""Compile the firmware kernels natively and check them bit-for-bit against reference/iqlut.py."""
import json
import subprocess
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "reference"))
import gen_coeffs  # noqa: E402
import iqlut as m  # noqa: E402

exe = ROOT / "host" / "build" / "kernel_test"
exe.parent.mkdir(exist_ok=True)
src = ROOT / "firmware" / "src"
subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", f"-I{src}",
                str(src / "iqgen.c"), str(ROOT / "host" / "native" / "kernel_test.c"),
                "-o", str(exe)], check=True)
rows = [json.loads(l) for l in subprocess.run([str(exe)], check=True, capture_output=True,
                                               text=True).stdout.splitlines()]
fail = 0
for r in rows:
    words = m.xorshift32(r["seed"], r["nwords"])
    lut = m.Lut(gen_coeffs.ALPHA, r["sps"], r["L"], 0.0, gen_coeffs.HEADROOM_DB)
    want = zlib.crc32(m.to_layout(m.generate(words, lut), r["layout"]).tobytes())
    ok = r["tables_ok"] and r["crc"] == want
    fail += not ok
    print(f"{'ok ' if ok else 'FAIL'} {r['kernel']:<10} sps={r['sps']} L={r['L']:<2} "
          f"crc={r['crc']:08x} want={want:08x} tables_ok={r['tables_ok']}")
sys.exit(1 if fail else 0)
