"""Actual sliced PV-SPI GCS ingress and C3 jobs vs independent scalar references.

The callback validates genuine PV messages then revokes each borrowed packet at
the next callback, including transient starvation. No hardware or timing claim.
"""
from __future__ import annotations

import functools
import json
from pathlib import Path
import subprocess
import tempfile
import zlib

import numpy as np

import c3_fec_trial as packed
import dual_spi
from test_dvbs2_8psk import scalar_frame
from pulse_profiles import FOUR_LEVELS_Q30

ROOT = Path(__file__).resolve().parents[1]
RATES = {5: "2/3", 6: "3/4", 8: "5/6"}


@functools.lru_cache(maxsize=24)
def reference(code_index: int, frame: int):
    rate = RATES[code_index]
    bb = dual_spi.bbframe(frame, gcs=True, rate=rate)
    fec = dual_spi.ref.fecframe(bb, rate, False)
    points = scalar_frame(fec, rate, False, True, 0)
    levels = np.asarray(FOUR_LEVELS_Q30, np.float64) / 2**30
    codes = np.asarray([np.abs(axis[:, None] - levels).argmin(axis=1)
                        for axis in (points.real, points.imag)], np.uint32)
    return zlib.crc32(np.packbits(bb).tobytes()), codes


def check(record: dict, stalled: bool) -> None:
    code, frames = record["code_index"], record["frames"]
    points = np.concatenate([reference(code, f)[1] for f in range(frames)], axis=1)
    groups, fill = divmod(points.shape[1], 16)
    values = packed.pack_codes(points[:, :groups * 16])
    expected = np.zeros((2, 4096), dtype="<u4")
    first = max(0, groups - 4096)
    expected[:, np.arange(first, groups) & 4095] = values[:, first:]
    assert np.array_equal(np.asarray(record["rings"], dtype="<u4"), expected), (code, frames, "rings")
    assert record["published_groups"] == groups
    assert record["pending_fill"] == fill
    assert record["pending_codes"] == [sum(int(v) << (2 * j) for j, v in enumerate(axis))
                                        for axis in points[:, groups * 16:]]
    assert record["bb_crc"] == [reference(code, f)[0] if f < frames else 0 for f in range(4)]
    assert bool(record["waits"]) == stalled


def main() -> None:
    src = ROOT / "firmware/src"
    with tempfile.TemporaryDirectory(prefix="b8-ingress-") as tmp:
        exe = Path(tmp) / "ingress"
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", f"-I{src}",
                        *(str(src / name) for name in ("dvbs2.c", "dvbs2_8psk.c", "pvproto.c",
                                                      "c3_fec.c", "b8_ingress.c")),
                        str(ROOT / "host/native/b8_ingress_test.c"), "-o", str(exe)], check=True)
        cases = [(options, 64, False) for options in (0, 1, 2, 4, 8, 16, 19, 27, 31)]
        cases += [(options, budget, True) for options in (0, 27) for budget in (1, 7, 63, 4096)]
        for options, budget, stalled in cases:
            result = subprocess.run([str(exe), str(options), str(budget), str(int(stalled))],
                                    check=True, capture_output=True, text=True)
            records = [json.loads(line) for line in result.stdout.splitlines()]
            assert len(records) == 12
            for record in records:
                check(record, stalled)
    print("Passed B8 real PV-message GCS ingress: 17 option/budget/starvation cases ×3 rates ×8 frames; "
          "all physical rings/carries and raw BB CRCs vs independent scalar GCS/BCH/LDPC/8PSK, "
          "borrowed-packet revocation, dirty FE/tail clearing, <=64-word and zero-budget guards.")


if __name__ == "__main__":
    main()
