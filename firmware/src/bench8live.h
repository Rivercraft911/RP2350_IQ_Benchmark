// Discrete full-CCM 8PSK/GCS digital trial; no persistent operating mode.
#pragma once

// Normal5/6, pilots, Gold0, globally rotated pi/8; L9/N2 quad shaper.
// Fixed cpw4. Supports128MHz/SPI32MHz and150MHz/SPI25MHz only.
// Borrowed transmitter buffers and the core1 task loop must be idle on entry.
// Prints one JSON result and a completed4096-word raw GPIO capture, then restores
// the QPSK LUTs, PRBS input and link ring. No RF/AFE qualification is implied.
void bench_8live(int ms);
// Alternates normal5/6 8PSK/QPSK PLFRAMEs at one symbol clock and one continuous
// pi/8-rotated L9/N2 shaper. MATYPE signals VCM; MODCOD15/9 in each PLHEADER.
void bench_vcm(int ms);
