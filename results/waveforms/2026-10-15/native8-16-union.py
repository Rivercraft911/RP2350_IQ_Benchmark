#!/usr/bin/env python3
"""Storage/alphabet/symmetry sanity check only; no runtime or RF claims.

Run from any directory; JSON goes to stdout. No files are changed. The taps
serve only as one symmetric integer example; no new filter is designed here.
"""
from pathlib import Path
import hashlib
import itertools
import json
import math

tap_file = Path(__file__).with_name("historical-preeq.json")
tap = json.loads(tap_file.read_text())[0]["coefficients"]
assert len(tap) == 18 and tap == tap[::-1]
L, N, K = 9, 2, 4
# Chronological 12-coordinate history, eight output samples per four symbols.
matrix = [[tap[2 * (L - 1 + r // N - k) + r % N]
           if 0 <= L - 1 + r // N - k < L else 0
           for k in range(L + K - 1)] for r in range(K * N)]
assert all(matrix[r][k] == matrix[7-r][11-k]
           for r in range(8) for k in range(12))
checks = []
for gamma in (3.15, 2.85, 2.75, 2.70, 2.60, 2.57):
    a, b, c, d = math.sin(math.pi/12), 1/math.sqrt(2), math.cos(math.pi/12), 1/(gamma*math.sqrt(2))
    alphabet = [0, a, -a, b, -b, c, -c, d, -d, 1, -1]
    assert len(set(round(x, 12) for x in alphabet)) == 11
    points = [complex(math.cos(k*math.pi/4), math.sin(k*math.pi/4)) for k in range(8)]
    points += [complex(math.cos((2*k+1)*math.pi/12), math.sin((2*k+1)*math.pi/12)) for k in range(12)]
    points += [complex(math.cos((2*k+1)*math.pi/4), math.sin((2*k+1)*math.pi/4))/gamma for k in range(4)]
    # Native diagonal header/pilot coordinates are already present. Quarter-turn
    # Gold rotation stays in the same signed axis union.
    points += [complex(b, b), complex(-b, b)]
    for point, turn in itertools.product(points, range(4)):
        z = point * (1j ** turn)
        assert all(min(abs(x-v) for v in alphabet) < 1e-12 for x in (z.real, z.imag))
    checks.append({"gamma": gamma, "coordinate_levels": len(alphabet)})

radix = 11
triples = list(itertools.product(range(radix), repeat=3))
assert len(triples) == radix**3 == 1331
assert {x+radix*y+radix**2*z for x,y,z in triples} == set(range(1331))
assert all((x+radix*y+radix**2*z) // 121 == z for x,y,z in triples)
# A 12-bit key contains three 4-bit coordinate codes. Invalid nibble codes
# 11..15 need rejection/sentinel entries, not aliases to valid rows.
valid_nibble_keys = {(x | y << 4 | z << 8) for x,y,z in triples}
assert len(valid_nibble_keys) == 1331 and max(valid_nibble_keys) < 4096
phase_l1 = max(sum(abs(tap[2*a+p]) for a in range(L)) for p in range(N)) / 256
print(json.dumps({
    "scope": "Future mathematical architecture idea; not implemented or timed",
    "L": L, "SPS": N, "output_symbols_per_group": K,
    "history_coordinates_per_axis": L+K-1,
    "native_alphabet_checks": checks,
    "symmetric_matrix_elements_checked": 8*12,
    "chunk_coordinates": [3,3,3,3], "rows_per_chunk": radix**3,
    "axis_samples_per_row": 8, "bytes_per_row": 16,
    "one_table_bytes": radix**3*16,
    "four_independent_tables_bytes": 4*radix**3*16,
    "two_symmetry_shared_tables_bytes": 2*radix**3*16,
    "packed12_to_radix11_map_bytes": 4096*2,
    "two_tables_plus_one_map_bytes": 2*radix**3*16+4096*2,
    "valid_packed12_keys": len(valid_nibble_keys), "invalid_packed12_keys": 4096-len(valid_nibble_keys),
    "optional_separate_radix_reverse_map_bytes": radix**3*2,
    "axis_row_lookups_per_quad": 4, "both_axes_row_lookups_per_quad": 8,
    "current_three_chunk_both_axes_row_lookups_per_quad": 6,
    "chunk_rounding_error_bound_lsb": 4/2,
    "additional_Q8_coordinate_coefficient_rounding_bound_lsb": L/(2*256),
    "illustrative_phase_l1_lsb": phase_l1,
    "illustrative_final_and_partial_sum_bound_lsb": phase_l1 + 4/2 + L/(2*256),
    "illustrative_tap_file_sha256": hashlib.sha256(tap_file.read_bytes()).hexdigest(),
}, indent=2))
