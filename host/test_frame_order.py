"""Native frame-parallel ownership model, then independent staged QPSK bit comparison.

The C harness reverses every even/odd FEC completion, enforces the stateful input
token, exercises output ring backpressure and wrap, and checks stale/wrong ownership,
sequence/cursor rollover and exact guard boundaries. Each staged PLFRAME is locally
padded, but only its exact symbols enter the persistent publisher's partial word.
This is a functional interleaving model, not device timing or concurrent LDPC proof.
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
sys.path.insert(0, str(ROOT / "reference/dvbs2"))
import dvbs2 as ref  # noqa: E402
import iqlut  # noqa: E402
import fwsrc  # noqa: E402  firmware/src/<area>/ layout


def expected(record: dict) -> np.ndarray:
    code = ref.code("5/6", False)
    axes: list[list[np.ndarray]] = [[], []]
    for sequence in range(record["frames"]):
        seed = (0xD1B54A35 ^ (sequence * 0x9E3779B9)) & 0xFFFFFFFF
        words = iqlut.xorshift32(seed, (code.kbch + 31) // 32).copy()
        words[0] ^= np.uint32((sequence & 1) << 31)
        bits = ((words[:, None] >> np.arange(31, -1, -1, dtype=np.uint32)) & 1).astype(np.uint8)
        b_i, b_q = ref.plframe(ref.fecframe(bits.ravel()[:code.kbch], "5/6", False),
                             "5/6", False, pilots=bool(record["pilots"]))
        assert len(b_i) == record["symbols"]
        axes[0].append(b_i)
        axes[1].append(b_q)
    return ref.pack_words(*(np.concatenate(axis) for axis in axes))


def main() -> None:
    src = ROOT / "firmware/src"
    with tempfile.TemporaryDirectory(prefix="frame-order-") as build:
        exe = Path(build) / "frame_order_test"
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", *fwsrc.INCLUDES,
                        str(src / "dvbs2/dvbs2.c"), str(src / "cpu_shaping/iqgen.c"),
                        str(ROOT / "host/native/frame_order_test.c"), "-o", str(exe)], check=True)
        records = [json.loads(line) for line in subprocess.check_output([str(exe)], text=True).splitlines()]
    assert len(records) == 2
    for record in records:
        np.testing.assert_array_equal(np.asarray(record["words"], dtype=np.uint32), expected(record))
        full, fill = divmod(record["frames"] * record["symbols"], 16)
        assert (record["full_words"], record["fill"]) == (full, fill)
        assert record["blocked"] == record["frames"] // 2
        print(f"ok {record['code']} pilots={record['pilots']} frames={record['frames']} "
              f"odd-first, ring wrap/backpressure, partial={fill}: independent bits exact")
    print("Ownership negatives, sequence/cursor rollover and exact reuse guard passed; "
          "shared LDPC scratch remains serialized.")


if __name__ == "__main__":
    main()
