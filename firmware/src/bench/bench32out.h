// Continuous32APSK component output, using precomputed rolling-triple keys.
#pragma once
// L8/N2, rate5/6 ring ratios, R3=1, cyclic8192-symbol geometric fixture.
// RAW_PAIRS GPIO output/capture; cpw4/6/8,kernel0C/1ASM; core1 idle.
// Encodingkeys happens once before timing and uses4 bytes/complexsymbol.
// No FEC/live input/fullmodem/AFE/RF qualification is implied.
void bench_32out(int cpw, int ms, int kernel);
