"""Two caller-owned bounded FEC workers vs serial C and independent Python bits.

Minimum-sized dirty parity matrices have canaries. Real host threads run selectors
1/2/3, both LDPC modes, BB+BCH slices1/7/128/511 and LDPC units1/3/17/128; zero/idempotent steps,
unaligned parity tails and unsupported/undersized begin are checked. Not M33 timing.
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


def main() -> None:
    src = ROOT / "firmware/src"
    with tempfile.TemporaryDirectory(prefix="dvbs2-parallel-") as build:
        exe = Path(build) / "parallel_test"
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", "-pthread",
                        f"-I{src}", str(src / "dvbs2.c"),
                        str(ROOT / "host/native/dvbs2_parallel_test.c"), "-o", str(exe)], check=True)
        result = subprocess.run([str(exe)], check=True, capture_output=True, text=True)
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(records) == 48, len(records)
    for record in records:
        rate = record["code"].split()[1]
        code = ref.code(rate)
        n = (code.kbch + 31) // 32
        pattern = record["pattern"]
        if pattern == 3:
            seed = (0xCA38D219 ^ record["ci"] ^ (record["worker"] * 0x9E3779B9)) & 0xFFFFFFFF
            words = iqlut.xorshift32(seed, n)
        else:
            words = np.full(n, (0, 0xFFFFFFFF, 0xAAAAAAAA)[pattern], dtype=np.uint32)
        bits = ((words[:, None] >> np.arange(31, -1, -1, dtype=np.uint32)) & 1).astype(np.uint8)
        want = ref.fecframe(bits.ravel()[:code.kbch], rate)
        got_words = np.asarray(record["words"], dtype=np.uint32)
        got = ((got_words[:, None] >> np.arange(31, -1, -1, dtype=np.uint32)) & 1).astype(np.uint8).ravel()
        assert np.array_equal(got, want), (record["code"], pattern, record["worker"])
    print("Passed 48 independent Python FEC vectors; two threaded workers, six normal rates, "
          "three BCH selectors, both LDPC modes, four bounded slice combinations, serial parity and canaries.")


if __name__ == "__main__":
    main()
