"""Check firmware/src/pvproto.c natively: pattern messages vs host/cm5/pv_spi_tx.py, validation
counters, and TS-mode BBFRAMEs vs reference/dvbs2 ts_bbframes() (itself matched to gr-dtv)."""
import json
import subprocess
import sys
import zlib
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "host" / "cm5"), str(ROOT / "reference" / "dvbs2")]
import dvbs2 as ref  # noqa: E402
import pv_spi_tx as tx  # noqa: E402

src, exe = ROOT / "firmware" / "src", ROOT / "host" / "build" / "pv_test"
exe.parent.mkdir(exist_ok=True)
subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", f"-I{src}", str(src / "pvproto.c"),
                str(ROOT / "host" / "native" / "pv_test.c"), "-o", str(exe)], check=True)
r = json.loads(subprocess.run([str(exe)], check=True, capture_output=True, text=True).stdout)
checks = []
checks.append(("pattern messages == pv_spi_tx.py", all(bytes.fromhex(h) == tx.message(m, tx.pattern_payload(m))
                                                    for m, h in enumerate(r["pattern"]))))
checks.append(("30 good accepted", r["good"] == 30 and r["ok"] == 31))
checks.append(("bad CRC / header / sync counted", (r["bad_crc"], r["bad_hdr"], r["bad_sync"]) == (1, 1, 1)))
checks.append(("sequence gap = 2 lost", r["lost"] == 2))
packets = np.frombuffer(b"".join(tx.pattern_packet(k) for k in range(212)), np.uint8)
want = ref.ts_bbframes(packets[: 210 * 188], "2/3", ro=0.20)
got = [np.unpackbits(np.frombuffer(bytes.fromhex(h), np.uint8)) for h in r["bbframes"]]
checks.append(("BBFRAMEs 1-4 == ts_bbframes()", all(np.array_equal(g, w) for g, w in zip(got[:4], want[:4]))))
# frame 5: the reference with the same 2 real packets, then null packets
nullp = np.frombuffer(bytes([0x47, 0x1F, 0xFF, 0x10]) + b"\xff" * 184, np.uint8)
used = 4 * (43040 - 80) // 8 // 188 + 1                       # packets touched by frames 1-4
stream = np.concatenate([packets[: (used + 2) * 188]] + [nullp] * 40)
want5 = ref.ts_bbframes(stream, "2/3", ro=0.20)[4]
checks.append(("BBFRAME 5 with null stuffing == reference", np.array_equal(got[4], want5)))
checks.append(("TS CRC-32 over accepted packets", r["ts_crc"] == zlib.crc32(packets[: r["ts_packets"] * 188].tobytes())))
fail = 0
for name, ok in checks:
    print(f"{'ok ' if ok else 'FAIL'} {name}")
    fail += not ok
sys.exit(1 if fail else 0)
