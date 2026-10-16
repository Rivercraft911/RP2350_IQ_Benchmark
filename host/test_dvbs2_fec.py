"""Focused native checks for tiled transpose and q60 parity packing; no hardware access.

Every matrix basis bit and128 dense matrices use the direct row/column definition.
Parity packing uses bit-serial LDPC, a minimum-sized destination with canaries, and
reconfiguration from a larger parity matrix to expose stale workspace dependencies.
Mapping uses direct per-symbol bit extraction and append, including all16 partial-word
fills, zero-length runs, ring wrap and destination canaries.
The native path cannot execute the Thumb-2 leaf; device frame/capture CRCs cover that.
"""
from pathlib import Path
import subprocess
import tempfile
import fwsrc

ROOT = Path(__file__).resolve().parents[1]

with tempfile.TemporaryDirectory(prefix="dvbs2-fec-") as build:
    exe = Path(build) / "fec_test"
    subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror",
                    *fwsrc.INCLUDES, str(ROOT / "host/native/dvbs2_fec_test.c"),
                    "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
