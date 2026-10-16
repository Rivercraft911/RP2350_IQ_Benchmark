"""Firmware source layout for host tests and tools: firmware/src/<area>/<file>.

Firmware sources include headers by bare name from any area, so a host compile needs one -I per
area (INCLUDES). path() finds a source file by its bare name.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / 'firmware/src'
AREAS = ('app', 'output', 'dvbs2', 'cpu_shaping', 'apsk', 'ingress', 'bench')  # as firmware/CMakeLists.txt
DIRS = tuple(SRC / area for area in AREAS)
INCLUDES = tuple(f'-I{d}' for d in DIRS)


def path(name: str) -> Path:
    """firmware/src/<area>/<name> for a bare file name such as 'dvbs2.c'."""
    hits = [d / name for d in DIRS if (d / name).is_file()]
    if len(hits) != 1:
        raise FileNotFoundError(f'{name}: {len(hits)} matches under {SRC}')
    return hits[0]
