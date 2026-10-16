# Core-0 stack overflow into SCRATCH_Y code: the kernel-3 hangs

**Finding:** core 0 uses at least 2,328 bytes of stack, but the SDK gives it 2,048 bytes at
the top of SCRATCH_Y, directly above SRAM-resident code. A complete `dualstream` run (its
final JSON `printf` is the deepest path observed) overflowed the stack and overwrote the
top of the SCRATCH_Y code. Before C2 the code ended 304 bytes below the stack, so the
overflow landed in unused space with about 24 bytes to spare; C2's t=8 BCH leaf filled the
gap (96 bytes left), and the next kernel-3 run executed corrupted code and hung. That
explains all three hangs (each followed a previous run's print) and why kernel 2 never hung.

**Fix:** `CORE0_STACK_SRAM_HI` (now default ON) moves core 0's stack to the top of SRAM4-7
with 4 KiB (`PICO_STACK_SIZE=0x1000`; core 1 keeps 2 KiB in SCRATCH_X). A link-time
assertion keeps it clear of `.sram_hi`. SCRATCH_Y then holds code only.

Evidence (Pico Plus 2, `stackcheck` paints each stack at boot and compares the scratch code
with its flash image; `stack_driver.py` reboots instead of running damaged code):

| Run | Image | Core-0 stack | Deepest use | Scratch code | Result |
|---|---|---:|---:|---|---|
| `stack-1/` | `85374b10f7`, stack in SCRATCH_Y | 2048 | ≥2144 (paint exhausted) | corrupted after one `dualstream` | next kernel-3 run hung |
| `stack-2-scratchy/` | `c6b87e4737`, stack in SCRATCH_Y | 2048 | ≥2328 (first corrupt code offset 1768) | corrupted | rebooted |
| `stack-3-sramhi/` | `c6b87e4737`, `CORE0_STACK_SRAM_HI=ON` | 4096 | 2312-2360 over 8 runs | intact after every run | all 8 runs, including both previously hanging kernel-3 cases, completed |

Core 1 peaks at 268 of 2048 bytes. Moving the stack did not change measured load: QPSK 9/10
TS, BCH kernel 2, 3 s gave core 0 86.71% with either stack placement (core 1 91.84% /
91.93%). `stack-3-sramhi` runs record errors and loads only; their captures are not
validated (the qualification runs are separate).

This overflow predates the round-2 work: any image with the default 2 KiB core-0 stack and
these print paths has been within about 24 bytes of corrupting SCRATCH_Y code.
