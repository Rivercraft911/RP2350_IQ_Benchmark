"""Q1 budget arithmetic (docs/rd/q1-qpsk.md). Plain arithmetic on recorded measurements.

Every input names its record. Outputs are ESTIMATES unless they restate an input; none is a
device measurement of the new code. Run: python3 docs/rd/q1_budget.py
"""
SYS = 128e6                       # Hz, SYS128 (all inputs below were measured at SYS128)
FRAME_SYMS = 33282                # normal QPSK PLFRAME with pilots, symbols
KBCH = {'2/3': 43040, '5/6': 53840, '9/10': 58192}


def per_frame(fraction, rate_msym):
    """Core-time fraction -> cycles per PLFRAME at the given symbol rate."""
    return fraction * SYS * FRAME_SYMS / (rate_msym * 1e6)


def fraction(cycles, rate_msym):
    return cycles * rate_msym * 1e6 / (FRAME_SYMS * SYS)


# --- Measured inputs -------------------------------------------------------------------------
# results/round2/2026-10-15/q1-ptr2-910k3-p13-60s/runs.jsonl: dualptr 1 60000 {5,6} 0 10, mask 13
R2 = dict(core0_bch=0.425133, supervision=0.031241, ts_core1=0.9701, ts_build=0.2821,
          gcs_core1=0.8276, gcs_build=0.1396, frames=28851, seconds=60.0)
# results/round2/2026-10-15/q3-desc-910k3-60s/runs.jsonl: dualstream 9/10, mask 0 (ts_busy)
Q3 = dict(ts_build=0.2597, gcs_build=0.1343)
# results/dualptr/2026-10-15/a1-8msym-60s/runs.jsonl: dualptr 2 60000 5 0 (5/6 TS, mask 0)
A1_TS = dict(core0_bch=0.225746, supervision=0.031846, core1=0.4636, build=0.1171)
# results/optimizations/2026-10-15/runs.jsonl: isolated dvbs2 stages, kernel 2, pack 1, map 1
STAGE = {'2/3': dict(bb_bch=91907, ldpc=107834, map=70603),
         '5/6': dict(bb_bch=113948, ldpc=111314, map=70603)}
# results/c2-qpsk-910/2026-10-15/stage-aba/1-kernel3.json: 9/10, kernel 3
STAGE['9/10'] = dict(bb_bch=108219, ldpc=100422, map=70603)
# results/dual-dma/2026-10-15/regression-2/runs.jsonl: pvtx 2 3000 1 (the pvflight profile)
PVTX = dict(core0_shaping=0.7875, core1_encoder=0.66, bb_cyc_per_frame=49813)
# results/c4-fec-speed/2026-10-15: two-core reentrant bounded LDPC, 2/3, all options
C4_LDPC_23 = (146.6e3, 154.3e3)
# results/ldpc-banks/2026-10-15: both cores together cost +8..15 % over one core
TWO_CORE = (1.08, 1.15)


def show(title, rows):
    print(f'\n{title}')
    for name, value in rows:
        print(f'  {name:58s} {value}')


# --- 1. QPSK 2/3 TS at 8 Msym/s on dualptr ---------------------------------------------------
cap = (KBCH['2/3'] - 80) * 8e6 / FRAME_SYMS
bch23 = per_frame(A1_TS['core0_bch'], 8) * STAGE['2/3']['bb_bch'] / STAGE['5/6']['bb_bch']
fec_factor = per_frame(A1_TS['core1'] - A1_TS['build'], 8) / (STAGE['5/6']['ldpc'] + STAGE['5/6']['map'])
fec23 = (STAGE['2/3']['ldpc'] + STAGE['2/3']['map']) * fec_factor
build_per_byte = per_frame(A1_TS['build'], 8) / (KBCH['5/6'] // 8)
build23 = build_per_byte * KBCH['2/3'] // 8
# results/dualptr/2026-10-15/a1-8msym-60s/runs.jsonl: dualptr 2 60000 6 0 (5/6 GCS): ts_busy 0.0604
gcs_per_byte = per_frame(0.0604, 8) / (KBCH['5/6'] // 8)
# Fused-adapter cycles per byte from the 9/10 model in section 2b (44.3k cycles / 7274 bytes).
FUSED_PER_BYTE = ((58192 - 80) / 8 / 188 * (1090 + 43) + 500) / (58192 // 8)
show('1. 2/3 TS at 8 Msym/s (estimate, scaled from the measured 5/6 dualptr run)', [
    ('BB capacity, Mb/s', f'{cap / 1e6:.4f}'),
    ('frame budget per core, cycles', f'{per_frame(1, 8):.0f}'),
    ('core 0: BB copy+BCH, cycles/frame (5/6 live x 2/3:5/6 isolated)', f'{bch23:.0f}'),
    ('core 0 total incl. supervision, %', f'{100 * (fraction(bch23, 8) + A1_TS["supervision"]):.1f}'),
    ('core 1: live/isolated LDPC+map factor (5/6)', f'{fec_factor:.3f}'),
    ('core 1: LDPC+map, cycles/frame', f'{fec23:.0f}'),
    ('core 1: TS build, cycles/byte (5/6 live) -> 2/3 cycles/frame', f'{build_per_byte:.2f} -> {build23:.0f}'),
    ('core 1 total, %', f'{100 * fraction(fec23 + build23, 8):.1f}'),
    ('core 1 with GCS (5/6 live 4.78 cycles/byte), %',
     f'{100 * fraction(fec23 + gcs_per_byte * KBCH["2/3"] // 8, 8):.1f}'),
    ('core 1 with pvkernel 1 (fused model x1.0 / x1.3), %',
     f'{100 * fraction(fec23 + FUSED_PER_BYTE * KBCH["2/3"] // 8, 8):.1f} / '
     f'{100 * fraction(fec23 + 1.3 * FUSED_PER_BYTE * KBCH["2/3"] // 8, 8):.1f}'),
    ('pvflight today (pvtx, measured): core 0 / core 1, %',
     f'{100 * PVTX["core0_shaping"]:.1f} / {100 * PVTX["core1_encoder"]:.1f}'),
])

# --- 2. TS cost at 16 Msym/s, 9/10 -----------------------------------------------------------
rate = R2['frames'] / R2['seconds']
ts, gcs = per_frame(R2['ts_build'], 16), per_frame(R2['gcs_build'], 16)
packets = (KBCH['9/10'] - 80) / 8 / 188
fec910 = per_frame(R2['ts_core1'] - R2['ts_build'], 16)
show('2a. TS vs GCS at 16 Msym/s 9/10 (measured, round 2)', [
    ('frames/s measured (28851 in 60 s)', f'{rate:.2f}'),
    ('TS build / GCS build, cycles/frame (mask 13)', f'{ts:.0f} / {gcs:.0f}'),
    ('difference per frame / per packet / per CRC byte (187)',
     f'{ts - gcs:.0f} / {(ts - gcs) / packets:.0f} / {(ts - gcs) / packets / 187:.2f}'),
    ('same difference with mask 0 (q3 descriptors), per packet',
     f'{(per_frame(Q3["ts_build"], 16) - per_frame(Q3["gcs_build"], 16)) / packets:.0f}'),
    ('core 1 LDPC+map, cycles/frame', f'{fec910:.0f}'),
])
# Instruction model (this report; RP2350 3.7.4.9 timing rules):
# - CRC-8 inlined in pv_bbframe: 14 instructions + taken branch per 4 bytes, two load-use
#   stalls -> 18 cycles; 46 x 18 + ~30 = ~858 cycles/packet (standalone pv_crc8: 16 cycles per
#   4 bytes, no stall, ~800/packet; pvbench measured 803 and 1006 on one older image).
# - Rest of today's TS/GCS build, rough: unaligned copy ~305, XIP swap pass ~306, adapter ~60,
#   per-message ~43 cycles/packet. Today's TS model ~1572 cycles/packet = 60.7k/frame against
#   69.1k (mask 0) and 75.1k (mask 13) measured: 1.14-1.24x.
# - Fused leaf: 19 instructions/word (17 at phase 0), one load-use stall, taken branch ->
#   21 cycles (19); 47 x 21 + ~100 = ~1090 cycles/packet; per-message share ~43; ~500 cycles
#   per frame of header, boundary bytes and calls. The range below spans 1.0-1.3x.
fused = packets * (1090 + 43) + 500
today_model = packets * (858 + 305 + 306 + 60 + 43)
show('2b0. Contention factor of today\'s TS build (model vs measured)', [
    ('model, cycles/frame', f'{today_model:.0f}'),
    ('measured / model at mask 0 / mask 13',
     f'{per_frame(Q3["ts_build"], 16) / today_model:.2f} / {ts / today_model:.2f}'),
])
show('2b. Fused adapter on core 1 (pvkernel 1), estimate', [
    ('contention-free model, cycles/frame', f'{fused:.0f}'),
    ('core 1 at x1.0 / x1.3 contention, %',
     f'{100 * (fraction(fec910, 16) + fraction(fused, 16)):.1f} / '
     f'{100 * (fraction(fec910, 16) + fraction(1.3 * fused, 16)):.1f}'),
    ('saving vs measured 97.0 %, points',
     f'{100 * (R2["ts_core1"] - fraction(fec910 + 1.3 * fused, 16)):.1f} .. '
     f'{100 * (R2["ts_core1"] - fraction(fec910 + fused, 16)):.1f}'),
])
core0_now = R2['core0_bch'] + R2['supervision']
show('2c. Fused adapter on core 0 (pvkernel 3), estimate', [
    ('core 1 = LDPC+map only, %', f'{100 * fraction(fec910, 16):.1f}'),
    ('core 0 = measured 45.6 % + fused ingress x1.0 / x1.3, %',
     f'{100 * (core0_now + fraction(fused, 16)):.1f} / {100 * (core0_now + fraction(1.3 * fused, 16)):.1f}'),
])

# --- 3. Frame-parallel QPSK FEC at 16 Msym/s, 9/10, per assigned frame (two periods) --------
budget = 2 * per_frame(1, 16)
bch_y = per_frame(R2['core0_bch'], 16)                   # core 0, scratch-Y four-word leaf, live
bch_striped = bch_y + 14544                              # core 1: five-word striped leaf (C2 estimate)
ldpc_bounded = [STAGE['9/10']['ldpc'] * x / STAGE['2/3']['ldpc'] for x in C4_LDPC_23]
ldpc_core1 = STAGE['9/10']['ldpc'] * fec_factor          # private-workspace copy of today's path
map_core0 = (STAGE['9/10']['map'] * 1.1, STAGE['9/10']['map'] * 1.3)   # sliced, striped placement
supervision = 2 * per_frame(R2['supervision'], 16)
for label, ingress in (('GCS', gcs), ('TS today', ts), ('TS fused', fused * 1.15)):
    c0 = [ingress + bch_y + ldpc_bounded[k] + map_core0[k] + 2000 + supervision for k in (0, 1)]
    c1 = ingress + bch_striped + ldpc_core1 + STAGE['9/10']['map'] * fec_factor + 2000
    show(f'3. frame-parallel, {label} (estimate; budget {budget:.0f} cycles/assigned frame)', [
        ('core 0: stages, then x1.00 .. x1.15 two-core factor, %',
         f'{100 * c0[0] / budget:.1f} .. {100 * c0[1] * TWO_CORE[1] / budget:.1f}'),
        ('core 1: %', f'{100 * c1 / budget:.1f} .. {100 * c1 * TWO_CORE[1] / budget:.1f}'),
    ])
