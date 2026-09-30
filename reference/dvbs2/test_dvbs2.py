"""Checks for dvbs2.py. Run: python3 reference/dvbs2/test_dvbs2.py [--record]

Independent cross-check: if crosscheck/build/bin exists (sh reference/dvbs2/crosscheck/build.sh),
the gr-dtv and leansdr transmitters run live on the same TS input and are compared stage by
stage; otherwise this model is compared with the stage digests those programs produced, recorded
in crosscheck/digests.json. --record rewrites digests.json from a live run of the two programs
(never from this model).
"""
import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent), str(HERE / "crosscheck")]
import dvbs2 as d  # noqa: E402
import iqlut  # noqa: E402   reference/iqlut.py: firmware word format, xorshift32
from dvbs2_tables import BCH_POLYS  # noqa: E402

MODES = [(s, r) for s in (False, True) for r in d.RATES if not (s and r == "9/10")]
rng = np.random.default_rng(20260929)


def rbits(n):
    return rng.integers(0, 2, n, dtype=np.uint8)


def name(short, rate):
    return f"{'short' if short else 'normal'} {rate}"


# ---------------------------------------------------------- tables and small sequences

def test_tables():
    for short, rate in MODES:
        c = d.code(rate, short)
        m = 14 if short else 16                  # every g_i of Table 6a (6b) has degree 16 (14)
        assert all(p[-1] == m for p in BCH_POLYS["short" if short else "normal"])
        r = d.bch_generator(short, c.t).bit_length() - 1
        assert r == c.nbch - c.kbch == m * c.t                          # Tables 5 vs 6
        assert c.n - c.nbch == 360 * c.q                                # Tables 5 vs 7
        assert len(c.rows) * 360 == c.nbch and c.kbch % 8 == 0          # Annex B/C
        for x in c.rows:
            assert len(set(x.tolist())) == len(x) and 0 <= x.min() and x.max() < c.n - c.nbch
    # Worked examples printed in the standard: parity addresses of information bit i_m
    examples = {
        ("1/2", 0): [54, 9318, 14392, 27561, 26909, 10219, 2534, 8597],      # Annex B, Table B.4
        ("1/2", 1): [144, 9408, 14482, 27651, 26999, 10309, 2624, 8687],
        ("1/2", 359): [32364, 9228, 14302, 27471, 26819, 10129, 2444, 8507],
        ("1/2", 360): [55, 7263, 4635, 2530, 28130, 3033, 23830, 3651],
        ("2/3", 0): [0, 10491, 16043, 506, 12826, 8065, 8226,                # 5.3.2.1, Table B.6
                     2767, 240, 18673, 9279, 10579, 20928],
        ("2/3", 1): [60, 10551, 16103, 566, 12886, 8125, 8286,
                     2827, 300, 18733, 9339, 10639, 20988]}
    for (rate, m), want in examples.items():
        c = d.code(rate)
        got = (c.rows[m // 360] + (m % 360) * c.q) % (c.n - c.nbch)
        assert sorted(got.tolist()) == sorted(want)
    entries = {(False, "1/2"): 450, (False, "2/3"): 480, (False, "3/4"): 540, (True, "1/2"): 85}
    assert all(sum(map(len, d.code(r, s).rows)) == e for (s, r), e in entries.items())  # README
    print("tables: 21 QPSK modes; deg g = Nbch - Kbch = 16t (normal) / 14t (short) (Tables 5 "
          "vs 6); nldpc - kldpc = 360 q (Table 7); kldpc/360 address rows, distinct and "
          "< nldpc - kldpc; addresses of i0, i1, i359, i360 (rate 1/2) and i0, i1 (rate 2/3) "
          "equal the worked examples of Annex B and 5.3.2.1")


def test_crc_prbs_sof():
    # CRC RevEng catalogue, CRC-8/DVB-S2: poly 0xd5, init 0, check("123456789") = 0xbc, residue 0
    assert d.crc8(np.unpackbits(np.frombuffer(b"123456789", np.uint8))) == 0xBC
    assert d.crc8(d.bbheader(d.matype1(d.GCS), 1234)) == 0          # header + its CRC-8
    p = d.bb_prbs(40000)
    assert p[:8].tolist() == [0, 0, 0, 0, 0, 0, 1, 1]                # Figure 5
    assert (p[32767:] == p[:40000 - 32767]).all()                    # period 2^15 - 1
    assert "".join(map(str, d.int_bits(d.SOF, 26))) == "01100011010010111010000010"   # 5.5.2.1
    print("crc8/prbs/sof: CRC-8 check value 0xBC (RevEng CRC-8/DVB-S2), zero residue over a "
          "BBHEADER; BB PRBS starts 00000011 (Figure 5), period 32767; SOF = 18D2E82h")


def test_pls():
    scr = d.int_bits(d.PLS_SCRAMBLE, 64)
    base = np.array([d.pls_code(mc, s, 0) ^ scr for mc in range(32) for s in (0, 1)])
    cw = np.concatenate([base, base ^ np.tile([0, 1], 32)])      # b7 = 1 complements odd bits
    for mc in (1, 4, 11):
        assert (d.pls_code(mc, 1, 1) ^ scr == cw[64 + 2 * mc + 1]).all()
    dist = (cw[:, None, :] != cw[None, :, :]).sum(axis=2)
    assert len({r.tobytes() for r in cw}) == 128 and dist[~np.eye(128, dtype=bool)].min() == 32
    print("pls: 128 distinct (64,7) PLS codewords, minimum distance 32 as stated in 5.5.2")


# ---------------------------------------------------------- FEC

def gf2_mod(bits, g):
    """Remainder of bits[0] x^(n-1) + ... + bits[n-1] divided by g (int): textbook long division."""
    gb = d.int_bits(g, g.bit_length())
    r, dg = bits.copy(), len(gb) - 1
    for i in range(len(r) - dg):
        if r[i]:
            r[i:i + dg + 1] ^= gb
    return r[len(r) - dg:]


def test_bch():
    for short, rate in MODES:
        c = d.code(rate, short)
        m = rbits(c.kbch)
        cw = d.bch_encode(m, rate, short)
        g = d.bch_generator(short, c.t)
        assert len(cw) == c.nbch and (cw[:c.kbch] == m).all()
        assert not gf2_mod(cw, g).any()
        assert (d.bch_encode_bytes(m, rate, short) == cw).all()
        cw[rng.integers(c.nbch)] ^= 1
        assert gf2_mod(cw, g).any()                                  # the check can fail
    print("bch: 21 modes, codeword = message + parity (Nbch, Kbch per Tables 5a/5b), c(x) "
          "divisible by g(x) (long division), not after one bit flip; byte-table encoder "
          "identical")


def syndrome(cw, rate, short):
    """H c over GF(2), H built from the tables: check row a joins the information bits
    accumulated into p_a (Annex B/C addresses) and the parity bits p_a and p_{a-1} (the
    accumulation p_a ^= p_{a-1})."""
    c = d.code(rate, short)
    K, M = c.nbch, c.n - c.nbch
    rows, cols = [], []
    for g, xs in enumerate(c.rows):
        rows.append(((xs[:, None] + c.q * np.arange(360)) % M).ravel())
        cols.append(np.broadcast_to(360 * g + np.arange(360), (len(xs), 360)).ravel())
    a = np.arange(M)
    rows += [a, a[1:]]
    cols += [K + a, K + a[:-1]]
    rows, cols = np.concatenate(rows), np.concatenate(cols)
    return np.bincount(rows, weights=cw[cols], minlength=M).astype(np.int64) % 2


def test_ldpc():
    for short, rate in MODES:
        c = d.code(rate, short)
        for _ in range(2):
            i = rbits(c.nbch)
            cw = d.ldpc_encode(i, rate, short)
            assert len(cw) == c.n and (cw[:c.nbch] == i).all()
            assert (d.ldpc_encode_groups(i, rate, short) == cw).all()
            assert not syndrome(cw, rate, short).any()
        cw[rng.integers(c.n)] ^= 1
        assert syndrome(cw, rate, short).any()
    print("ldpc: 21 modes x 2 random frames, H c = 0 with H built from Annex B/C (nonzero after "
          "a bit flip); bit-serial and 360-bit group encoders identical")


# ---------------------------------------------------------- mapping and framing

def plframe_complex(fec, rate, short, pilots, n):
    """PLFRAME from the standard's formulas in complex arithmetic, to compare with the bits."""
    ph = {(0, 0): 1, (1, 0): 3, (1, 1): 5, (0, 1): 7}           # Figure 9, angle in pi/4 units
    pairs = zip(fec[0::2].tolist(), fec[1::2].tolist())
    x = np.exp(1j * np.pi / 4 * np.array([ph[k] for k in pairs]))
    S = len(x) // 90
    npil = 36 * ((S - 1) // 16) if pilots else 0
    out = np.full(90 * S + npil, (1 + 1j) / np.sqrt(2))           # pilots (5.5.3)
    k = np.arange(len(x))
    out[k + (36 * (k // 1440) if pilots else 0)] = x            # a block per 16 slots = 1440 sym
    out *= np.exp(1j * np.pi / 2 * d.pl_scrambling_sequence(len(out), n))            # 5.5.4
    pls = d.pls_code(d.code(rate, short).modcod, short, pilots)
    y = 1.0 - 2.0 * np.concatenate([d.int_bits(d.SOF, 26), pls])
    hdr = np.where(np.arange(90) % 2 == 0, (1 + 1j) * y, (-1 + 1j) * y) / np.sqrt(2)  # 5.5.2
    return np.concatenate([hdr, out])


def test_constellation():
    for R in range(4):                                           # 5.5.4 table, every input
        for bi in (0, 1):
            for bq in (0, 1):
                si, sq = d.pl_scramble(np.array([bi], np.uint8), np.array([bq], np.uint8),
                                       np.array([R]))
                assert np.isclose(d.symbols(si, sq)[0], d.symbols(bi, bq) * 1j ** R)
    for short, rate, pilots, n in ((0, "1/2", 1, 0), (0, "2/3", 0, 0), (1, "1/4", 1, 7),
                                   (1, "8/9", 0, 0)):
        short, pilots = bool(short), bool(pilots)
        fec = rbits(d.code(rate, short).n)
        z = plframe_complex(fec, rate, short, pilots, n)
        assert np.abs(d.symbols(*d.plframe(fec, rate, short, pilots, n)) - z).max() < 1e-12
        assert np.allclose(np.abs(z.real), 2**-0.5) and np.allclose(np.abs(z.imag), 2**-0.5)
    print("constellation: every PLFRAME symbol (pi/2-BPSK header, data, pilots, after PL "
          "scrambling) equals the complex value from 5.4.1 and 5.5.2-5.5.4 and lies on "
          "(+-1 +-j)/sqrt2. Bits: header even k -> (y, y), odd k -> (1-y, y); pilot -> (0, 0); "
          "scrambling R=1 (1-bQ, bI), R=2 (1-bI, 1-bQ), R=3 (bQ, 1-bI)")


def test_frame_lengths():
    expect = {(False, True): 33282, (False, False): 32490, (True, True): 8370, (True, False): 8190}
    for short, rate in MODES:
        for pilots in (True, False):
            bI, bQ = d.plframe(np.zeros(d.code(rate, short).n, np.uint8), rate, short, pilots)
            assert len(bI) == len(bQ) == d.plframe_length(short, pilots) == expect[(short, pilots)]
    print("frame lengths: QPSK PLFRAME symbols normal 33282 (pilots) / 32490, short 8370 "
          "(pilots) / 8190, all rates; = 90(S+1) + 36 int((S-1)/16) (Figure 13, Table 11)")


def test_pack_words():
    bI, bQ = rbits(33282), rbits(33282)
    w = d.pack_words(bI, bQ)
    ui, uq = iqlut.unpack_bits(w)
    assert len(w) == 2081 and (ui[:33282] == bI).all() and (uq[:33282] == bQ).all()
    assert not ui[33282:].any() and not uq[33282:].any()             # padding = (0, 0)
    one = np.zeros(16, np.uint8)
    one[3] = 1
    assert d.pack_words(one, 0 * one)[0] == 1 << 3 and d.pack_words(0 * one, one)[0] == 1 << 19
    assert all(8 * d.plframe_length(s, p) % 16 == 0 for s in (0, 1) for p in (0, 1))
    print("pack_words: inverse of iqlut.unpack_bits (I bit j, Q bit 16+j); 33282 symbols -> "
          "2081 words, 14 padding symbols (0, 0); 8 frames of any QPSK format fill whole words")


def test_gcs_roundtrip():
    """Undo the transmitter step by step and parse the BBHEADER (5.1.6) of each frame."""
    rate, short = "3/4", False
    c = d.code(rate, short)
    data = rbits(c.kbch - 80 + 1000)
    bI, bQ = d.transmit(data, rate, short, pilots=True)
    L, got = d.plframe_length(short, True), []
    for f in range(2):
        fi, fq = bI[f * L + 90:(f + 1) * L], bQ[f * L + 90:(f + 1) * L]
        R = d.pl_scrambling_sequence(len(fi))
        fi, fq = d.pl_scramble(fi, fq, (4 - R) % 4)                  # inverse rotation
        keep = np.ones(len(fi), bool)
        for b in range(1, (c.n // 180 - 1) // 16 + 1):
            keep[b * 1476 - 36:b * 1476] = False                     # 16 slots + 36 pilots
        assert not fi[~keep].any() and not fq[~keep].any()           # pilots were (0, 0)
        fec = np.empty(c.n, np.uint8)
        fec[0::2], fec[1::2] = fi[keep], fq[keep]
        assert not syndrome(fec, rate, short).any()
        bbf = d.bb_scramble(fec[:c.kbch])
        h = int("".join(map(str, bbf[:80])), 2)
        assert h >> 72 == 0x70 and (h >> 64) & 0xFF == 0             # MATYPE: GCS SIS CCM, 0.35
        assert (h >> 48) & 0xFFFF == 0                               # UPL
        assert (h >> 24) & 0xFF == 0 and (h >> 8) & 0xFFFF == 0      # SYNC, SYNCD
        assert d.crc8(bbf[:80]) == 0
        dfl = (h >> 32) & 0xFFFF
        got.append(bbf[80:80 + dfl])
        assert not bbf[80 + dfl:].any()                              # padding (5.2.1)
    assert [len(g) for g in got] == [c.kbch - 80, 1000] and (np.concatenate(got) == data).all()
    print("gcs: 2 frames undone symbol -> bit: pilots, PL scrambling, H c = 0, BB scrambling; "
          "BBHEADER MATYPE-1 = 70h, UPL = SYNC = SYNCD = 0, CRC ok, DFL = 48328 then 1000, zero "
          "padding, data intact")


# ---------------------------------------------------------- independent implementations

XBIN = HERE / "crosscheck" / "build" / "bin"
DIGESTS = HERE / "crosscheck" / "digests.json"
NFRAMES, SEED = 3, 2026
# leansdr's own tables differ from the standard (crosscheck/check_tables.py): normal 1/3 splits
# rows 12/11/13 where the PDF lines break; short 2/5 and 3/4 carry wrong entry counts.
LEANSDR_TABLE_DIFFS = {"normal 1/3", "short 2/5", "short 3/4"}
GOLD = (1, 99999, 262141)       # PL scrambling codes n != 0: gr-dtv only (leansdr fixes n = 0)


def ts_input(npk):
    """TS packets: sync 0x47 + 187 bytes of iqlut.xorshift32(SEED) output (little-endian)."""
    w = iqlut.xorshift32(SEED, (npk * 187 + 3) // 4)
    pay = w.view(np.uint8)[:npk * 187].reshape(npk, 187)
    return np.concatenate([np.full((npk, 1), 0x47, np.uint8), pay], axis=1)


def digest(bits):
    return hashlib.sha256(np.packbits(np.asarray(bits, np.uint8)).tobytes()).hexdigest()[:32]


def model_stages(pk, rate, short, pilots, n=0):
    c = d.code(rate, short)
    bb = d.ts_bbframes(pk, rate, short)[:NFRAMES]
    scr = [d.bb_scramble(b) for b in bb]
    bch = [d.bch_encode(s, rate, short) for s in scr]
    fec = [d.ldpc_encode_groups(b, rate, short) for b in bch]
    pl = [np.stack(d.plframe(f, rate, short, pilots, n), axis=1).ravel() for f in fec]  # bI bQ..
    assert len(bb) == NFRAMES and len(bch[0]) == c.nbch
    return {k: np.concatenate(v) for k, v in dict(bb=bb, scr=scr, bch=bch, fec=fec, pl=pl).items()}


def run_external(prog, pk, rate, short, pilots, n=0):
    """Stage bits from grdtv_tx or leansdr_tx. PL symbols become bits by sign, after checking
    |I| = |Q| for every symbol and a constant amplitude (i.e. all on the four QPSK points)."""
    c = d.code(rate, short)
    with tempfile.TemporaryDirectory() as t:
        pk.tofile(f"{t}/in.ts")
        args = [str(XBIN / prog), rate if prog == "grdtv_tx" else str(c.modcod),
                "short" if short else "normal", str(int(pilots)), str(NFRAMES), f"{t}/in.ts", t]
        subprocess.run(args + ([str(n)] if n else []), check=True, capture_output=True)
        bb = np.fromfile(f"{t}/bb.u8", np.uint8)
        fec = np.fromfile(f"{t}/fec.u8", np.uint8).reshape(NFRAMES, c.n)
        scr = (np.fromfile(f"{t}/scr.u8", np.uint8) if prog == "grdtv_tx"
               else fec[:, :c.kbch].ravel())
        iq = np.fromfile(f"{t}/pl.f32", np.float32).reshape(-1, 2)
    a = np.abs(iq)      # leansdr keeps its data constellation as signed char: 53 vs 75/sqrt2
    assert np.allclose(a[:, 0], a[:, 1], rtol=1e-6) and np.allclose(a, a.mean(), rtol=1e-3), \
        f"{prog}: symbol off the QPSK points"
    return dict(bb=bb, scr=scr, bch=fec[:, :c.nbch].ravel(), fec=fec.ravel(),
                pl=(iq < 0).astype(np.uint8).ravel())


def test_crosscheck(record=False):
    live = (XBIN / "grdtv_tx").exists() and (XBIN / "leansdr_tx").exists()
    assert live or not record, "build the harnesses first: sh reference/dvbs2/crosscheck/build.sh"
    rec = {} if record else json.loads(DIGESTS.read_text())
    table = {}
    configs = [(s, r, p, 0) for s, r in MODES for p in (True, False)]
    configs += [(s, r, True, n) for s, r in ((False, "1/2"), (True, "2/3")) for n in GOLD]
    for short, rate, pilots, n in configs:
        c = d.code(rate, short)
        pk = ts_input(-(-NFRAMES * (c.kbch - 80) // 1504) + 2)
        cfg = f"{name(short, rate)} {'pilots' if pilots else 'nopilots'}" + (f" gold{n}" * bool(n))
        mine = model_stages(pk, rate, short, pilots, n)
        for prog, impl in (("grdtv_tx", "gr-dtv"), ("leansdr_tx", "leansdr"))[:1 if n else 2]:
            if live:
                ext = run_external(prog, pk, rate, short, pilots, n)
                same = {k: len(ext[k]) == len(v) and bool((ext[k] == v).all())
                        for k, v in mine.items()}
                dg = {k: digest(v) for k, v in ext.items()}
                if record:
                    rec.setdefault(impl, {})[cfg] = dg
                else:
                    assert rec[impl][cfg] == dg, f"{impl} {cfg}: output differs from digests.json"
            else:
                same = {k: rec[impl][cfg][k] == digest(v) for k, v in mine.items()}
            table[(impl, cfg)] = same
    if record:
        rec["_about"] = (f"sha256[:32] of np.packbits(stage bits), {NFRAMES} frames per config, "
                         f"TS input test_dvbs2.ts_input (seed {SEED}); produced by the "
                         "crosscheck/build.sh harnesses (gr-dtv aee9fd3, leansdr 84c59e1), not "
                         "by dvbs2.py")
        DIGESTS.write_text(json.dumps(rec, indent=1, sort_keys=True) + "\n")
    for (impl, cfg), same in table.items():
        if impl == "leansdr" and " ".join(cfg.split()[:2]) in LEANSDR_TABLE_DIFFS:
            assert same["bb"] and same["scr"] and same["bch"] and not same["fec"], (impl, cfg)
        else:
            assert all(same.values()), (impl, cfg, same)
    for req in ("normal 1/2 pilots", "normal 2/3 pilots"):
        assert all(table[("gr-dtv", req)].values()) and all(table[("leansdr", req)].values())
    print(f"crosscheck ({'live run' if live else 'recorded digests'}): 42 configs (21 modes x "
          f"pilots on/off, Gold n = 0), {NFRAMES} TS-mode frames each; stages BBFRAME, scrambled "
          "BBFRAME, BCH codeword, FECFRAME, PLFRAME symbols. gr-dtv identical in all 42. leansdr "
          f"identical in 36; in the other 6 ({', '.join(sorted(LEANSDR_TABLE_DIFFS))}) identical "
          "up to the BCH codeword, then its LDPC parity differs (its tables disagree with the "
          f"standard). Gold n = {GOLD} (normal 1/2, short 2/3, pilots): gr-dtv identical")


if __name__ == "__main__":
    if "--record" in sys.argv:
        test_crosscheck(record=True)
        sys.exit(0)
    for t in (test_tables, test_crc_prbs_sof, test_pls, test_bch, test_ldpc, test_constellation,
              test_frame_lengths, test_pack_words, test_gcs_roundtrip, test_crosscheck):
        t()
    if (HERE / "crosscheck" / "build" / "src").exists():
        import check_tables
        res = check_tables.compare()
        assert not any(res["gr-dtv"].values()) and not any(res["xdsopl"].values())
        bad = {name(k[0] == "short", k[1]) for k, v in res["leansdr"].items() if v}
        assert bad == LEANSDR_TABLE_DIFFS
        print("tables vs other transcriptions: all 21 LDPC tables identical to gr-dtv and "
              f"xdsopl/LDPC; leansdr differs in {', '.join(sorted(bad))}")
    print("all checks passed")
