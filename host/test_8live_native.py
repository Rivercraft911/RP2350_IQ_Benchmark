from pathlib import Path
import sys,tempfile,subprocess,ctypes as ct,zlib,json
import numpy as np
ROOT=Path(__file__).resolve().parents[1];SRC=ROOT/'firmware/src'
sys.path[:0]=[str(ROOT/'host')]
import test_dual_codes as dc
import test_dvbs2_8psk as maptest
import test_iq8quad as pulse
s=(SRC/'bench8live.c').read_text();function=dc.function
constants=s[s.index('enum {'):s.index('};')+2]
state=s[s.index('static struct {'):s.index('} live;')+len('} live;')]
head='''#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include "config.h"
#include "dvbs2_codes.h"
#include "dvbs2_8psk.h"
#include "dvbs2_qpsk8.h"
#include "iq8quad.h"
#include "iqgen.h"
#include "pvproto.h"
#define __dmb() ((void)0)
#define BB_WORDS (64800/32+2)
#define PV_SLOTS 24
#define PV_EMU_WORDS (16*PV_MSG_WORDS)
static uint32_t bb_buf[BB_WORDS],in_buf[IN_WORDS],gold[TABLE_WORDS];
static uint32_t a[IQ8QUAD_TABLE_WORDS],b[IQ8QUAD_TABLE_WORDS];
static uint32_t trace[512*2048];
static uint32_t cycles(void){return 0;}
static unsigned packet_index;
static uint8_t *pvspi_next_packet(void) {
 static uint8_t p[188];unsigned k=packet_index++%112;
 p[0]=0x47;p[1]=1;p[2]=0;p[3]=0x10|(k&15);
 for(unsigned j=0;j<184;j++)p[j+4]=(k+j)&255;
 return p;
}
'''
tail='''
uint32_t prefill_crc,prefill_groups,prefill_fill,bb_crc[4],encoded_frames;
static uint32_t hash_live(void) {
 uint32_t hash=2166136261;
 iq8_input_t *input=(iq8_input_t*)in_buf;
 for(uint32_t k=live.consumed;k<live.total;k++) {
  hash=(hash^input[k&INPUT_MASK].a)*16777619;
  hash=(hash^input[k&INPUT_MASK].b)*16777619;
 }
 return hash;
}
static int encode_check(void) {
 uint32_t before=hash_live(),total=live.total;
 if(!encode_frame())return 1;
 uint32_t saved=live.total;live.total=total;
 int changed=hash_live()!=before;live.total=saved;
 return changed?2:0;
}
int trial(int vcm) {
 packet_index=0;pv_init(16);dvbs2_bch_set_kernel(S2_BCH_STREAM_ARM);
 dvbs2_map_set_kernel(1);
 dvbs2_init(&DVBS2_CODES[S2_N5_6],true);
 live=(typeof(live)){.vcm=vcm,.stream={.out=(iq8_input_t*)in_buf,.mask=INPUT_MASK}};
 if(!dvbs2_8psk_init(&live.profile,&DVBS2_CODES[S2_N5_6],true,0,gold,TABLE_WORDS))return 3;
 live.frame_groups=((vcm?dvbs2_plframe_symbols():live.profile.frame_symbols)+15)/16;
 live.qpsk_scratch=gold+dvbs2_8psk_arena_words(&DVBS2_CODES[S2_N5_6],true);
 for(unsigned n=0;n<2;n++)if(!encode_frame())return 4;
 prefill_groups=live.total;prefill_fill=live.stream.fill;
 prefill_crc=crc32_update(0,in_buf,prefill_groups*8);
 iq8quad_tables_t tables={a,b};iq8quad_build(&tables);
 iq8_input_t *input=(iq8_input_t*)in_buf;
 for(uint32_t sequence=0;sequence<512;sequence++) {
   const uint32_t need=(sequence+1)*BLOCK_GROUPS;
   if(live.stream.n+live.frame_groups+1-live.consumed<=INPUT_GROUPS) {
     if(encode_check())return 5;
   }
   if(live.total<need)return 6;
   uint32_t group=(sequence*BLOCK_GROUPS)&INPUT_MASK;
   iq8quad_kernel_c(trace+sequence*OUT_WORDS,input+group,BLOCK_GROUPS,
       sequence?input+((group-1)&INPUT_MASK):NULL,&tables);
   live.consumed=(sequence+1)*BLOCK_GROUPS-1;
 }
 encoded_frames=live.frames;
 for(unsigned n=0;n<4;n++)bb_crc[n]=live.bb_crc[n];
 return 0;
}
const uint32_t *output(void){return trace;}
'''
with tempfile.TemporaryDirectory(prefix='8live-native-') as d:
 d=Path(d);c=d/'trial.c';so=d/'trial.so';c.write_text(head+constants+state+function(s,'encode_frame')+tail)
 subprocess.run(['cc','-std=gnu11','-O2','-Wall','-Wextra','-Werror','-shared','-fPIC','-I'+str(SRC),str(c),
   *(str(SRC/n) for n in ('dvbs2.c','dvbs2_8psk.c','dvbs2_qpsk8.c','pvproto.c','iqgen.c','iq8quad.c')),'-o',str(so)],check=True)
 lib=ct.CDLL(str(so));lib.output.restype=ct.POINTER(ct.c_uint32)
 get=lambda n:ct.c_uint32.in_dll(lib,n).value
 for vcm in (False,True):
  assert lib.trial(vcm)==0,'native queue/immutable input'
  points=[];bb=[]
  for k in range(24):
   bits=dc.bbframe(k,'5/6',True)
   if vcm:
    bits[:80]=dc.spi.ref.bbheader(dc.spi.ref.matype1(dc.spi.ref.GCS,.20,ccm=0),53760,sync=0xb9)
   bb.append(bits)
   fec=dc.spi.ref.fecframe(bits,'5/6')
   if vcm and k&1:
    bi,bq=dc.spi.ref.plframe(fec,'5/6',pilots=True)
    frame=((1-2*bi.astype(float))+1j*(1-2*bq.astype(float)))/np.sqrt(2)*np.exp(1j*np.pi/8)
   else:frame=maptest.scalar_frame(fec,'5/6',False,True)
   points.append(frame)
  prefill_symbols=sum(len(frame) for frame in points[:2])
  points=np.concatenate(points)
  assert len(points)>=512*1024
  packed=maptest.packed_groups(points[:prefill_symbols])
  assert len(packed)==get('prefill_groups') and zlib.crc32(packed.tobytes())==get('prefill_crc')
  assert get('prefill_fill')==4
  crcs=list((ct.c_uint32*4).in_dll(lib,'bb_crc'))
  assert crcs==[zlib.crc32(np.packbits(frame).tobytes()) for frame in bb[:4]]
  taps,coef=pulse.coefficients();bits=maptest.sign_planes(points[:512*1024])
  want,_,error=pulse.direct_reference(bits,taps,coef)
  actual=np.ctypeslib.as_array(lib.output(),shape=(512*2048,))
  assert np.array_equal(actual,want),np.nonzero(actual!=want)[0][:10]
  result=dict(vcm=vcm,blocks=512,symbols=512*1024,source_ring_wraps=8,raw_words=len(actual),
   raw_crc=zlib.crc32(actual.tobytes()),prefill_groups=get('prefill_groups'),prefill_crc=get('prefill_crc'),
   bb_crc=crcs,encoded_frames=get('encoded_frames'),max_float_error_lsb=error)
  print(json.dumps(result))
