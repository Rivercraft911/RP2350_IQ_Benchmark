// Direct planar 16APSK rows through the shared dual-SM descriptor output engine.
#pragma once

// cpw4/6/8,100..60000ms,128/150MHz; core1 idle and cyclic PRBS coordinates.
// Four guarded output blocks, immutable row pointers, sparse exact GPIO capture.
void bench_16planar(int cpw, int ms);
