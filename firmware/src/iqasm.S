// Hand-scheduled LUT kernels whose register needs exceed what GCC inline asm can allocate.
// void k_asm8_<L>(uint32_t *out, const uint32_t *in, uint32_t nwords, uint32_t prev,
//                 const iq_cfg_t *cfg)            -- same contract as iq_kernel_t (iqgen.h)
// N = 8 samples/symbol, PAIRS layout. Per symbol: 2 UBFX, 2 ADD, 2 LDM x4, 1 STM x8.
// I words load into r1 r3 r5 r8 and Q words into r2 r4 r6 r9, so the single STM writes
// [I01][Q01][I23][Q23][I45][Q45][I67][Q67], the order the PAIRS PIO program expects.
// Registers: r0 out, r7 prev, r10 ti, r11 tq, r12 ri, lr rq; in/end live on the stack.

    .syntax unified
    .thumb
    .section .time_critical.iqasm, "ax"

    .equ CFG_TI, 12                     // offsetof(iq_cfg_t, ti)
    .equ CFG_TQ, 16                     // offsetof(iq_cfg_t, tq)

    .macro K8 L
    .global k_asm8_\L
    .type k_asm8_\L, %function
    .thumb_func
k_asm8_\L:
    push    {r4-r11, lr}
    ldr     ip, [sp, #36]               // cfg: 5th argument, above 9 saved registers
    ldr     r10, [ip, #CFG_TI]
    ldr     r11, [ip, #CFG_TQ]
    mov     r7, r3
    add     r2, r1, r2, lsl #2          // end
    sub     sp, #8
    strd    r1, r2, [sp]
1:  ldrd    r1, r2, [sp]
    cmp     r1, r2
    beq     2f
    ldr     r3, [r1], #4                // cur = *in++
    str     r1, [sp]
    pkhbt   r12, r7, r3, lsl #16        // ri = prev.I | cur.I << 16
    pkhtb   lr, r3, r7, asr #16         // rq = prev.Q | cur.Q << 16
    mov     r7, r3
    .irp j, 0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15
    ubfx    r1, r12, #(17+\j-\L), #\L
    ubfx    r2, lr, #(17+\j-\L), #\L
    add     r1, r10, r1, lsl #4
    add     r2, r11, r2, lsl #4
    ldm     r1, {r1, r3, r5, r8}
    ldm     r2, {r2, r4, r6, r9}
    stm     r0!, {r1-r6, r8, r9}
    .endr
    b       1b
2:  add     sp, #8
    pop     {r4-r11, pc}
    .size k_asm8_\L, . - k_asm8_\L
    .endm

    K8 8
    K8 10

// N = 4, software-pipelined (v5). The next symbol's UBFX pair sits between this symbol's loads
// and stores, so no store waits on a just-loaded register. The first index pair of each word is
// computed with the window build. Registers: r0 out, r1/r2 idx I/Q, r3-r6 data, r7 prev,
// r8 ti0, r9 tq0, r10 ti1 (= ti0 + 4), r11 tq1, r12 ri, lr rq; in/end on the stack.
    .macro SYM4P L, j, last
    ldr     r3, [r8, r1, lsl #3]        // I phases 0,1
    ldr     r4, [r9, r2, lsl #3]        // Q phases 0,1
    ldr     r5, [r10, r1, lsl #3]       // I phases 2,3
    ldr     r6, [r11, r2, lsl #3]       // Q phases 2,3
    .if \last == 0
    ubfx    r1, r12, #(18+\j-\L), #\L
    ubfx    r2, lr, #(18+\j-\L), #\L
    .endif
    strd    r3, r4, [r0], #8
    strd    r5, r6, [r0], #8
    .endm

    .macro K4P L, sect
    .section \sect, "ax"
    .global k_asm4p_\L
    .type k_asm4p_\L, %function
    .thumb_func
k_asm4p_\L:
    push    {r4-r11, lr}
    ldr     ip, [sp, #36]
    ldr     r8, [ip, #CFG_TI]
    ldr     r9, [ip, #CFG_TQ]
    add     r10, r8, #4
    add     r11, r9, #4
    mov     r7, r3
    add     r2, r1, r2, lsl #2
    sub     sp, #8
    strd    r1, r2, [sp]
1:  ldrd    r1, r2, [sp]
    cmp     r1, r2
    beq     2f
    ldr     r3, [r1], #4
    str     r1, [sp]
    pkhbt   r12, r7, r3, lsl #16
    pkhtb   lr, r3, r7, asr #16
    mov     r7, r3
    ubfx    r1, r12, #(17-\L), #\L
    ubfx    r2, lr, #(17-\L), #\L
    .irp j, 0,1,2,3,4,5,6,7,8,9,10,11,12,13,14
    SYM4P   \L, \j, 0
    .endr
    SYM4P   \L, 15, 1
    b       1b
2:  add     sp, #8
    pop     {r4-r11, pc}
    .size k_asm4p_\L, . - k_asm4p_\L
    .endm

    K4P 10, .scratch_x.k_asm4p_10       // SRAM8, like k_asm_4_10, for a like-for-like stream test
    K4P 12, .time_critical.k_asm4p_12

// Isolated 8PSK experiment: L=12,N=4, two signed binary-amplitude planes.
// Contract in iq8.h. Eight sample-word reads/four additions per symbol; no IQ flag in tables.
// r0 out, r1/r2 A0/B0, r3/r4 I/Q history A, r5/r6 I/Q history B,
// r7/r8 indices, r9/r10 samples, r11/lr A1/B1, r12 packed 14-bit mask.
    .section .time_critical.iq8_asm, "ax"
    .macro IQ8_AXIS hist_a, hist_b, j, dst0, dst1, flag
    ubfx    r7, \hist_a, #(5+\j), #12
    ubfx    r8, \hist_b, #(5+\j), #12
    ldr     r9, [r1, r7, lsl #3]
    ldr     r10, [r2, r8, lsl #3]
    sadd16  r9, r9, r10
    and     r9, r9, r12
    .if \flag
    orr     r9, r9, #0x40004000
    .endif
    str     r9, [r0, #\dst0]
    ldr     r9, [r11, r7, lsl #3]
    ldr     r10, [lr, r8, lsl #3]
    sadd16  r9, r9, r10
    and     r9, r9, r12
    .if \flag
    orr     r9, r9, #0x40004000
    .endif
    str     r9, [r0, #\dst1]
    .endm

    .global iq8_kernel_asm
    .type iq8_kernel_asm, %function
    .thumb_func
iq8_kernel_asm:
    push    {r4-r11, lr}
    sub     sp, #16
    add     r2, r1, r2, lsl #3
    strd    r1, r2, [sp]                 // input,end
    cmp     r3, #0
    beq     3f
    ldm     r3, {r3, r4}                 // previous A,B words
    b       4f
3:  movs    r3, #0
    movw    r4, #0
    movt    r4, #0xffff                  // prior repeated pi/8 symbols
4:  strd    r3, r4, [sp, #8]
    ldr     r12, [sp, #52]               // 5th argument, beyond saved regs + locals
    ldm     r12, {r1, r2}                // table A,B
    add     r11, r1, #4
    add     lr, r2, #4
    movw    r12, #0x3fff
    movt    r12, #0x3fff
1:  ldrd    r7, r8, [sp]
    cmp     r7, r8
    beq     2f
    ldm     r7!, {r3, r5}                // current A,B
    str     r7, [sp]
    ldrd    r4, r6, [sp, #8]             // previous A,B
    strd    r3, r5, [sp, #8]
    pkhbt   r7, r4, r3, lsl #16          // I-A
    pkhtb   r4, r3, r4, asr #16          // Q-A
    pkhbt   r8, r6, r5, lsl #16          // I-B
    pkhtb   r6, r5, r6, asr #16          // Q-B
    mov     r3, r7
    mov     r5, r8
    .irp j, 0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15
    IQ8_AXIS r3, r5, \j, 0, 8, 1
    IQ8_AXIS r4, r6, \j, 4, 12, 0
    add     r0, #16
    .endr
    b       1b
2:  add     sp, #16
    pop     {r4-r11, pc}
    .size iq8_kernel_asm, . - iq8_kernel_asm
