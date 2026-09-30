// Runs the leansdr DVB-S2 transmit chain (pabr/leansdr, work branch, pinned in build.sh) on a TS file:
// s2_framer -> s2_fecenc -> s2_interleaver -> s2_frame_transmitter, as leandvbtx --standard DVB-S2 wires
// it, with extra readers on the intermediate pipes. Dumps each stage into OUTDIR for test_dvbs2.py.
//
// usage: leansdr_tx MODCOD normal|short PILOTS NFRAMES IN.ts OUTDIR
//   bb.u8 (unscrambled BBFRAME) / fec.u8 (FECFRAME; its first Kbch bits are the scrambled BBFRAME):
//   one byte (0/1) per bit; pl.f32: I,Q float32 per symbol.
#include "leansdr/framework.h"
#include "leansdr/generic.h"
#include "leansdr/dvbs2.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

using namespace leansdr;

static void dump_bits(FILE* f, const uint8_t* bytes, int nbits)
{
    for (int k = 0; k < nbits; k++) fputc((bytes[k / 8] >> (7 - k % 8)) & 1, f);
}

int main(int argc, char** argv)
{
    if (argc != 7) { fprintf(stderr, "usage: leansdr_tx MODCOD normal|short PILOTS NFRAMES IN.ts OUTDIR\n"); return 2; }
    s2_pls pls;
    pls.modcod = atoi(argv[1]);
    pls.sf = !strcmp(argv[2], "short");
    pls.pilots = atoi(argv[3]) != 0;
    int nf = atoi(argv[4]);
    std::string out = argv[6];

    std::vector<tspacket> ts;
    FILE* f = fopen(argv[5], "rb");
    if (!f) { perror(argv[5]); return 1; }
    for (tspacket p; fread(p.data, 1, 188, f) == 188;) ts.push_back(p);
    fclose(f);

    scheduler sch;
    pipebuf<tspacket> p_ts(&sch, "ts", ts.size());
    pipebuf<bbframe> p_bb(&sch, "bb", nf);                // capacity nf: the framer stops after nf frames
    pipebuf<fecframe<hard_sb> > p_fec(&sch, "fec", nf);
    pipebuf<plslot<hard_ss> > p_slots(&sch, "slots", nf * (1 + modcod_info::MAX_SLOTS_PER_FRAME));
    pipebuf<cf32> p_iq(&sch, "iq", nf * modcod_info::MAX_SYMBOLS_PER_FRAME);
    s2_framer framer(&sch, p_ts, p_bb);
    framer.pls_seq = &pls;
    framer.n_pls_seq = 1;                                   // CCM
    framer.rolloff_code = 0;                                // 0.35
    s2_fecenc fecenc(&sch, p_bb, p_fec);
    s2_interleaver inter(&sch, p_fec, p_slots);
    s2_frame_transmitter<float> tx(&sch, p_slots, p_iq);
    pipereader<bbframe> t_bb(p_bb);
    pipereader<fecframe<hard_sb> > t_fec(p_fec);
    pipereader<cf32> t_iq(p_iq);
    pipewriter<tspacket> w(p_ts);
    memcpy(w.wr(), ts.data(), ts.size() * sizeof(tspacket));
    w.written(ts.size());
    sch.run();

    const modcod_info* mi = check_modcod(pls.modcod);
    const fec_info* fi = &fec_infos[pls.sf][mi->rate];
    int nbits = pls.sf ? 16200 : 64800;
    if ((int)t_bb.readable() != nf || (int)t_fec.readable() != nf) {
        fprintf(stderr, "produced %d/%d frames\n", (int)t_bb.readable(), (int)t_fec.readable());
        return 1;
    }
    FILE* fb = fopen((out + "/bb.u8").c_str(), "wb");
    FILE* ff = fopen((out + "/fec.u8").c_str(), "wb");
    for (int k = 0; k < nf; k++) {
        dump_bits(fb, t_bb.rd()[k].bytes, fi->Kbch);
        dump_bits(ff, t_fec.rd()[k].bytes, nbits);
    }
    fclose(fb);
    fclose(ff);
    FILE* fp = fopen((out + "/pl.f32").c_str(), "wb");
    int nsym = t_iq.readable();
    for (int k = 0; k < nsym; k++) {
        float iq[2] = { t_iq.rd()[k].re, t_iq.rd()[k].im };
        fwrite(iq, sizeof(float), 2, fp);
    }
    fclose(fp);
    printf("kbch %d kldpc %d symbols %d frames %d\n", fi->Kbch, fi->kldpc, nsym, nf);
    return 0;
}
