// Runs the gr-dtv DVB-S2 transmit blocks (GNU Radio gr-dtv/lib/dvb and lib/dvbs2, pinned in build.sh)
// on a TS file without the GNU Radio runtime: each block's work function is called once on whole frames.
// Chain as in a normal gr-dtv DVB-S2 flowgraph: bbheader -> bbscrambler -> bch -> ldpc -> interleaver
// -> modulator -> physical. Dumps every stage into OUTDIR for reference/dvbs2/test_dvbs2.py.
//
// usage: grdtv_tx RATE normal|short PILOTS NFRAMES IN.ts OUTDIR [GOLDCODE]
//   bb.u8 / scr.u8 / fec.u8: one byte (0/1) per bit; pl.f32: I,Q float32 per symbol (the block's
//   2x zero-stuffing removed; the harness checks the stuffed samples are zero).
#include "dvb/dvb_bbheader_bb_impl.h"
#include "dvb/dvb_bbscrambler_bb_impl.h"
#include "dvb/dvb_bch_bb_impl.h"
#include "dvb/dvb_ldpc_bb_impl.h"
#include "dvbs2/dvbs2_interleaver_bb_impl.h"
#include "dvbs2/dvbs2_modulator_bc_impl.h"
#include "dvbs2/dvbs2_physical_cc_impl.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>

using namespace gr::dtv;

template <class T> static void dump(const std::string& path, const T* p, size_t n)
{
    FILE* f = fopen(path.c_str(), "wb");
    if (!f || fwrite(p, sizeof(T), n, f) != n) { perror(path.c_str()); exit(1); }
    fclose(f);
}

int main(int argc, char** argv)
{
    if (argc != 7 && argc != 8) {
        fprintf(stderr, "usage: grdtv_tx RATE normal|short PILOTS NFRAMES IN.ts OUTDIR [GOLDCODE]\n");
        return 2;
    }
    const std::map<std::string, dvb_code_rate_t> rates = {
        { "1/4", C1_4 }, { "1/3", C1_3 }, { "2/5", C2_5 }, { "1/2", C1_2 }, { "3/5", C3_5 }, { "2/3", C2_3 },
        { "3/4", C3_4 }, { "4/5", C4_5 }, { "5/6", C5_6 }, { "8/9", C8_9 }, { "9/10", C9_10 } };
    dvb_code_rate_t rate = rates.at(argv[1]);
    bool shortf = !strcmp(argv[2], "short");
    dvbs2_pilots_t pilots = atoi(argv[3]) ? PILOTS_ON : PILOTS_OFF;
    int nf = atoi(argv[4]);
    std::string out = argv[6];
    int gold = argc == 8 ? atoi(argv[7]) : 0;
    dvb_framesize_t fs = shortf ? FECFRAME_SHORT : FECFRAME_NORMAL;
    int nldpc = shortf ? 16200 : 64800, nsym = nldpc / 2;

    std::vector<unsigned char> ts;
    FILE* f = fopen(argv[5], "rb");
    if (!f) { perror(argv[5]); return 1; }
    for (int c; (c = fgetc(f)) != EOF;) ts.push_back((unsigned char)c);
    fclose(f);

    dvb_bbheader_bb_impl bbh(STANDARD_DVBS2, fs, rate, RO_0_35, INPUTMODE_NORMAL, INBAND_OFF, 168, 4000000);
    dvb_bbscrambler_bb_impl scr(STANDARD_DVBS2, fs, rate);
    dvb_bch_bb_impl bch(STANDARD_DVBS2, fs, rate);
    dvb_ldpc_bb_impl ldpc(STANDARD_DVBS2, fs, rate, MOD_QPSK);
    dvbs2_interleaver_bb_impl il(fs, rate, MOD_QPSK);
    dvbs2_modulator_bc_impl mod(fs, rate, MOD_QPSK, INTERPOLATION_OFF);
    dvbs2_physical_cc_impl phy(fs, rate, MOD_QPSK, pilots, gold);

    gr_vector_int req(1);   // frame sizes from the blocks' own forecast()
    ldpc.forecast(nldpc, req);
    int nbch = req[0];
    bch.forecast(nbch, req);
    int kbch = req[0];
    int slots = nsym / 90, plen = 90 * (slots + 1) + (pilots ? 36 * ((slots - 1) / 16) : 0);

    std::vector<unsigned char> bb(nf * kbch), sc(nf * kbch), bc(nf * nbch), fec(nf * nldpc), idx(nf * nsym);
    std::vector<gr_complex> xs(nf * nsym), pl(2 * nf * plen);
    gr_vector_int nin(1);
    auto run = [&](gr::block& b, int nout, const void* in, void* o) {
        gr_vector_const_void_star i{ in };
        gr_vector_void_star v{ o };
        return b.general_work(nout, nin, i, v);
    };
    run(bbh, nf * kbch, ts.data(), bb.data());
    if (bbh.consumed > (int)ts.size() || bbh.d_logger->warnings) { fprintf(stderr, "bad TS input\n"); return 1; }
    {
        gr_vector_const_void_star i{ bb.data() };
        gr_vector_void_star v{ sc.data() };
        scr.work(nf * kbch, i, v);
    }
    run(bch, nf * nbch, sc.data(), bc.data());
    run(ldpc, nf * nldpc, bc.data(), fec.data());
    run(il, nf * nsym, fec.data(), idx.data());
    run(mod, nf * nsym, idx.data(), xs.data());
    run(phy, 2 * nf * plen, xs.data(), pl.data());

    std::vector<float> iq(2 * nf * plen);
    for (int k = 0; k < nf * plen; k++) {
        if (pl[2 * k + 1] != gr_complex(0, 0)) { fprintf(stderr, "stuffing sample not zero\n"); return 1; }
        iq[2 * k] = pl[2 * k].real();
        iq[2 * k + 1] = pl[2 * k].imag();
    }
    dump(out + "/bb.u8", bb.data(), bb.size());
    dump(out + "/scr.u8", sc.data(), sc.size());
    dump(out + "/fec.u8", fec.data(), fec.size());
    dump(out + "/pl.f32", iq.data(), iq.size());
    printf("kbch %d nbch %d plframe %d frames %d ts_bytes_used %d\n", kbch, nbch, plen, nf, bbh.consumed);
    return 0;
}
