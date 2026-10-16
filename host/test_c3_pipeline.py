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
# Retain every C4 combination, prior C5/C6 cases, each C7 bit, and combined
# profiles. TABLE3+LDM is deliberately invalid, including the full ALL mask.
OPTION_CASES = tuple(dict.fromkeys(
    list(range(32)) +
    [feature | base for feature in (32, 64, 96) for base in (0, 1, 2, 3, 4, 8, 16, 27, 31)] +
    [feature | base for feature in (128, 256, 512) for base in (0, 27, 32, 123, 127)] +
    [251, 383, 639, 767, 895, 1023, 1024, 1152, 1275, 1536, 1568, 1787, 1791, 2048, 2299, 4096, 4347, 4603, 6395, 8192, 8443, 12539, 12795, 14587]))


def check_record(record, expected_cache, physical_cache):
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
    # Independently pack every physical word, including untouched slots before
    # the first wrap. Cache references, while keeping just one mask's output.
    if key not in physical_cache:
        points = np.concatenate([reference.frame_codes(f, code) for f in range(frames)], axis=1)
        groups = points.shape[1] // 16
        packed = reference.pack_codes(points[:, :groups * 16])
        expected = np.zeros((2, RING), dtype="<u4")
        first = max(0, groups - RING)
        expected[:, np.arange(first, groups) & (RING - 1)] = packed[:, first:]
        physical_cache[key] = expected
    assert np.array_equal(got, physical_cache[key]), (code, frames, "physical rings")


def main() -> None:
    assert len(set(OPTION_CASES)) == len(OPTION_CASES)
    for bit in (1 << shift for shift in range(14)):
        assert any(options & bit for options in OPTION_CASES)
        assert any(not options & bit for options in OPTION_CASES)
    assert {32, 33, 64, 66, 96, 99, 123, 512, 544, 1023,
            reference.C8_BEST_BASE, reference.C8_BEST, 1787, 1791} <= set(OPTION_CASES)
    src = ROOT / "firmware/src"
    expected_cache, physical_cache, records = {}, {}, 0
    with tempfile.TemporaryDirectory(prefix="c3-pipeline-") as build:
        exe = Path(build) / "pipeline_test"
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", f"-I{src}",
                        *(str(src / name) for name in ("dvbs2.c", "dvbs2_8psk.c", "pvproto.c", "c3_fec.c")),
                        str(ROOT / "host/native/c3_pipeline_test.c"), "-o", str(exe)], check=True)
        for options in OPTION_CASES:
            result = subprocess.run([str(exe), str(options)], check=True, capture_output=True, text=True)
            for line in result.stdout.splitlines():
                check_record(json.loads(line), expected_cache, physical_cache)
                records += 1
        for invalid in (reference.C8_ALL, reference.C9_ALL + 1, 1024 | 256, 2048 | 256, 2048 | 1024):
            rejected = subprocess.run([str(exe), str(invalid)], capture_output=True, text=True)
            assert rejected.returncode == 10 and not rejected.stdout, invalid
    assert records == len(OPTION_CASES) * 24, records
    print(f"Passed C3 real portable job pipeline: {len(OPTION_CASES)} sampled option masks ×three rates ×16 continuous frames, "
          "odd-first completion, every pair's physical rings/carry vs independent Python; "
          "GCS zero/one-word slices vs existing PV adapter and canaries.")


if __name__ == "__main__":
    main()
