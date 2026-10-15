#include "dvbs2_apsk.h"
#include <stddef.h>
#include <string.h>

static unsigned modcod(const dvbs2_code_t *code, unsigned modulation) {
    if (!code || (code->nldpc != 64800 && code->nldpc != 16200) ||
        code->nldpc != (code->is_short ? 16200 : 64800) ||
        (code->is_short && code->modcod == 11)) return 0;
    if (modulation == 16 && code->modcod >= 6 && code->modcod <= 11) return code->modcod+12;
    if (modulation == 32 && code->modcod >= 7 && code->modcod <= 11) return code->modcod+17;
    return 0;
}

uint32_t dvbs2_apsk_arena_words(const dvbs2_code_t *code, unsigned modulation, bool pilots) {
    if (!modcod(code, modulation)) return 0;
    const uint32_t data = code->nldpc / (modulation == 16 ? 4u : 5u);
    const uint32_t body = data + (pilots ? 36u*((data/90u-1u)/16u) : 0);
    return 2u*((body+31u)/32u+1u);
}

static uint32_t xstep(uint32_t x) { return (x>>1) | ((((x>>7)^x)&1u)<<17); }
static uint32_t ystep(uint32_t y) { return (y>>1) | ((((y>>10)^(y>>7)^(y>>5)^y)&1u)<<17); }

bool dvbs2_apsk_init(dvbs2_apsk_cfg_t *cfg, const dvbs2_code_t *code, unsigned modulation,
                     bool pilots, uint32_t gold_code, uint32_t *arena, uint32_t arena_words) {
    const uint32_t need = dvbs2_apsk_arena_words(code, modulation, pilots);
    if (!cfg || !arena || !need || arena_words < need || gold_code >= 262143u) return false;
    *cfg = (dvbs2_apsk_cfg_t){.code=code, .gold=(dvbs2_apsk_gold_t *)arena,
        .data_symbols=code->nldpc/(modulation == 16 ? 4u : 5u), .gold_code=gold_code,
        .modulation=(uint8_t)modulation, .modcod=(uint8_t)modcod(code,modulation),
        .pilots=pilots, .is_short=code->is_short};
    cfg->pilot_blocks = pilots ? (cfg->data_symbols/90u-1u)/16u : 0;
    cfg->body_symbols = cfg->data_symbols + 36u*cfg->pilot_blocks;
    cfg->frame_symbols = cfg->body_symbols + 90u;
    memset(arena, 0, need*sizeof *arena);
    uint32_t xa=1, ya=0x3ffffu, yb=ya;
    for (uint32_t i=0; i<gold_code; ++i) xa=xstep(xa);
    uint32_t xb=xa;
    for (uint32_t i=0; i<131072u; ++i) { xb=xstep(xb); yb=ystep(yb); }
    uint32_t data=0, pilot=cfg->data_symbols;
    for (uint32_t i=0; i<cfg->body_symbols; ++i) {
        const uint32_t bit = pilots && i%1476u >= 1440u ? pilot++ : data++;
        cfg->gold[bit>>5].r0 |= ((xa^ya)&1u)<<(bit&31u);
        cfg->gold[bit>>5].r1 |= ((xb^yb)&1u)<<(bit&31u);
        xa=xstep(xa); ya=ystep(ya); xb=xstep(xb); yb=ystep(yb);
    }
    // Same SOF/bi-orthogonal PLS construction as the existing QPSK/8PSK mappers.
    static const uint32_t generator[6] = {0x55555555u,0x33333333u,0x0f0f0f0fu,
        0x00ff00ffu,0x0000ffffu,0xffffffffu};
    const uint32_t signalling = (uint32_t)cfg->modcod<<2 | (uint32_t)cfg->is_short<<1 | pilots;
    uint32_t y=0;
    for (unsigned j=0; j<6; ++j) if ((signalling>>(6-j))&1u) y^=generator[j];
    uint64_t pls=0;
    for (unsigned j=0; j<32; ++j) {
        const uint64_t bit=(y>>(31-j))&1u;
        pls |= bit<<(63-2*j) | (bit^(signalling&1u))<<(62-2*j);
    }
    pls ^= 0x719d83c953422dfaull;
    for (unsigned j=0; j<90; ++j) {
        const uint32_t bit = j<26 ? (0x18d2e82u>>(25-j))&1u : (uint32_t)(pls>>(89-j))&1u;
        cfg->header[j>>4] |= (bit^(j&1u))<<(j&15u) | bit<<(16+(j&15u));
    }
    return true;
}

static inline uint32_t reverse32(uint32_t x) {
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
    __asm("rbit %0, %1" : "=r"(x) : "r"(x));
    return x;
#else
    x=((x&0x55555555u)<<1)|((x>>1)&0x55555555u);
    x=((x&0x33333333u)<<2)|((x>>2)&0x33333333u);
    x=((x&0x0f0f0f0fu)<<4)|((x>>4)&0x0f0f0f0fu);
    return (x<<24)|((x&0xff00u)<<8)|((x>>8)&0xff00u)|(x>>24);
#endif
}

static inline uint32_t column32(const uint32_t *frame, uint32_t bit) {
    const uint32_t word=bit>>5, shift=bit&31u;
    return reverse32(shift ? frame[word]<<shift | frame[word+1]>>(32-shift) : frame[word]);
}

static inline dvbs2_apsk_gold_t gold32(const dvbs2_apsk_cfg_t *cfg, uint32_t position) {
    const uint32_t word=position>>5, shift=position&31u;
    const dvbs2_apsk_gold_t a=cfg->gold[word];
    if (!shift) return a;
    const dvbs2_apsk_gold_t b=cfg->gold[word+1];
    return (dvbs2_apsk_gold_t){a.r0>>shift | b.r0<<(32-shift), a.r1>>shift | b.r1<<(32-shift)};
}

static inline bool emit(dvbs2_apsk_stream_t *s, uint32_t word) {
    if (!(s->n&(DVBS2_APSK_BATCH_WORDS-1u)) && s->service && !s->service(s->context,s->n)) return false;
    s->out[s->n++ & s->mask]=word;
    return true;
}

static const uint16_t spread4[16] = {
    0,1,8,9,64,65,72,73,512,513,520,521,576,577,584,585,
};

// The Q magnitude planes are the I magnitude planes in reverse order.
typedef struct { uint32_t m0,m1,si,sq; } planes16_t;
static bool put16_small(dvbs2_apsk_stream_t *s, planes16_t p, unsigned count) {
    while (count) {
        const unsigned take = count < 4u-s->fill ? count : 4u-s->fill;
        const uint32_t mask=(1u<<take)-1u, shift=3u*s->fill;
        const uint32_t a=spread4[p.m0&mask], b=spread4[p.m1&mask];
        const uint32_t i=s->acc_i | (a | b<<1 | spread4[p.si&mask]<<2)<<shift;
        const uint32_t q=s->acc_q | (b | a<<1 | spread4[p.sq&mask]<<2)<<shift;
        if (s->fill+take == 4) {
            if (!emit(s,i | q<<16)) return false;
            s->acc_i=s->acc_q=s->fill=0;
        } else { s->acc_i=i; s->acc_q=q; s->fill+=take; }
        p.m0>>=take; p.m1>>=take; p.si>>=take; p.sq>>=take;
        count-=take;
    }
    return true;
}

// A32-symbol run leaves the native partial fill unchanged. Mask each lane
// before shifting so its overflow never contaminates the other axis.
static bool put16(dvbs2_apsk_stream_t *s, planes16_t p, unsigned count) {
    if (count != 32) return put16_small(s,p,count);
    const unsigned shift=3u*s->fill;
    const uint32_t keep=((1u<<(12-shift))-1u)*0x10001u;
    const uint32_t spill=((1u<<shift)-1u)*0x10001u;
    uint32_t carry=s->acc_i | s->acc_q<<16, n=s->n;
    unsigned words=8;
    while (words) {
        s->n=n;
        if (!(n&63u) && s->service && !s->service(s->context,n)) {
            s->acc_i=carry&0xffffu; s->acc_q=carry>>16;
            return false;
        }
        unsigned batch=64u-(n&63u);
        if (batch>words) batch=words;
        const uint32_t to_wrap=s->mask-(n&s->mask);
        if (batch-1u>to_wrap) batch=to_wrap+1u;
        uint32_t *output=s->out+(n&s->mask);
        for (unsigned j=0; j<batch; ++j) {
            const uint32_t a=spread4[p.m0&15u], b=spread4[p.m1&15u];
            const uint32_t i=a | b<<1 | spread4[p.si&15u]<<2;
            const uint32_t q=b | a<<1 | spread4[p.sq&15u]<<2;
            const uint32_t native=i | q<<16;
            *output++=carry | ((native&keep)<<shift);
            carry=(native>>(12-shift))&spill;
            p.m0>>=4; p.m1>>=4; p.si>>=4; p.sq>>=4;
        }
        n+=batch;
        words-=batch;
    }
    s->n=n;
    s->acc_i=carry&0xffffu; s->acc_q=carry>>16;
    return true;
}

// Label abcd: I magnitude=2a+b, Q magnitude=2b+a, I/Q signs=c/d.
static inline planes16_t map16(uint32_t a,uint32_t b,uint32_t c,uint32_t d,
                                uint32_t r0,uint32_t r1) {
    const uint32_t magnitude_swap=(a^b)&r0, sign_swap=(c^d)&r0;
    const uint32_t x=a^magnitude_swap, y=b^magnitude_swap;
    return (planes16_t){y,x,c^sign_swap^r0^r1,d^sign_swap^r1};
}

#define C(i,q) ((uint32_t)(i) | (uint32_t)(q)<<16)
static const uint32_t points32[128] = {
    // Gold quarter turn0.
    C(3,3), C(2,4), C(3,11), C(2,12), C(11,3), C(10,4), C(11,11), C(10,12),
    C(7,5), C(5,7), C(6,14), C(0,16), C(14,6), C(0,8), C(15,13), C(13,15),
    C(4,2), C(1,1), C(4,10), C(1,9), C(12,2), C(9,1), C(12,10), C(9,9),
    C(8,0), C(6,6), C(7,13), C(5,15), C(15,5), C(13,7), C(16,0), C(14,14),
    // Gold quarter turn1.
    C(11,3), C(12,2), C(3,3), C(4,2), C(11,11), C(12,10), C(3,11), C(4,10),
    C(13,7), C(15,5), C(6,6), C(8,0), C(14,14), C(16,0), C(5,15), C(7,13),
    C(10,4), C(9,1), C(2,4), C(1,1), C(10,12), C(9,9), C(2,12), C(1,9),
    C(0,8), C(14,6), C(5,7), C(7,5), C(13,15), C(15,13), C(0,16), C(6,14),
    // Gold quarter turn2.
    C(11,11), C(10,12), C(11,3), C(10,4), C(3,11), C(2,12), C(3,3), C(2,4),
    C(15,13), C(13,15), C(14,6), C(0,8), C(6,14), C(0,16), C(7,5), C(5,7),
    C(12,10), C(9,9), C(12,2), C(9,1), C(4,10), C(1,9), C(4,2), C(1,1),
    C(16,0), C(14,14), C(15,5), C(13,7), C(7,13), C(5,15), C(8,0), C(6,6),
    // Gold quarter turn3.
    C(3,11), C(4,10), C(11,11), C(12,10), C(3,3), C(4,2), C(11,3), C(12,2),
    C(5,15), C(7,13), C(14,14), C(16,0), C(6,6), C(8,0), C(13,7), C(15,5),
    C(2,12), C(1,9), C(10,12), C(9,9), C(2,4), C(1,1), C(10,4), C(9,1),
    C(0,16), C(6,14), C(13,15), C(15,13), C(5,7), C(7,5), C(0,8), C(14,6),
};
#undef C

static bool put32point(dvbs2_apsk_stream_t *s,uint32_t point) {
    const uint32_t i=s->acc_i | (point&31u)<<(5*s->fill);
    const uint32_t q=s->acc_q | (point>>16)<<(5*s->fill);
    if (s->fill) {
        if (!emit(s,i | q<<16)) return false;
        s->acc_i=s->acc_q=s->fill=0;
    } else { s->acc_i=i; s->acc_q=q; s->fill=1; }
    return true;
}

// Two symbols share one gather, native-word publication and carry update.
static const uint16_t spread2[4]={0,1,128,129};
static bool map32_carry(dvbs2_apsk_stream_t *s,uint32_t a,uint32_t b,uint32_t c,uint32_t d,
                   uint32_t e,dvbs2_apsk_gold_t gold,unsigned count) {
    const unsigned shift=5u*s->fill;
    const uint32_t keep=((1u<<(10-shift))-1u)*0x10001u;
    const uint32_t spill=((1u<<shift)-1u)*0x10001u;
    uint32_t carry=s->acc_i | s->acc_q<<16;
    while (count>=2) {
        const uint32_t pair=spread2[a&3u]<<4 | spread2[b&3u]<<3 | spread2[c&3u]<<2 |
                            spread2[d&3u]<<1 | spread2[e&3u] | spread2[gold.r0&3u]<<5 |
                            spread2[gold.r1&3u]<<6;
        const uint32_t native=points32[pair&127u] | points32[pair>>7]<<5;
        if (!emit(s,carry | ((native&keep)<<shift))) {
            s->acc_i=carry&0xffffu; s->acc_q=carry>>16;
            return false;
        }
        carry=(native>>(10-shift))&spill;
        a>>=2; b>>=2; c>>=2; d>>=2; e>>=2; gold.r0>>=2; gold.r1>>=2;
        count-=2;
    }
    s->acc_i=carry&0xffffu; s->acc_q=carry>>16;
    if (count) {
        const unsigned index=(a&1u)<<4 | (b&1u)<<3 | (c&1u)<<2 | (d&1u)<<1 | (e&1u) |
                              (gold.r0&1u)<<5 | (gold.r1&1u)<<6;
        return put32point(s,points32[index]);
    }
    return true;
}

// Transpose seven2-bit columns into two7-bit indexes, one per halfword.
static inline uint32_t gather_pair7(uint32_t a,uint32_t b,uint32_t c,uint32_t d,
                                     uint32_t e,dvbs2_apsk_gold_t gold) {
    const uint32_t v=(a&3u)<<8 | (b&3u)<<6 | (c&3u)<<4 | (d&3u)<<2 | (e&3u) |
                     (gold.r0&3u)<<10 | (gold.r1&3u)<<12;
    uint32_t lanes=(v&0x5555u) | (v&0xaaaau)<<15;
    lanes=(lanes | lanes>>1)&0x33333333u;
    lanes=(lanes | lanes>>2)&0x0f0f0f0fu;
    return (lanes | lanes>>4)&0x00ff00ffu;
}

// All standard32APSK frame lengths are even; retain the general odd-fill
// path for caller prefixes. Publish/reserve only at64-word credit boundaries.
static bool map32(dvbs2_apsk_stream_t *s,uint32_t a,uint32_t b,uint32_t c,uint32_t d,
                   uint32_t e,dvbs2_apsk_gold_t gold,unsigned count) {
    if (s->fill) return map32_carry(s,a,b,c,d,e,gold,count);
    uint32_t n=s->n;
    unsigned words=count/2;
    while (words) {
        s->n=n;
        if (!(n&63u) && s->service && !s->service(s->context,n)) return false;
        unsigned batch=64u-(n&63u);
        if (batch>words) batch=words;
        const uint32_t to_wrap=s->mask-(n&s->mask);
        if (batch-1u>to_wrap) batch=to_wrap+1u;
        uint32_t *output=s->out+(n&s->mask);
        for (unsigned j=0; j<batch; ++j) {
            const uint32_t pair=gather_pair7(a,b,c,d,e,gold);
            *output++=points32[pair&127u] | points32[pair>>16]<<5;
            a>>=2; b>>=2; c>>=2; d>>=2; e>>=2; gold.r0>>=2; gold.r1>>=2;
        }
        n+=batch;
        words-=batch;
    }
    s->n=n;
    if (count&1u) return map32_carry(s,a,b,c,d,e,gold,1);
    return true;
}

static bool header(const dvbs2_apsk_cfg_t *cfg,dvbs2_apsk_stream_t *s) {
    for (unsigned group=0; group<6; ++group) {
        const unsigned count=group==5 ? 10u : 16u;
        const uint32_t i=cfg->header[group]&0xffffu,q=cfg->header[group]>>16;
        if (cfg->modulation==16) {
            if (!put16(s,(planes16_t){0,0,i,q},count)) return false;
        } else {
            for (unsigned j=0; j<count; ++j)
                if (!put32point(s,(6u+8u*((i>>j)&1u)) | (6u+8u*((q>>j)&1u))<<16)) return false;
        }
    }
    return true;
}

static bool pilot(const dvbs2_apsk_cfg_t *cfg,dvbs2_apsk_stream_t *s,uint32_t block) {
    const uint32_t position=cfg->data_symbols+36u*block;
    for (unsigned start=0; start<36; start+=32) {
        const dvbs2_apsk_gold_t gold=gold32(cfg,position+start);
        const unsigned count=start ? 4u : 32u;
        if (cfg->modulation==16) {
            if (!put16(s,(planes16_t){0,0,gold.r0^gold.r1,gold.r1},count)) return false;
        } else {
            for (unsigned j=0; j<count; ++j) {
                const uint32_t i=(gold.r0^gold.r1)>>j, q=gold.r1>>j;
                if (!put32point(s,(6u+8u*(i&1u)) | (6u+8u*(q&1u))<<16)) return false;
            }
        }
    }
    return true;
}

bool dvbs2_apsk_frame(const dvbs2_apsk_cfg_t *cfg, const uint32_t *frame,
                      dvbs2_apsk_stream_t *stream) {
    if (!cfg || !frame || !stream || !stream->out ||
        (cfg->modulation!=16 && cfg->modulation!=32) ||
        stream->fill >= (cfg->modulation==16 ? 4u : 2u) || (stream->mask&(stream->mask+1u))) return false;
    dvbs2_apsk_stream_t s=*stream;
    bool ok=header(cfg,&s);
    uint32_t data=0, block=0;
    while (ok && data<cfg->data_symbols) {
        uint32_t run=cfg->data_symbols-data;
        if (cfg->pilots && run>1440) run=1440;
        const uint32_t end=data+run;
        while (ok && data<end) {
            const unsigned count=end-data<32 ? end-data : 32;
            const uint32_t a=column32(frame,data),b=column32(frame,cfg->data_symbols+data),
                           c=column32(frame,2*cfg->data_symbols+data),d=column32(frame,3*cfg->data_symbols+data);
            const dvbs2_apsk_gold_t gold=gold32(cfg,data);
            if (cfg->modulation==16) ok=put16(&s,map16(a,b,c,d,gold.r0,gold.r1),count);
            else ok=map32(&s,a,b,c,d,column32(frame,4*cfg->data_symbols+data),gold,count);
            data+=count;
        }
        if (ok && block<cfg->pilot_blocks && data<cfg->data_symbols) ok=pilot(cfg,&s,block++);
    }
    if (ok && s.service) ok=s.service(s.context,s.n);
    *stream=s;
    return ok;
}
