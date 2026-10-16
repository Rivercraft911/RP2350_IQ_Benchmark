# R2 on the Pico: four DMA input grants that re-arm themselves, no CPU service

Pico Plus 2, RP2350B A2, SYS128, USB only, image `523de4a7ed` (`rd/r2-hw-offload`,
experiment build; `flash.txt`), runner `host/r2_grant_trial.py`. The `r2grant` diagnostic
runs four independent input readers, each fed by finite 512-word DMA grants that a dedicated
reload channel re-arms from a prepublished control list, ending in a null-trigger sentinel.
Four PIO SMs echo every word they receive into SRAM, so the host checks every issued word
exactly. No CPU touches the grants while the readers run. Design and budgets:
[docs/rd/r2-hw-offload.md](../../../docs/rd/r2-hw-offload.md).

| Run set | Rate (cpw) | Priority | Runs | Result |
|---|---|---:|---:|---|
| `grants-clean/` | 8 and 16 Msym/s (2, 1) | 0, 13 | 24 (6 each), 8 grants per reader | 24/24 exact: 4 x 4096 words issued and echoed, no TX stall, stop on the sentinel |
| `grants-short-prefix/` | 16 Msym/s (1) | 13 | 3 | 3/3 starvation detected as designed: reader 3 publishes one grant short (3584 words), stalls and stops |

## Reading

This shows the reload seam works with no CPU service for a finite, prepublished epoch of
eight grants (about 4 ms at 16 Msym/s). It is not integrated with B6's output engine: doing
so needs four more DMA channels (16 of 16 with capture disabled) and a rolling publication
scheme. B6 currently reserves core 1 for this re-arming; R2's lower-channel alternative is a
completion interrupt (estimated about 1% of one core, unmeasured).
