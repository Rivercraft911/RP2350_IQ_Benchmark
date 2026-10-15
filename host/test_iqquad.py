"""Independent checks for the L9 and opt-in convex L8 four-symbol-row trials.

Builds the actual C table/descriptor functions in a temporary directory. Checks every
sample against direct integer FIR, chronological input/word boundaries, complete DVB-S2
PLFRAME streams, interpolator feedback and both actual PIO instruction schedules.
Native execution does not validate the Arm leaves, DMA throughput or analog RF quality.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import zlib
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "reference"))
sys.path.insert(0, str(ROOT / "reference/dvbs2"))
import iqlut as m
import dvbs2 as s2
from pulse_profiles import L8_COEFFICIENTS

L, SPS, ROWS, ROW_WORDS = 9, 4, 4096, 8
U32 = 0xffffffff
BASE_I, BASE_Q = 0x20040000, 0x20040000
CANARY = 0x9ad4c781
PTR = ctypes.POINTER(ctypes.c_uint32)


class ProfileInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint) for name in ("span", "history_bits", "rows", "words")]
    _fields_ += [("table_crc", ctypes.c_uint32), ("id", ctypes.c_char_p)]


def fir_rows(lut):
    """Direct all-key FIR sums, independent of builder's history-table composition."""
    span, sps = lut.L, lut.sps
    keys = np.arange(1 << (span + 3), dtype=np.int64)[:, None]
    ages = np.arange(span)[None, :]
    rows = []
    for newest in range(span - 1, span + 3):
        signs = 1 - 2 * ((keys >> (newest - ages)) & 1)
        acc = signs @ lut.coef.reshape(span, sps)
        slots = (((acc + 128) >> 8) & 0x3fff).astype("<u4")
        rows.append(slots[:, 0::2] | slots[:, 1::2] << 16)
    return np.concatenate(rows, axis=1)


def descriptors(words, prev, bases, span=L):
    """Gather chronological bits directly, including arbitrary prehistory."""
    result = []
    for axis, base in enumerate(bases):
        before = [(prev >> (16 * axis + j)) & 1 for j in range(16)]
        bits = before + [int(word >> (16 * axis + j)) & 1 for word in words for j in range(16)]
        result.append(np.asarray([
            (base + 32 * sum(bits[16 + n - (span - 1) + k] << k for k in range(span + 3))) & U32
            for n in range(0, len(words) * 16, 4)
        ], dtype="<u4"))
    return result


def feedback_model(words, prev, bases, span=L):
    def ror(x, n):
        return ((x >> n) | (x << (32 - n))) & U32
    outputs = [[], []]
    for word in words:
        cur = int(word)
        windows = [(prev & 0xffff) | ((cur << 16) & U32),
                   (prev >> 16) | (cur & 0xffff0000)]
        for axis, base in enumerate(bases):
            window = windows[axis]
            for _ in range(4):
                outputs[axis].append((base + (ror(window, 12 - span) &
                                              (((1 << (span + 3)) - 1) << 5))) & U32)
                window = ror(window, 4)
        prev = cur
    return [np.asarray(out, dtype="<u4") for out in outputs]


def native_descriptors(lib, words, prev, bases, block, span=L):
    n = len(words)
    arrays = [(ctypes.c_uint32 * (4 * n + 2))() for _ in range(2)]
    for out in arrays:
        out[0] = out[-1] = CANARY
    data = np.asarray(words, dtype=np.uint32)
    for start in range(0, n, block):
        take = min(block, n - start)
        kernel = lib.iqdesc_quad8_c if span == 8 else lib.iqdesc_quad_c
        kernel(
            ctypes.cast(ctypes.byref(arrays[0], 4 + start * 16), PTR),
            ctypes.cast(ctypes.byref(arrays[1], 4 + start * 16), PTR),
            data[start:].ctypes.data_as(PTR), take,
            int(data[start - 1]) if start else prev, *bases)
    # The zero-word contract must not touch either output.
    kernel = lib.iqdesc_quad8_c if span == 8 else lib.iqdesc_quad_c
    kernel(ctypes.cast(ctypes.byref(arrays[0], 4), PTR),
                     ctypes.cast(ctypes.byref(arrays[1], 4), PTR),
                     data.ctypes.data_as(PTR), 0, prev, *bases)
    for out in arrays:
        assert out[0] == out[-1] == CANARY, "descriptor overwrite"
    return [np.ctypeslib.as_array(out)[1:-1].copy() for out in arrays]


def resolve_waveform(desc, rows, bases):
    offsets = [((d.astype(np.uint64) - base) & U32) for d, base in zip(desc, bases)]
    assert all(np.all((o & 31) == 0) and np.all(o < rows.nbytes) for o in offsets)
    i, q = (rows[o // 32] for o in offsets)
    return np.stack([i | np.uint32(0x40004000), q], axis=-1).ravel()


def symbol_words(planes):
    n = len(planes[0])
    padded = [np.pad(p, (0, (-n) % 16)).reshape(-1, 16) for p in planes]
    shifts = np.arange(16, dtype=np.uint32)
    return ((padded[0].astype(np.uint32) << shifts).sum(axis=1, dtype=np.uint32) |
            (padded[1].astype(np.uint32) << shifts).sum(axis=1, dtype=np.uint32) << 16)


def scalar_waveform(words, previous, coefficients):
    """Causal integer FIR convolution, without any history-key or row lookup."""
    axes = []
    for axis, bits in enumerate(m.unpack_bits(words)):
        before = (previous >> (16 * axis + np.arange(16))) & 1
        signs = 1 - 2 * np.concatenate([before, bits]).astype(np.int64)
        samples = np.empty((len(bits), SPS), dtype=np.int64)
        for phase in range(SPS):
            sums = np.convolve(signs, coefficients[phase::SPS])[16:16 + len(bits)]
            samples[:, phase] = (sums + 128) >> 8
        axes.append(samples.ravel())
    return m.to_layout(m.pack(*axes), m.LAYOUT_PAIRS)


def profile_waveform(lib, words, previous, rows, span, block):
    expected = descriptors(words, previous, (BASE_I, BASE_Q), span)
    actual = native_descriptors(lib, words, previous, (BASE_I, BASE_Q), block, span)
    for axis in range(2):
        np.testing.assert_array_equal(actual[axis], expected[axis])
        np.testing.assert_array_equal(feedback_model(words, previous, (BASE_I, BASE_Q), span)[axis],
                                      expected[axis])
    return resolve_waveform(actual, rows, (BASE_I, BASE_Q))


def l8_profile_check(lib):
    infos = [lib.iqquad_profile_info(profile).contents for profile in (0, 1)]
    assert [(p.span, p.history_bits, p.rows, p.words, p.table_crc, p.id) for p in infos] == [
        (9, 12, 4096, 32768, 0xd0cb695f, b"qpsk_l9_n4_kaiser_b080"),
        (8, 11, 2048, 16384, 0x2581bd84, b"qpsk_l8_n4_both_m010_far2"),
    ]
    assert hashlib.sha256(L8_COEFFICIENTS.astype("<i4").tobytes()).hexdigest() == \
        "28a65e7881d8649dcae694866b798491a57b36d4c43528b58fe44a66b32a1dcd"
    guarded = (ctypes.c_uint32 * (ROWS * ROW_WORDS + 2))(*([CANARY] * (ROWS * ROW_WORDS + 2)))
    out = ctypes.cast(ctypes.byref(guarded, 4), PTR)
    for bad in (-1, 2, 0x7fffffff):
        assert not lib.iqquad_profile_info(bad)
        assert not lib.iqquad_build_profile(out, ROWS * ROW_WORDS, bad)
    for profile, info in enumerate(infos):
        assert not lib.iqquad_build_profile(out, info.words - 1, profile)
        assert not lib.iqquad_build_profile(None, info.words, profile)
    assert all(value == CANARY for value in guarded)
    info = infos[1]
    assert lib.iqquad_build_profile(out, info.words, 1)
    assert guarded[0] == CANARY and all(value == CANARY for value in guarded[info.words + 1:])
    rows = np.ctypeslib.as_array(guarded)[1:info.words + 1].copy().reshape(info.rows, ROW_WORDS)
    pulse = SimpleNamespace(L=8, sps=SPS, coef=L8_COEFFICIENTS)
    np.testing.assert_array_equal(rows, fir_rows(pulse))
    assert zlib.crc32(rows.astype("<u4").tobytes()) == info.table_crc
    assert rows.nbytes == 65536 and not np.any(rows & np.uint32(0xc000c000))
    slots = np.stack([rows & 0x3fff, (rows >> 16) & 0x3fff], axis=-1)
    signed = ((slots.astype(np.int32) ^ 0x2000) - 0x2000)
    assert signed.min() == -7300 and signed.max() == 7300
    cases = 0
    for seed in (0, 1, 0x1234abcd, U32):
        words = m.xorshift32(seed, 137)
        for previous in (0, 0x716bc429, U32):
            expected = scalar_waveform(words, previous, L8_COEFFICIENTS)
            for block in (1, 3, 7, 64, 137):
                np.testing.assert_array_equal(profile_waveform(lib, words, previous, rows, 8, block), expected)
                cases += 1
    # Consecutive normal/short frames with different pilot settings have partial
    # raw words at the PLFRAME boundaries; history and row assembly must continue.
    planes = []
    for index, (rate, short, pilots) in enumerate((("2/3", False, True), ("1/4", True, False),
                                                 ("9/10", False, False), ("5/6", True, True))):
        c = s2.code(rate, short)
        raw = m.xorshift32(0x1234abcd ^ index, (c.n + 31) // 32)
        bits = ((raw[:, None] >> np.arange(31, -1, -1, dtype=np.uint32)) & 1).astype(np.uint8).ravel()[:c.n]
        planes.append(s2.plframe(bits, rate, short=short, pilots=pilots))
    words = symbol_words([np.concatenate([p[a] for p in planes]) for a in range(2)])
    expected = scalar_waveform(words, 0, L8_COEFFICIENTS)
    for block in (1, 7, 256, len(words)):
        np.testing.assert_array_equal(profile_waveform(lib, words, 0, rows, 8, block), expected)
    # Selecting/building another profile never changes the legacy API's output.
    lib.iqquad_build(out)
    legacy = np.ctypeslib.as_array(guarded)[1:-1].copy()
    assert zlib.crc32(legacy.astype("<u4").tobytes()) == 0xd0cb695f
    return dict(profile=info.id.decode(), rows=info.rows, bytes=rows.nbytes,
                table_crc=info.table_crc, exact_peak=7300, scalar_samples=info.rows * 16,
                stream_cases=cases, mixed_plframes=len(planes), waveform_crc=zlib.crc32(expected.tobytes()))


def full_frame_check(lib, rows, lut):
    c = s2.code("2/3")
    raw = m.xorshift32(0x9e3779b9 ^ 5, (c.kbch + 31) // 32)
    planes = []
    for frame in range(3):
        words = raw.copy()
        if frame & 1:
            words[::64] ^= np.uint32(0x80000000)
        bits = ((words[:, None] >> np.arange(31, -1, -1, dtype=np.uint32)) & 1).astype(np.uint8).ravel()[:c.kbch]
        planes.append(s2.plframe(s2.fecframe(bits, "2/3"), "2/3", pilots=True))
    words = symbol_words([np.concatenate([p[a] for p in planes]) for a in range(2)])
    got = resolve_waveform(native_descriptors(lib, words, 0, (BASE_I, BASE_Q), 7), rows,
                           (BASE_I, BASE_Q))
    expected = m.to_layout(m.generate(words, lut), m.LAYOUT_PAIRS)
    np.testing.assert_array_equal(got, expected)
    return dict(frames=3, pilots=True, symbols=3 * s2.plframe_length(False, True),
                padded_symbols=len(words) * 16, waveform_crc=zlib.crc32(got.tobytes()))


def program(name):
    source = (ROOT / "firmware/src/dual_dma.c").read_text()
    match = re.search(r"\b" + name + r"\[\]\s*=\s*\{([^}]+)\}", source)
    assert match, name
    body = re.sub(r"//[^\n]*", "", match[1])
    return [int(x.strip(), 0) for x in body.split(",") if x.strip()]


@dataclass
class Machine:
    code: list[int]
    stream: int
    pc: int
    x: int
    writers: int
    delay: int = 0
    samples: int = 0

    def step(self):
        if self.delay:
            self.delay -= 1
            return None, None
        insn = self.code[self.pc]
        flag = (insn >> 11) & 1 if insn & 0x1000 else None
        self.delay = (insn >> 8) & 7
        nxt = (self.pc + 1) % len(self.code)
        sample = None
        opcode = insn >> 13
        if opcode == 3:
            assert ((insn >> 5) & 7, insn & 31) == (0, 16)
            row = self.samples // 16
            if self.writers == 4:
                row = 2 * row + self.stream // 2
            sample = 16 * row + self.samples % 16
            self.samples += 1
        elif opcode == 0:
            cond, target = (insn >> 5) & 7, insn & 31
            if cond == 0:
                nxt = target
            else:
                assert cond == 2
                old = self.x
                self.x = (old - 1) & U32
                if old:
                    nxt = target
        elif opcode == 7:
            assert (insn >> 5) & 7 == 1
            self.x = insn & 31
        else:
            assert insn & 0xe0ff == 0xa042, f"unexpected MOV/NOP {insn:04x}"
        self.pc = nxt
        return sample, flag


def check_pio():
    for writers in (2, 4):
        names = ("paired_i_code", "paired_q_code") if writers == 2 else (
            "quad_four_i_code", "quad_four_q_code")
        codes = [program(name) for name in names]
        starts, xs = ((0, 1), (0, 0)) if writers == 2 else ((0, 1, 2, 1), (15, 16, 0, 0))
        machines = [Machine(codes[s & 1], s, starts[s], xs[s], writers) for s in range(writers)]
        for tick in range(65536):
            writes, flags = [], []
            for machine in machines:
                sample, flag = machine.step()
                if sample is not None:
                    writes.append((machine.stream, sample))
                if flag is not None:
                    flags.append((machine.stream, flag))
            owner = (tick & 1) + (2 * ((tick // 32) & 1) if writers == 4 else 0)
            assert writes == [(owner, tick // 2)], (writers, tick, writes)
            assert flags == [(owner, 1 - (tick & 1))], (writers, tick, flags)


def main():
    lut = m.Lut(.20, SPS, L, .8, 1.0)
    with tempfile.TemporaryDirectory(prefix="iqquad-native-") as build:
        library = Path(build) / "iqquad.so"
        src = ROOT / "firmware/src"
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                        f"-I{src}", str(src / "iqquad.c"), str(src / "iqdesc.c"), "-o", str(library)], check=True)
        lib = ctypes.CDLL(str(library))
        lib.iqquad_build.argtypes = [PTR]
        lib.iqquad_profile_info.argtypes = [ctypes.c_int]
        lib.iqquad_profile_info.restype = ctypes.POINTER(ProfileInfo)
        lib.iqquad_build_profile.argtypes = [PTR, ctypes.c_uint, ctypes.c_int]
        lib.iqquad_build_profile.restype = ctypes.c_bool
        lib.iqdesc_quad_c.argtypes = [PTR, PTR, PTR] + [ctypes.c_uint32] * 4
        lib.iqdesc_quad8_c.argtypes = lib.iqdesc_quad_c.argtypes
        table = (ctypes.c_uint32 * (ROWS * ROW_WORDS + 2))()
        table[0] = table[-1] = CANARY
        lib.iqquad_build(ctypes.cast(ctypes.byref(table, 4), PTR))
        assert table[0] == table[-1] == CANARY, "table overwrite"
        rows = np.ctypeslib.as_array(table)[1:-1].copy().reshape(ROWS, ROW_WORDS)
        np.testing.assert_array_equal(rows, fir_rows(lut))
        assert not np.any(rows & np.uint32(0xc000c000)), "table contains flag/spare bits"
        cases = 0
        for seed in (0, 1, 0x1234abcd, U32):
            words = m.xorshift32(seed, 137)
            for prev in (0, 0x716bc429):
                for bases in ((BASE_I, BASE_Q), (0xfffffff0, 0x10)):
                    expected = descriptors(words, prev, bases)
                    for got in (feedback_model(words, prev, bases), *(
                            native_descriptors(lib, words, prev, bases, b) for b in (1, 7, 64, 137))):
                        for a in range(2):
                            np.testing.assert_array_equal(got[a], expected[a])
                        cases += 1
            got = resolve_waveform(native_descriptors(lib, words, 0, (BASE_I, BASE_Q), 7), rows,
                                   (BASE_I, BASE_Q))
            np.testing.assert_array_equal(got, m.to_layout(m.generate(words, lut), m.LAYOUT_PAIRS))
        frame_result = full_frame_check(lib, rows, lut)
        l8_result = l8_profile_check(lib)
    check_pio()
    print(json.dumps(dict(profile="l9_quad", L=L, filter_beta=.8, rows=ROWS,
                          bytes=rows.nbytes, table_crc=zlib.crc32(rows.tobytes()),
                          descriptor_cases=cases, pio_slots_per_mode=65536,
                          full_frame=frame_result, l8_profile=l8_result)))


if __name__ == "__main__":
    main()
