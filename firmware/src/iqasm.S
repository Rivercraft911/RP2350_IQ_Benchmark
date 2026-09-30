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
