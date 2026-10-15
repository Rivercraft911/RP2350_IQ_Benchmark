"""Finite independent GCS/FEC/APSK/PL/FIR validation for the live digital trial.

The runner never builds or flashes firmware. The Pico supplies its own on-chip
PV-SPI source; the host reconstructs only a finite capture window. No AFE/RF or
external-payload qualification is implied.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import functools
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import zlib

import numpy as np

import dual_spi
import iqbench as iq
import iq16out_trial as ref16
import iq32out_trial as ref32
from dual_trial import parse_capture

RATE = '5/6'
BB_BITS, DATA_BYTES = 53840, 6720
CAP_WORDS, BLOCK_SYMBOLS, OUT_BLOCKS = 4096, 1024, 4
INPUT_WORDS, PREFILL_FRAMES = 8192, 1
SEARCH_SYMBOLS, BUS_PER_SYMBOL = OUT_BLOCKS*BLOCK_SYMBOLS+32, 4
FRAME_SYMBOLS = {16: 16686, 32: 13338}
SYMBOLS_PER_WORD = {16: 4, 32: 2}
RETAINED_WORDS = {16: 2, 32: 4}
MODCOD = {16: 21, 32: 26}
AMPLITUDE_POLICY = 'outer_radius_one'
STANDARD = 'https://www.etsi.org/deliver/etsi_en/302300_302399/30230701/01.04.01_60/en_30230701v010401p.pdf'


@functools.lru_cache(maxsize=2)
def constellation(modulation):
    """ETSI figures11/12 in binary-label order, with the admitted outer radius1."""
    if modulation == 16:
        return ref16.constellation(270)
    if modulation != 32:
        raise ValueError('modulation must be16 or32')
    points = np.empty(32, np.complex128)
    middle = {0: 3, 1: 5, 2: -3, 3: -5, 4: 9, 5: 7, 6: -9, 7: -7,
              16: 1, 18: -1, 20: 11, 22: -11}
    inner = {17: 3, 19: -3, 21: 9, 23: -9}
    outer = dict(zip((8,9,10,11,12,13,14,15,24,25,26,27,28,29,30,31),
                     (1,3,-2,-4,6,4,-7,-5,0,2,-1,-3,7,5,8,-6)))
    for labels, radius, step in ((middle, 2.64/4.64, np.pi/12),
                                  (inner, 1/4.64, np.pi/12), (outer, 1., np.pi/8)):
        for label, angle in labels.items():
            points[label] = radius*np.exp(1j*step*angle)
    points.flags.writeable = False
    return points


def scalar_frame(fec, modulation):
    """Bit interleaving, absolute mapping, pilots, Gold0 and the real90-symbol header."""
    width = 4 if modulation == 16 else 5 if modulation == 32 else 0
    if not width or len(fec) != 64800:
        raise ValueError('normal16/32APSK FEC frame required')
    columns = np.asarray(fec, np.uint8).reshape(width, -1)
    labels = sum(columns[i].astype(np.uint32) << (width-1-i) for i in range(width))
    data = constellation(modulation)[labels]
    chunks = []
    for start in range(0, len(data), 1440):
        chunks.append(data[start:start+1440])
        if start+1440 < len(data):
            chunks.append(np.full(36, (1+1j)/np.sqrt(2)))
    body = np.concatenate(chunks)
    body *= 1j**dual_spi.ref.pl_scrambling_sequence(len(body), 0)
    hi, hq = dual_spi.ref.plheader(MODCOD[modulation], False, True)
    result = np.concatenate([dual_spi.ref.symbols(hi, hq), body])
    assert len(result) == FRAME_SYMBOLS[modulation]
    return result


@functools.lru_cache(maxsize=24)
def fec_bits(index):
    bb = dual_spi.bbframe(index, gcs=True, rate=RATE)
    result = dual_spi.ref.fecframe(bb, RATE, False)
    result.flags.writeable = False
    return result


@functools.lru_cache(maxsize=24)
def frame_points(index, modulation):
    """Random access to actual GCS BBFRAME → scalar BCH/LDPC → complete PLFRAME."""
    if type(index) is not int or index < 0:
        raise ValueError('nonnegative frame index required')
    result = scalar_frame(fec_bits(index), modulation)
    result.flags.writeable = False
    return result


def native_words(points, modulation):
    """Match exact coordinates to an independently defined mathematical alphabet."""
    alphabet = ref16.levels(270)[0] if modulation == 16 else ref32.levels(2)[0]
    codes = [np.argmin(abs(axis[:, None]-alphabet), axis=1).astype(np.uint32)
             for axis in (points.real, points.imag)]
    for axis, code in zip((points.real, points.imag), codes):
        if np.max(abs(axis-alphabet[code]), initial=0) > 2e-14:
            raise ValueError('PL coordinates lie outside the native shaper alphabet')
    count, width = SYMBOLS_PER_WORD[modulation], 3 if modulation == 16 else 5
    take = len(points)//count*count
    shifts = width*np.arange(count, dtype=np.uint32)
    packed = [(axis[:take].reshape(-1, count) << shifts).sum(axis=1, dtype=np.uint32)
              for axis in codes]
    return (packed[0] | packed[1] << 16).astype('<u4')


@functools.lru_cache(maxsize=2)
def prefill_reference(modulation):
    points = frame_points(0, modulation)
    words = native_words(points, modulation)
    return dict(prefill_frames=1, prefill_words=len(words),
                prefill_fill=len(points) % SYMBOLS_PER_WORD[modulation],
                prefill_crc=zlib.crc32(words.tobytes()))


def shaped_bus(points, modulation):
    """Exact native partial-sum quantization, continuously grouped across frames."""
    words = native_words(points, modulation)
    if modulation == 16:
        q30 = ref16.levels(270)[1]
        scaled = (q30[:, None]*ref16.COEFFICIENTS[None, :]+(1 << 29)) >> 30
        axes, _ = ref16.direct_output(words, None, scaled)
        i, q = (np.asarray(axis, np.int64) & 0x3fff for axis in axes)
    else:
        floating, q30 = ref32.levels(2)
        scaled = (q30[:, None]*ref32.COEFFICIENTS+(1 << 29)) >> 30
        raw, _, _ = ref32.expected(words, None, scaled, floating)
        values = raw.view('<i2').reshape(-1, 4).astype(np.int64)
        i, q = values[:, :2].ravel() & 0x3fff, values[:, 2:].ravel() & 0x3fff
    return np.stack([i | 0x4000, q], axis=1).ravel().astype('<u2')


def reference_window(cursor, modulation, search=SEARCH_SYMBOLS):
    """Finite real frames; one earlier frame supplies history, never periodic replay."""
    if type(cursor) is not int or cursor <= 0 or search != SEARCH_SYMBOLS:
        raise ValueError('invalid finite capture cursor/search')
    frame, group = FRAME_SYMBOLS[modulation], SYMBOLS_PER_WORD[modulation]
    lower = max(0, cursor-search)
    first = max(0, lower//frame-1)
    #16APSK frame length is2mod4: preserve global native344 rounding groups.
    while (first*frame) % group:
        first -= 1
    last = (cursor+2*CAP_WORDS//BUS_PER_SYMBOL+group+frame-1)//frame
    points = np.concatenate([frame_points(index, modulation) for index in range(first, last)])
    bus = shaped_bus(points, modulation)
    base = first*frame*BUS_PER_SYMBOL
    lo, hi = lower*BUS_PER_SYMBOL-base, cursor*BUS_PER_SYMBOL-base
    assert 0 <= lo < hi and hi+2*CAP_WORDS <= len(bus)
    assert first == 0 or lo >= 8*BUS_PER_SYMBOL
    return first, bus, lo, hi


def validate_capture(result, lines, *, window=reference_window):
    try:
        if result.get('cap_error'):
            raise ValueError(str(result['cap_error']))
        got = parse_capture(lines)
        if len(got) != CAP_WORDS or result.get('cap_words') != CAP_WORDS:
            raise ValueError('capture is incomplete')
        if zlib.crc32(got.tobytes()) != result.get('cap_crc') or result.get('cap_stride') != 1:
            raise ValueError('capture transport CRC/stride mismatch')
        modulation = result.get('modulation')
        if modulation not in (16, 32):
            raise ValueError('invalid modulation')
        first, expected, lo, hi = window(result['cap_symbol_cursor'], modulation,
                                         result['cap_search_symbols'])
    except (KeyError, ValueError, TypeError) as exc:
        return dict(cap_transport_ok=False, cap_aligned=False, cap_mismatches=None,
                    cap_checked_bus_words=0, cap_error=str(exc))
    bus = got.view('<u2')
    candidates = np.flatnonzero(expected[lo:hi] == bus[0])+lo
    for j in range(1, 32):
        candidates = candidates[expected[candidates+j] == bus[j]]
        if not len(candidates):
            break
    if not len(candidates):
        return dict(cap_transport_ok=True, cap_aligned=False, cap_mismatches=None,
                    cap_checked_bus_words=0, cap_reference_first_frame=first,
                    cap_error='no bounded finite offset matches first32 observations')
    offset, bad = min(((int(o), np.flatnonzero(expected[o:o+len(bus)] != bus)) for o in candidates),
                      key=lambda item: len(item[1]))
    flags = ((bus >> 14) & 1).astype(np.uint8)
    physical = np.array_equal(flags, int(flags[0]) ^ (np.arange(len(bus)) & 1)) and not np.any(bus & 0x8000)
    global_start = first*FRAME_SYMBOLS[modulation]*4+offset
    boundary = result.get('cap_frame_boundary')
    covered = (type(boundary) is int and boundary > 0 and boundary % FRAME_SYMBOLS[modulation] == 0 and
               global_start <= 4*boundary and global_start+len(bus) >= 4*(boundary+90))
    return dict(cap_transport_ok=True, cap_aligned=bool(physical), cap_physical_iq_ok=bool(physical),
                cap_mismatches=int(len(bad)), cap_first_bad=int(bad[0]) if len(bad) else None,
                cap_checked_bus_words=len(bus), cap_global_bus_offset=global_start,
                cap_header_covered=bool(covered),
                cap_reference_first_frame=first, cap_start_search_bus_words=hi-lo)


def capture_lines(bus):
    words = np.asarray(bus, dtype='<u2').view('<u4')
    lines = ['@cap '+str(k)+' '+' '.join(f'{int(w):08x}' for w in words[k:k+16])
             for k in range(0, len(words), 16)]
    return dict(cap_words=len(words), cap_crc=zlib.crc32(words.tobytes()), cap_stride=1), lines


@functools.lru_cache(maxsize=1)
def first_fec_crcs():
    return [zlib.crc32(np.packbits(fec_bits(index)).view('>u4').astype('<u4').tobytes())
            for index in range(4)]


def expected_profile(modulation, clock_khz, cpw, ms):
    profile = ref16 if modulation == 16 else ref32
    return dict(cmd='benchapskfec', modulation=modulation, clk_hz=clock_khz*1000, cpw=cpw, ms=ms,
                code_index=8, s2_code='normal 5/6', modcod=MODCOD[modulation], kbch=BB_BITS,
                pilots=True, gold=0, bb_matype1=114, outer_radius=1, header_pilot_radius=1,
                gamma100=270 if modulation == 16 else 0, gamma1_100=264 if modulation == 32 else 0,
                gamma2_100=464 if modulation == 32 else 0, L=8, sps=2,
                row_profile=profile.PROFILE, pulse_profile=profile.PULSE,
                coefficient_sha256=profile.COEFFICIENT_SHA,
                table_bytes=139264 if modulation == 16 else 117912, table_crc=profile.TABLE_CRCS,
                frame_symbols=FRAME_SYMBOLS[modulation], native_words=INPUT_WORDS,
                symbols_per_word=SYMBOLS_PER_WORD[modulation],
                words_per_block=BLOCK_SYMBOLS//SYMBOLS_PER_WORD[modulation],
                retained_words=RETAINED_WORDS[modulation], publication_words=64,
                block_symbols=BLOCK_SYMBOLS, out_blocks=OUT_BLOCKS, out_words=2*BLOCK_SYMBOLS,
                bch_kernel=2, idle_wait='timer_poll_1us', **prefill_reference(modulation))


def configuration_faults(result, modulation, clock, cpw, ms, expected_git=None):
    if result.get('error'):
        return [f"firmware error: {result['error']}"]
    expected = expected_profile(modulation, clock, cpw, ms)
    if expected_git is not None:
        expected['git'] = expected_git
    faults = [f'{key}={result.get(key)!r}, expected {want!r}'
              for key, want in expected.items() if result.get(key) != want]
    if not isinstance(result.get('git'), str) or not result['git']:
        faults.append('missing firmware identity')
    rs = clock*1000/(4*cpw)
    for key, want in (('sym_rate', rs), ('bb_payload_bps', rs*(BB_BITS-80)/FRAME_SYMBOLS[modulation])):
        if type(result.get(key)) not in (int, float) or not math.isclose(result[key], want, abs_tol=.11):
            faults.append(f'{key} differs from configured mode')
    return faults


def stream_faults(result, modulation, clock, cpw, ms, expected_git=None):
    r = result
    faults = configuration_faults(r, modulation, clock, cpw, ms, expected_git)
    if r.get('error'):
        return faults
    for key in ('errors', 'underruns', 'own_errors', 'idle_sent', 'txstalls'):
        if type(r.get(key)) is not int or r[key] != 0:
            faults.append(key)
    counts = ('elapsed_us', 'cleanup_us', 'blocks', 'generated_blocks', 'done_blocks',
              'input_published_words', 'input_consumed_words', 'stream_words', 'stream_fill',
              'mapped_symbols', 'min_input_words', 'input_waits', 'encoder_frames',
              'encoder_runtime_frames', 'encoder_attempted_frames', 'encoder_elapsed_us',
              'encoder_credit_wait_cycles', 'encoder_max_frame_work_cyc', 'cap_symbol_cursor',
              'cap_input_words', 'cap_done_blocks', 'cap_frame_boundary', 'prepare_cycles', 'shape_cycles', 'producer_work_cycles')
    invalid = [key for key in counts if type(r.get(key)) is not int or r[key] < 0]
    if invalid:
        return list(dict.fromkeys(faults+['invalid runtime counters: '+', '.join(invalid)]))
    hz, rs, spw = clock*1000, clock*1000/(4*cpw), SYMBOLS_PER_WORD[modulation]
    elapsed, blocks = r['elapsed_us'], r['blocks']
    frames, attempts = r['encoder_frames'], r['encoder_attempted_frames']
    if not ms*1000 <= elapsed <= ms*1000+50000 or not blocks:
        faults.append('duration or output block count invalid')
    if abs(blocks*BLOCK_SYMBOLS-rs*elapsed/1e6) > 8*BLOCK_SYMBOLS:
        faults.append('output block count differs from elapsed clock rate')
    if r['generated_blocks'] != blocks+OUT_BLOCKS or not 0 <= r['generated_blocks']-r['done_blocks'] <= OUT_BLOCKS:
        faults.append('output block publication/credit mismatch')
    if frames <= 1 or r['encoder_runtime_frames'] != frames-1 or attempts-frames not in (0, 1):
        faults.append('encoder completed/attempted frame accounting mismatch')
    if type(r.get('encoder_partial_frame')) is not bool or r['encoder_partial_frame'] != (attempts != frames):
        faults.append('partial-frame marker mismatch')
    mapped = r['stream_words']*spw+r['stream_fill']
    if (r['mapped_symbols'] != mapped or not 0 <= r['stream_fill'] < spw or
            not frames*FRAME_SYMBOLS[modulation] <= mapped <= attempts*FRAME_SYMBOLS[modulation]):
        faults.append('continuous partial-frame symbol accounting mismatch')
    if r['input_published_words'] != r['stream_words']:
        faults.append('final native word publication differs from mapper cursor')
    consumed = (blocks+OUT_BLOCKS)*(BLOCK_SYMBOLS//spw)-RETAINED_WORDS[modulation]
    if r['input_consumed_words'] != consumed or not RETAINED_WORDS[modulation] <= r['input_published_words']-consumed <= INPUT_WORDS:
        faults.append('native input ring credit/history mismatch')
    if not 0 <= r['min_input_words'] <= INPUT_WORDS:
        faults.append('input lead diagnostic outside capacity')
    cursor = r['cap_symbol_cursor']
    if cursor % BLOCK_SYMBOLS or not OUT_BLOCKS*BLOCK_SYMBOLS <= cursor <= (blocks+OUT_BLOCKS)*BLOCK_SYMBOLS:
        faults.append('capture cursor differs from generated block boundary')
    frame = FRAME_SYMBOLS[modulation]
    denominator = 8000*cpw*frame
    boundary = ((hz*ms+denominator-1)//denominator)*frame
    if r['cap_frame_boundary'] != boundary:
        faults.append('capture boundary differs from planned midpoint frame')
    if not boundary-BLOCK_SYMBOLS <= cursor <= boundary+OUT_BLOCKS*BLOCK_SYMBOLS:
        faults.append('capture cursor differs from planned frame boundary')
    if not 0 <= cursor//BLOCK_SYMBOLS-r['cap_done_blocks'] <= OUT_BLOCKS:
        faults.append('capture output credit/cursor mismatch')
    if not cursor//spw <= r['cap_input_words'] <= cursor//spw+INPUT_WORDS-RETAINED_WORDS[modulation]:
        faults.append('capture input publication outside capacity')
    budget = elapsed*hz/1e6
    if r['producer_work_cycles'] < r['prepare_cycles']+r['shape_cycles']:
        faults.append('producer work omits measured stages')
    for cycles, busy in (('shape_cycles', 'gen_busy'), ('producer_work_cycles', 'producer_work_busy')):
        if (type(r.get(busy)) not in (int, float) or not math.isfinite(r[busy]) or not 0 <= r[busy] <= 1.001 or
                not budget or not math.isclose(r[cycles]/budget, r[busy], abs_tol=2e-6)):
            faults.append(f'{busy} cycle budget mismatch')
    for stage, cost, worst in (('prepare_cycles', 'prepare_cyc_per_sym', 'worst_prepare_cyc_per_sym'),
                                ('shape_cycles', 'gen_cyc_per_sym', 'worst_shape_cyc_per_sym')):
        if (type(r.get(cost)) not in (int, float) or not math.isfinite(r[cost]) or not blocks or
                not math.isclose(r[stage]/(blocks*BLOCK_SYMBOLS), r[cost], abs_tol=.00011) or
                type(r.get(worst)) not in (int, float) or not math.isfinite(r[worst]) or r[worst]+.0002 < r[cost]):
            faults.append(f'{stage} average/worst accounting mismatch')
    if modulation == 16 and r['prepare_cycles'] != 0:
        faults.append('16APSK unexpectedly charged coordinate preparation')
    stage, maxima = r.get('encoder_stage_cycles'), r.get('encoder_stage_max_cyc')
    names = {'bb', 'bb_bch', 'ldpc', 'map'}
    valid = (isinstance(stage, dict) and set(stage) == names and
             all(type(value) is int and value > 0 for value in stage.values()))
    valid_max = (isinstance(maxima, dict) and set(maxima) == names and
                 all(type(value) is int and value > 0 for value in maxima.values()))
    if not valid or not valid_max:
        faults.append('encoder stage cycle accounting missing/invalid')
    else:
        if any(stage[key] > maxima[key]*(attempts-1) for key in names):
            faults.append('encoder stage totals exceed attempted-frame maxima')
        enc_budget = r['encoder_elapsed_us']*hz/1e6
        if (not enc_budget or type(r.get('encoder_busy')) not in (int, float) or
                not math.isfinite(r['encoder_busy']) or not 0 < r['encoder_busy'] <= 1.001 or
                not math.isclose(sum(stage.values())/enc_budget, r['encoder_busy'], abs_tol=2e-6)):
            faults.append('encoder work/budget fraction mismatch')
        if sum(stage.values())+r['encoder_credit_wait_cycles'] > enc_budget*1.005:
            faults.append('encoder work plus credit wait exceeds elapsed budget')
    spi = r.get('pv_spi')
    if not isinstance(spi, dict):
        faults.append('missing PV-SPI metadata')
    else:
        if spi.get('bbframes') != attempts or spi.get('runtime_bbframes') != attempts-1:
            faults.append('GCS construction differs from attempted encoder frames')
        if spi.get('sck_hz') != (32000000 if clock == 128000 else 25000000):
            faults.append('SPI clock differs from configured profile')
        try:
            faults.extend(dual_spi.packet_faults(dict(r, source=6)))
        except (KeyError, ValueError, TypeError, OverflowError) as exc:
            faults.append(f'invalid SPI accounting: {type(exc).__name__}')
    if r.get('fe_crc') != first_fec_crcs():
        faults.append('FEC frames differ from independent scalar BCH/LDPC')
    if not (r.get('cap_transport_ok') and r.get('cap_aligned') and r.get('cap_header_covered') and r.get('cap_mismatches') == 0 and
            r.get('cap_checked_bus_words') == 2*CAP_WORDS):
        faults.append(r.get('cap_error') or 'complete finite capture mismatch')
    return list(dict.fromkeys(faults))
