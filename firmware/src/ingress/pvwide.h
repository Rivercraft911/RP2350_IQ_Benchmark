// Opt-in, idle-only PV message ingress benchmark. No transmitter defaults change.
#pragma once

// Same wire protocol in both widths: data GP17..20 (MSB nibble first at width 4),
// clock GP21, CS_N GP26, READY GP22. In emulated mode external devices must
// leave these pins undriven. half is the on-chip sender clock half-period
// in SYS clocks. fault: 0 clean; 1 message 0/16 CRC corruption; 2 all short 4 B.
// emulate=1 generates traffic internally; emulate=0 receives an external
// source on the same bus (fault must be0, half is ignored for external timing).
void bench_pvwide(int ms, int width, int half, int fault, int emulate);
