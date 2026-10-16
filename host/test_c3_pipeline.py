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
# Retain every C4 combination and sample the independent C5/C6 bits with each
# other, both superseded selectors, BCH placement, and the full combined masks.
OPTION_CASES = tuple(range(32)) + tuple(
    feature | base for feature in (32, 64, 96) for base in (0, 1, 2, 3, 4, 8, 16, 27, 31))


def main() -> None:
    assert len(set(OPTION_CASES)) == len(OPTION_CASES)
    for bit in (1, 2, 4, 8, 16, 32, 64):
        assert any(options & bit for options in OPTION_CASES)
        assert any(not options & bit for options in OPTION_CASES)
    assert {32, 33, 64, 66, 96, 99, 123, reference.C8_ALL} <= set(OPTION_CASES)
    src = ROOT / "firmware/src"
    with tempfile.TemporaryDirectory(prefix="c3-pipeline-") as build:
        exe = Path(build) / "pipeline_test"
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", f"-I{src}",
                        *(str(src / name) for name in ("dvbs2.c", "dvbs2_8psk.c", "pvproto.c", "c3_fec.c")),
                        str(ROOT / "host/native/c3_pipeline_test.c"), "-o", str(exe)], check=True)
        results = [subprocess.run([str(exe), str(options)], check=True, capture_output=True, text=True)
                   for options in OPTION_CASES]
    records = [json.loads(line) for result in results for line in result.stdout.splitlines()]
    assert len(records) == len(OPTION_CASES) * 24, len(records)
    expected_cache, physical_cache = {}, {}
    for record in records:
        code, frames = record["code_index"], record["frames"]
        key = (code, frames)
        if key not in expected_cache:
            expected_cache[key] = reference.ring_reference(code, frames, RING)
        want = expected_cache[key]
        got = np.asarray(record["rings"], dtype="<u4")
        assert got.shape == (2, RING)
        assert [zlib.crc32(axis.tobytes()) for axis in got] == want["code_crc"], (code, frames, "CRC")
        for field in ("published_groups", "pending_fill", "pending_codes"):
            assert record[field] == want[field], (code, frames, field)
        # Check every physical word too, including untouched zero slots before
        # the first wrap. This independently packs concatenated exact symbols.
        if key not in physical_cache:
            points = np.concatenate([reference.frame_codes(f, code) for f in range(frames)], axis=1)
            groups = points.shape[1] // 16
            packed = reference.pack_codes(points[:, :groups * 16])
            expected = np.zeros((2, RING), dtype="<u4")
            first = max(0, groups - RING)
            expected[:, np.arange(first, groups) & (RING - 1)] = packed[:, first:]
            physical_cache[key] = expected
        expected = physical_cache[key]
        assert np.array_equal(got, expected), (code, frames, "physical rings")
    print(f"Passed C3 real portable job pipeline: {len(OPTION_CASES)} sampled option masks ×three rates ×16 continuous frames, "
          "odd-first completion, every pair's physical rings/carry vs independent Python; "
          "GCS zero/one-word slices vs existing PV adapter and canaries.")


if __name__ == "__main__":
    main()
