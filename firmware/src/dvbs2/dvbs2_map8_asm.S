// C6: byte-dilated, packed 8PSK mapper with immediate even-fill shifts.
// The caller clamps each run so its 2*count output words are contiguous in
// both rings. It uses the C4 leaf for odd fills and a pair crossing a ring edge.
// Same seven-argument ABI as codes_lut_run; count must be nonzero. Header
// emission has already dealt with defer_first. LUT is 256 uint16_t entries,
// each spreading the eight index bits into the even bits of a halfword.
//
// This entire section is copied into the caller's arena. Keep every branch
// local and do not introduce absolute data/code references or relocations.
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
    .syntax unified
    .thumb
    .section c6_mapper, "ax", %progbits
    .align 2
    .global dvbs2_map8_c6
    .type dvbs2_map8_c6, %function
    .thumb_func
dvbs2_map8_c6:
    push    {r4-r11, lr}
    sub     sp, #28
    // Local words: I carry, end(a), stream, final n. The remaining words
    // provide ABI alignment and leave the original three stack arguments
    // at fixed offsets 64, 68 and 72.
    ldr     r4, [sp, #64]
    ldr     r12, [sp, #68]
    ldr     r11, [sp, #72]
    cmp     r11, #0
    beq.w   .Lmap8_return
    str     r12, [sp, #8]
    add     lr, r0, r11, lsl #2
    str     lr, [sp, #4]
    ldmia   r12, {r5-r8} // I base, Q base, mask, n
    and     lr, r7, r8
    add     r5, r5, lr, lsl #2
    add     r6, r6, lr, lsl #2
    add     r8, r8, r11, lsl #1
    str     r8, [sp, #12]
    ldr     r7, [r12, #16]
    ldr     r8, [r12, #20]
    str     r7, [sp, #0]
    ldr     r11, [r12, #24]
    cmp     r11, #0
    beq.w   .Lmap8_0
    cmp     r11, #2
    beq.w   .Lmap8_4
    cmp     r11, #4
    beq.w   .Lmap8_8
    cmp     r11, #6
    beq.w   .Lmap8_12
    cmp     r11, #8
    beq.w   .Lmap8_16
    cmp     r11, #10
    beq.w   .Lmap8_20
    cmp     r11, #12
    beq.w   .Lmap8_24
    b.w     .Lmap8_28

    // r0-r3 retain the four source pointers; r4 is LUT, r5/r6 advance
    // through the contiguous I/Q interval, r8 is Q carry. I carry lives
    // in one stack word to provide the six temporaries needed for mapping.
    // Keeping both carries in registers instead spills the three upper
    // bitplanes, increasing the inner-loop memory traffic.
    .macro map8_input
    ldr     r7, [r0], #4
    ldr     r9, [r1], #4
    ldr     r10, [r2], #4
    rbit    r7, r7
    rbit    r9, r9
    rbit    r10, r10
    ldr     r11, [r3], #4
    ldr     r12, [r3], #4
    eor     lr, r9, r10
    eor     lr, lr, r11
    and     lr, lr, r7
    eor     lr, lr, r12
    eor     r9, r9, lr
    eor     r10, r10, lr
    eor     r10, r10, r11
    eor     r7, r7, r11
    .endm

    // Leave Q in r11 and I in r12. Source bitplanes r9=qa, r10=qb,
    // r7=d survive both halves. Independent lookup loads overlap as far
    // as the three temporary registers permit.
    .macro map8_codes byte0, byte1
    ubfx    r11, r9, #\byte0, #8
    ubfx    r12, r9, #\byte1, #8
    ubfx    lr, r10, #\byte0, #8
    ldrh    r11, [r4, r11, lsl #1]
    ldrh    r12, [r4, r12, lsl #1]
    ldrh    lr, [r4, lr, lsl #1]
    orr     r11, r11, r12, lsl #16
    ubfx    r12, r10, #\byte1, #8
    orr     r11, r11, lr, lsl #1
    ldrh    lr, [r4, r12, lsl #1]
    ubfx    r12, r7, #\byte0, #8
    ldrh    r12, [r4, r12, lsl #1]
    orr     r11, r11, lr, lsl #17
    ubfx    lr, r7, #\byte1, #8
    ldrh    lr, [r4, lr, lsl #1]
    orr     r12, r12, lr, lsl #16
    .endm

    .macro map8_finish
    eor     r12, r12, r12, lsl #1
    eor     r12, r12, r11
    eor     r12, r12, #0xaaaaaaaa
    .endm

    .macro map8_put shift, carry
    orr     lr, \carry, r12, lsl #\shift
    str     lr, [r5], #4
    lsr     lr, r12, #(32-\shift)
    str     lr, [sp, #0]
    orr     lr, r8, r11, lsl #\shift
    str     lr, [r6], #4
    lsr     r8, r11, #(32-\shift)
    .endm

    // Eight copies buy immediate packing shifts and remove two permanent
    // shift registers. They share the 512-byte LUT and add no static SRAM.
    .macro map8_run shift
.Lmap8_\shift:
    map8_input
    map8_codes 0, 8
    map8_finish
    ldr     lr, [sp, #0]
    map8_put \shift, lr
    map8_codes 16, 24
    // All qa bytes have now been consumed. Reuse r9 to preload I carry,
    // hiding both the complex d-join result and this load's use latency.
    ldr     r9, [sp, #0]
    map8_finish
    map8_put \shift, r9
    ldr     lr, [sp, #4]
    cmp     r0, lr
    bne.w   .Lmap8_\shift
    b.w     .Lmap8_done
    .endm

    map8_run 0
    map8_run 4
    map8_run 8
    map8_run 12
    map8_run 16
    map8_run 20
    map8_run 24
    map8_run 28

.Lmap8_done:
    ldr     r12, [sp, #8]
    ldr     r7, [sp, #0]
    ldr     r11, [sp, #12]
    str     r7, [r12, #16]
    str     r8, [r12, #20]
    str     r11, [r12, #12]
.Lmap8_return:
    add     sp, #28
    pop     {r4-r11, pc}
    .size dvbs2_map8_c6, .-dvbs2_map8_c6
#endif
