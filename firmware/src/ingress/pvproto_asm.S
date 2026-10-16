// Position-independent PV-SPI TS word leaf with pv_words_c semantics (pvproto.c).
// No literal pool, external branch or mutable global: pv_words_prepare copies these
// bytes into a caller-owned SRAM arena so the loop does not fetch through XIP.
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
    .syntax unified
    .thumb

// One aligned source word w = b0 | b1<<8 | b2<<16 | b3<<24 (b0 first on the wire):
// crc = T3[crc ^ b0] ^ T2[b1] ^ T1[b2] ^ T0[b3], then the swapped word rev(w) is
// funnelled after the phase's pending bytes. r0 src, r1 dst, r4 acc, r5 crc,
// r6..r9 T0..T3; r3, r10-r12 and lr are scratch. Each load result is consumed at
// least one instruction later.
    .macro PV_WORD_CRC
    ldr     r10, [r0], #4
    ubfx    r12, r10, #8, #8
    eor     r11, r10, r5
    ubfx    lr, r10, #16, #8
    uxtb    r11, r11
    ubfx    r3, r10, #24, #8
    ldrb    r12, [r8, r12]
    ldrb    r11, [r9, r11]
    ldrb    lr, [r7, lr]
    ldrb    r3, [r6, r3]
    rev     r10, r10
    eor     r11, r11, r12
    eor     lr, lr, r3
    eor     r5, r11, lr
    .endm
    .macro PV_WORD_PUT shift
    orr     r11, r4, r10, lsr #\shift
    lsl     r4, r10, #(32-\shift)
    str     r11, [r1], #4
    .endm

    .section .text.pv_words_pic, "ax", %progbits
    .align 2
    .global pv_words_pic
    .global pv_words_pic_end
    .type pv_words_pic, %function
    .thumb_func
// r0 = source words, r1 = output words, r2 = count >= 1, r3 = pv_words_state_t
// {acc, crc, phase, tables}. Updates acc and crc; phase is unchanged.
pv_words_pic:
    push    {r3-r11, lr}
    ldr     r4, [r3, #0]
    ldr     r5, [r3, #4]
    ldr     r6, [r3, #12]
    ldr     r12, [r3, #8]
    add     r7, r6, #256
    add     r8, r6, #512
    add     r9, r6, #768
    cmp     r12, #1
    beq     .Lpv_phase1
    cmp     r12, #2
    beq     .Lpv_phase2
    cmp     r12, #3
    beq     .Lpv_phase3
.Lpv_phase0:
    PV_WORD_CRC
    str     r10, [r1], #4
    subs    r2, r2, #1
    bne     .Lpv_phase0
    b       .Lpv_done
.Lpv_phase1:
    PV_WORD_CRC
    PV_WORD_PUT 8
    subs    r2, r2, #1
    bne     .Lpv_phase1
    b       .Lpv_done
.Lpv_phase2:
    PV_WORD_CRC
    PV_WORD_PUT 16
    subs    r2, r2, #1
    bne     .Lpv_phase2
    b       .Lpv_done
.Lpv_phase3:
    PV_WORD_CRC
    PV_WORD_PUT 24
    subs    r2, r2, #1
    bne     .Lpv_phase3
.Lpv_done:
    ldr     r3, [sp, #0]
    str     r4, [r3, #0]
    str     r5, [r3, #4]
    pop     {r3-r11, pc}
    .size pv_words_pic, .-pv_words_pic
pv_words_pic_end:
#endif
