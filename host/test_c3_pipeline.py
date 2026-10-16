"""Actual portable C3 GCS/FEC/mapper jobs and ordered publication vs Python.

Odd frames finish first, and every pair's complete physical rings and carry are
checked. Sixteen continuous frames cover ring wraps and every boundary fill,
including a zero-fill frame boundary. No hardware or timing inference is used.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import zlib

import numpy as np

import c3_fec_trial as reference

ROOT = Path(__file__).resolve().parents[1]
RING = 4096


def main() -> None:
    src = ROOT / "firmware/src"
    with tempfile.TemporaryDirectory(prefix="c3-pipeline-") as build:
        exe = Path(build) / "pipeline_test"
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", f"-I{src}",
                        *(str(src / name) for name in ("dvbs2.c", "dvbs2_8psk.c", "pvproto.c", "c3_fec.c")),
                        str(ROOT / "host/native/c3_pipeline_test.c"), "-o", str(exe)], check=True)
        result = subprocess.run([str(exe)], check=True, capture_output=True, text=True)
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(records) == 24, len(records)
    for record in records:
        code, frames = record["code_index"], record["frames"]
        want = reference.ring_reference(code, frames, RING)
        got = np.asarray(record["rings"], dtype="<u4")
        assert got.shape == (2, RING)
        assert [zlib.crc32(axis.tobytes()) for axis in got] == want["code_crc"], (code, frames, "CRC")
        for key in ("published_groups", "pending_fill", "pending_codes"):
            assert record[key] == want[key], (code, frames, key)
        # Check every physical word too, including untouched zero slots before
        # the first wrap. This independently packs concatenated exact symbols.
        points = np.concatenate([reference.frame_codes(f, code) for f in range(frames)], axis=1)
        groups = points.shape[1] // 16
        packed = reference.pack_codes(points[:, :groups * 16])
        expected = np.zeros((2, RING), dtype="<u4")
        first = max(0, groups - RING)
        expected[:, np.arange(first, groups) & (RING - 1)] = packed[:, first:]
        assert np.array_equal(got, expected), (code, frames, "physical rings")
    print("Passed C3 real portable job pipeline: three rates ×16 continuous frames, "
          "odd-first completion, every pair's physical rings/carry vs independent Python; "
          "GCS zero/one-word slices vs existing PV adapter and canaries.")


if __name__ == "__main__":
    main()
