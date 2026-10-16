"""R2 Hazard3 instruction semantics against scalar Python complete PLFRAMEs.

The native build explicitly emulates pack/zip/brev8/rev8; it measures no target
cycles. This checks the opt-in mapper for every supported rate/frame size,
pilots, Gold sequences, all partial fills, ring wrap and deferred publication.
"""
from __future__ import annotations
import ctypes as ct
import json
from pathlib import Path
import subprocess
import tempfile
import numpy as np
import test_dvbs2_8psk_codes as codes
import test_dvbs2_8psk as old
import r2_hazard3_trial as trial

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix="r2-hazard3-native-") as directory:
        temp = Path(directory)
        src = ROOT / "firmware/src"
        bridge = temp / "bridge.c"
        bridge.write_text('#include "dvbs2_8psk.h"\n#include "dvbs2_codes.h"\n'
                          '#include "r2_hazard3_bits.h"\n'
                          'const dvbs2_code_t *test_code(unsigned i){return &DVBS2_CODES[i];}\n'
                          'uint32_t test_reverse(uint32_t x){return r2_hazard3_reverse32(x);}\n'
                          'uint32_t test_zip(uint32_t x,uint32_t y){return r2_hazard3_zip16(x,y);}\n'
                          'uint32_t test_spread(uint32_t x){return r2_hazard3_spread16(x);}\n')
        library = temp / "r2.so"
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                        "-DIQ_R2_HAZARD3=1", "-DIQ_R2_HAZARD3_EMULATE=1", f"-I{src}",
                        str(src / "dvbs2_8psk.c"), str(src / "iq4hist.c"), str(bridge),
                        "-o", str(library)], check=True)
        lib = ct.CDLL(str(library))
        lib.test_code.argtypes = [ct.c_uint]; lib.test_code.restype = ct.c_void_p
        lib.dvbs2_8psk_arena_words.argtypes = [ct.c_void_p, ct.c_bool]
        lib.dvbs2_8psk_arena_words.restype = ct.c_uint32
        lib.dvbs2_8psk_init.argtypes = [ct.c_void_p, ct.c_void_p, ct.c_bool, ct.c_uint32, old.PTR, ct.c_uint32]
        lib.dvbs2_8psk_init.restype = ct.c_bool
        lib.dvbs2_8psk_frame.argtypes = [ct.c_void_p, old.PTR, ct.POINTER(old.Stream)]
        lib.dvbs2_8psk_flush.argtypes = [ct.POINTER(old.Stream)]
        lib.iq4hist_pack_group.argtypes = [ct.POINTER(old.Input), old.PTR, old.PTR]
        lib.dvbs2_8psk_codes_begin.argtypes = [ct.POINTER(codes.State), ct.c_void_p, old.PTR, ct.POINTER(codes.Codes)]
        lib.dvbs2_8psk_codes_step.argtypes = [ct.POINTER(codes.State), ct.c_uint32]
        lib.dvbs2_8psk_codes_step.restype = ct.c_bool
        lib.dvbs2_8psk_codes_frame.argtypes = [ct.c_void_p, old.PTR, ct.POINTER(codes.Codes)]
        lib.dvbs2_8psk_codes_flush.argtypes = [ct.POINTER(codes.Codes)]
        for name, arguments in (("reverse", 1), ("spread", 1), ("zip", 2)):
            fn = getattr(lib, "test_" + name)
            fn.argtypes = [ct.c_uint32] * arguments
            fn.restype = ct.c_uint32
        for x in range(65536):
            y = x * 40503 & 65535
            expected = sum(((x >> j & 1) | (y >> j & 1) << 1) << (2*j) for j in range(16))
            assert lib.test_zip(x | 0xdead0000, y | 0xbeef0000) == expected
            assert lib.test_spread(x | 0xface0000) == expected & 0x55555555
        for x in [0, 0xffffffff, 0xaaaaaaaa, 0x55555555] + [1 << j for j in range(32)]:
            assert lib.test_reverse(x) == int(f"{x:032b}"[::-1], 2)
        rng = np.random.default_rng(20261015)
        cases = calls = 0
        for short in (False, True):
            for rate in old.RATES:
                if short and rate == "9/10": continue
                for pilots in (False, True):
                    for ring in (False, True):
                        calls += codes.check(lib, rate, short, pilots, 0, cases % 16, ring, rng)
                        cases += 1
        for fill in range(16):
            calls += codes.check(lib, "3/5" if fill & 1 else "2/3", False, bool(fill & 1),
                                 (1, 99999, 262142)[fill % 3], fill, True, rng)
            cases += 1
        for rate in ("2/3", "3/4", "5/6"):
            for fill, start in ((0, 0), (7, 8190), (15, 8191)):
                codes.deferred(lib, rate, rng, fill, start)
        for code, rate in trial.RATES.items():
            cfg, arena = codes.setup(lib, rate, False, True, 0)
            frame = old.frame_words(trial.frame_bits())
            for count in (1, 2, 3, 8, 9):
                i = (ct.c_uint32*trial.GROUPS)(); q = (ct.c_uint32*trial.GROUPS)()
                stream = codes.Codes(i=i, q=q, mask=trial.GROUPS-1)
                for _ in range(count):
                    lib.dvbs2_8psk_codes_frame(cfg, frame.ctypes.data_as(old.PTR), ct.byref(stream))
                expected = trial.reference(code, count)
                assert stream.n == expected["published_groups"] and stream.fill == expected["pending_fill"]
                assert [stream.acc_i, stream.acc_q] == expected["pending_codes"]
                assert [trial.fnv(i), trial.fnv(q)] == [expected["hash_i"][0], expected["hash_q"][0]]
    print(json.dumps(dict(result="bit-exact-emulation", configurations=22, cases=cases,
                          frames=3*cases, deferred_frames=36, slice_calls=calls,
                          trial_reference_cases=15, trial_reference_frames=69,
                          exhaustive_zip_inputs=65536, measured_target_cycles=False)))


if __name__ == "__main__": main()
