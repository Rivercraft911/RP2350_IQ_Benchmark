#pragma once

// Idle-only cyclic 8PSK hardware shaper. cpw2/4, SYS128, inverted DDR control0/1.
void bench4live(int cpw, int ms, int invert);
// Signed8 cyclic shaper; SYS128/256, cpw1/2/4 with per-axis DAC rate<=65MHz.
void bench4live8(int cpw, int ms, int invert, int priority);
