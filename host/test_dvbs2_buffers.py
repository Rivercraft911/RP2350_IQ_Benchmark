"""Verify caller-owned FE interleaving against independent Python DVB-S2 output.

All 42 code/pilot settings use three different frames and all three BCH selectors.
The C harness also checks legacy-wrapper equivalence, untouched waiting buffers,
caller BB preservation, canaries, and the continuous partial symbol word. Degree160
frontends additionally use chunks of 1/7/128/511 words and a bit-serial parity check;
unsupported modes must leave init arguments untouched before the explicit fallback.
This is a functional sequential interleaving test, not a multicore timing measurement.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "reference"))
sys.path.insert(0, str(ROOT / "reference" / "dvbs2"))
import dvbs2 as ref  # noqa: E402
import iqlut  # noqa: E402


def expected(record: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    frame, rate = record["code"].split()
    short = frame == "short"
    code = ref.code(rate, short)
    axes: list[list[np.ndarray]] = [[], []]
    for phase in range(record["frames"]):
        seed = (0xD1B54A35 ^ record["ci"] ^ (phase * 0x9E3779B9)) & 0xFFFFFFFF
        words = iqlut.xorshift32(seed, (code.kbch + 31) // 32).copy()
        words[0] ^= np.uint32((phase & 1) << 31)
        bits = ((words[:, None] >> np.arange(31, -1, -1, dtype=np.uint32)) & 1).astype(np.uint8)
        b_i, b_q = ref.plframe(ref.fecframe(bits.ravel()[:code.kbch], rate, short),
                             rate, short, pilots=bool(record["pilots"]))
        axes[0].append(b_i)
        axes[1].append(b_q)
    b_i, b_q = (np.concatenate(axis) for axis in axes)
    return ref.pack_words(b_i, b_q), b_i, b_q


def main() -> None:
    src = ROOT / "firmware" / "src"
    with tempfile.TemporaryDirectory(prefix="dvbs2-buffers-") as build:
        exe = Path(build) / "buffers_test"
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", f"-I{src}",
                        str(src / "dvbs2.c"), str(src / "iqgen.c"),
                        str(ROOT / "host/native/dvbs2_buffers_test.c"), "-o", str(exe)], check=True)
        result = subprocess.run([str(exe), *sys.argv[1:2]], check=True, capture_output=True, text=True)
    records = [json.loads(line) for line in result.stdout.splitlines()]
    if not records:
        raise AssertionError("no matching DVB-S2 configuration")
    for record in records:
        want, b_i, b_q = expected(record)
        got = np.asarray(record["words"], dtype=np.uint32)
        assert record["ok"], record["code"]
        assert record["syms"] == len(b_i)
        assert np.array_equal(got, want), (record["code"], record["pilots"], "waveform bits")
        full, fill = divmod(len(b_i), 16)
        assert record["unflushed_words"] == full and record["fill"] == fill
        for name, bits in (("acc_i", b_i), ("acc_q", b_q)):
            tail = sum(int(bit) << j for j, bit in enumerate(bits[full * 16:]))
            assert record[name] == tail, (record["code"], name, "partial word")
        print(f"ok {record['code']:<11} pilots={record['pilots']} frames=3 "
              f"syms={len(b_i)} kernels=3 partial={fill}")
    if len(sys.argv) == 1:
        assert len(records) == 42, len(records)
    print(f"Passed {len(records)} configurations ×3 varied frames ×3 BCH selectors; "
          "continuous words match independent Python reference; "
          "degree160 chunk sizes1/7/128/511 also match bit-serial parity.")


if __name__ == "__main__":
    main()
