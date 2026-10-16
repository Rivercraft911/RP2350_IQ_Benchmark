// C5 reentrant LDPC packing leaf. Copy the complete [pic,end) extent into a
// caller-owned SRAM arena; there are no literal pools or external branches.
// Prefix application is fused with gathering, and the common q=60/q=30 gather
// also performs the first two transpose stages before writing the local tile.
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
    .syntax unified
    .thumb

    .macro C5_SWAP a, b, shift, mask
    eor     lr, \a, \b, lsr #\shift
    and     lr, lr, \mask
    eor     \a, \a, lr
    eor     \b, \b, lr, lsl #\shift
    .endm
    .macro C5_LOAD4
    ldr     r4, [r0]
    ldr     r5, [r0, #384]
    ldr     r6, [r0, #768]
    ldr     r7, [r0, #1152]
    .endm
    .macro C5_LOAD3
    ldr     r4, [r0]
    ldr     r5, [r0, #384]
    ldr     r6, [r0, #768]
    mov     r7, r2
    .endm
    .macro C5_GATHER_WIDE
    eor     r4, r4, r2
    eor     r5, r5, r2
    eor     r6, r6, r2
    eor     r7, r7, r2
    mov     r8, r4
    pkhtb   r4, r4, r6, asr #16
    pkhbt   r6, r6, r8, lsl #16
    mov     r8, r5
    pkhtb   r5, r5, r7, asr #16
    pkhbt   r7, r7, r8, lsl #16
    C5_SWAP r4, r5, 8, r12
    C5_SWAP r6, r7, 8, r12
    str     r4, [r1]
    str     r5, [r1, #32]
    str     r6, [r1, #64]
    str     r7, [r1, #96]
    adds    r0, #48
    adds    r1, #4
    subs    r3, #1
    .endm
    .macro C5_PACK60 j
    ldr     r5, [r0, #(4*\j)]
    ldr     r6, [r1, #(4*\j)]
    orr     r7, r4, r5, lsr #(32-4*\j)
    lsl     r8, r5, #(4*\j)
    orr     r8, r8, r6, lsr #(32-4*\j)
    str     r7, [r2], #4
    str     r8, [r2], #4
    lsl     r4, r6, #(4*\j)
    .endm
    .macro C5_PACK30_HEAD j
    ldr     r5, [r0, #(4*\j)]
    orr     r6, r4, r5, lsr #(16-2*\j)
    lsl     r4, r5, #(16+2*\j)
    str     r6, [r2], #4
    .endm
    .macro C5_PACK30_TAIL j
    ldr     r5, [r0, #(4*\j)]
    orr     r6, r4, r5, lsr #(48-2*\j)
    lsl     r4, r5, #(2*\j-16)
    str     r6, [r2], #4
    .endm

    .section .text.dvbs2_ldpc_pack_c5_pic, "ax", %progbits
    .align 2
    .global dvbs2_ldpc_pack_c5_pic
    .global dvbs2_ldpc_pack_c5_pic_end
    .type dvbs2_ldpc_pack_c5_pic, %function
    .thumb_func
// void pack(const uint32_t rows[][12], uint32_t *dst,
//           const uint32_t prefix[12], uint32_t q, uint32_t k,
//           uint32_t io[3]); io = {acc, fill, written}. 1<=q<=64, k<12.
// One tile (at most 32 parity columns) is one bounded reentrant LDPC unit.
// Main saves 320 bytes; a private transpose call adds four bytes (324 maximum).
dvbs2_ldpc_pack_c5_pic:
.Lc5_pack_entry:
    push    {r4-r11, lr}
    sub     sp, #284
    ldr     r4, [sp, #320]
    ldr     r5, [sp, #324]
    str     r1, [sp, #260]
    str     r3, [sp, #264]
    str     r4, [sp, #268]
    str     r5, [sp, #272]
    add     r0, r0, r4, lsl #2
    str     r0, [sp, #256]
    ldr     r2, [r2, r4, lsl #2]
    str     r2, [sp, #276]
    mov     r1, sp
    mov.w   r12, #0x00ff00ff
    cmp     r3, #60
    beq     .Lc5_gather60
    cmp     r3, #30
    beq     .Lc5_gather30

    mov     r4, r3
    mov     r5, r3
.Lc5_gather_generic:
    ldr     r6, [r0]
    eor     r6, r6, r2
    str     r6, [r1], #4
    adds    r0, #48
    subs    r4, #1
    bne     .Lc5_gather_generic
    movs    r4, #32
    cmp     r5, #32
    bls     .Lc5_pad_count
    movs    r4, #64
.Lc5_pad_count:
    sub     r4, r4, r5
    movs    r6, #0
    cmp     r4, #0
    beq     .Lc5_transpose_generic
.Lc5_pad:
    str     r6, [r1], #4
    subs    r4, #1
    bne     .Lc5_pad
.Lc5_transpose_generic:
    mov     r0, sp
    bl      .Lc5_transpose_full
    ldr     r3, [sp, #264]
    cmp     r3, #32
    bls     .Lc5_pack
    add     r0, sp, #128
    bl      .Lc5_transpose_full
    b       .Lc5_pack

.Lc5_gather60:
    movs    r3, #8
.Lc5_gather60_full:
    C5_LOAD4
    C5_GATHER_WIDE
    bne     .Lc5_gather60_full
    ldr     r0, [sp, #256]
    add     r0, #1536
    add     r1, sp, #128
    movs    r3, #4
.Lc5_gather60_tail4:
    C5_LOAD4
    C5_GATHER_WIDE
    bne     .Lc5_gather60_tail4
    movs    r3, #4
.Lc5_gather60_tail3:
    C5_LOAD3
    C5_GATHER_WIDE
    bne     .Lc5_gather60_tail3
    mov     r0, sp
    bl      .Lc5_transpose_low
    add     r0, sp, #128
    bl      .Lc5_transpose_low
    b       .Lc5_pack

.Lc5_gather30:
    movs    r3, #6
.Lc5_gather30_full:
    C5_LOAD4
    C5_GATHER_WIDE
    bne     .Lc5_gather30_full
    movs    r3, #2
.Lc5_gather30_tail:
    C5_LOAD3
    C5_GATHER_WIDE
    bne     .Lc5_gather30_tail
    mov     r0, sp
    bl      .Lc5_transpose_low

.Lc5_pack:
    ldr     r11, [sp, #272]
    ldr     r10, [sp, #260]
    ldr     r2, [r11, #8]
    add     r2, r10, r2, lsl #2
    ldr     r7, [sp, #264]
    ldr     r9, [sp, #268]
    movs    r3, #32
    cmp     r9, #11
    bne     .Lc5_columns_ready
    movs    r3, #8
.Lc5_columns_ready:
    mov     r0, sp
    add     r1, sp, #128
    ldr     r4, [r11]
    ldr     r5, [r11, #4]
    cmp     r7, #60
    bne     .Lc5_try_pack30
    cmp     r5, #0
    beq     .Lc5_pack60
.Lc5_try_pack30:
    cmp     r7, #30
    bne     .Lc5_generic_setup
    cmp     r5, #16
    beq     .Lc5_pack30
.Lc5_generic_setup:
    mov     r6, r7
    cmp     r7, #32
    bls     .Lc5_generic_column
    sub     r6, #32
.Lc5_generic_column:
    cmp     r7, #32
    bls     .Lc5_generic_append_first
    ldr     r8, [r0], #4
    lsr     r9, r8, r5
    orr     r9, r9, r4
    str     r9, [r2], #4
    rsb     r10, r5, #32
    lsl     r4, r8, r10
    ldr     r8, [r1], #4
    b       .Lc5_generic_append
.Lc5_generic_append_first:
    ldr     r8, [r0], #4
.Lc5_generic_append:
    lsr     r9, r8, r5
    orr     r4, r4, r9
    add     r5, r5, r6
    cmp     r5, #32
    blo     .Lc5_generic_next
    str     r4, [r2], #4
    sub     r5, #32
    sub     r10, r6, r5
    lsl     r4, r8, r10
.Lc5_generic_next:
    subs    r3, #1
    bne     .Lc5_generic_column
    str     r4, [r11]
    str     r5, [r11, #4]
    b       .Lc5_written

.Lc5_pack60:
    lsrs    r3, r3, #3
.Lc5_pack60_chunk:
    ldr     r5, [r0]
    ldr     r4, [r1]
    str     r5, [r2], #4
    C5_PACK60 1
    C5_PACK60 2
    C5_PACK60 3
    C5_PACK60 4
    C5_PACK60 5
    C5_PACK60 6
    C5_PACK60 7
    adds    r0, #32
    adds    r1, #32
    subs    r3, #1
    bne     .Lc5_pack60_chunk
    b       .Lc5_written

.Lc5_pack30:
    C5_PACK30_HEAD 0
    C5_PACK30_HEAD 1
    C5_PACK30_HEAD 2
    C5_PACK30_HEAD 3
    C5_PACK30_HEAD 4
    C5_PACK30_HEAD 5
    C5_PACK30_HEAD 6
    C5_PACK30_HEAD 7
    cmp     r3, #8
    beq     .Lc5_pack30_last
    ldr     r4, [r0, #32]
    C5_PACK30_TAIL 9
    C5_PACK30_TAIL 10
    C5_PACK30_TAIL 11
    C5_PACK30_TAIL 12
    C5_PACK30_TAIL 13
    C5_PACK30_TAIL 14
    C5_PACK30_TAIL 15
    adds    r0, #64
    subs    r3, #16
    bne     .Lc5_pack30
    str     r4, [r11]
    b       .Lc5_written
.Lc5_pack30_last:
    movs    r4, #0
    str     r4, [r11]
    str     r4, [r11, #4]
.Lc5_written:
    ldr     r10, [sp, #260]
    sub     r2, r2, r10
    lsrs    r2, r2, #2
    str     r2, [r11, #8]
    add     sp, #284
    pop     {r4-r11, pc}

// Private transpose entries deliberately preserve no registers. They share
// the original exact permutation, but execute beside the caller in its arena.
.Lc5_transpose_full:
    push    {lr}
    movs    r1, #8
    mov.w   r3, #0x00ff00ff
.Lc5_transpose_wide:
    ldr     r4, [r0]
    ldr     r5, [r0, #32]
    ldr     r6, [r0, #64]
    ldr     r7, [r0, #96]
    mov     r8, r4
    pkhtb   r4, r4, r6, asr #16
    pkhbt   r6, r6, r8, lsl #16
    mov     r8, r5
    pkhtb   r5, r5, r7, asr #16
    pkhbt   r7, r7, r8, lsl #16
    C5_SWAP r4, r5, 8, r3
    C5_SWAP r6, r7, 8, r3
    str     r4, [r0]
    str     r5, [r0, #32]
    str     r6, [r0, #64]
    str     r7, [r0, #96]
    adds    r0, #4
    subs    r1, #1
    bne     .Lc5_transpose_wide
    subs    r0, #32
    b       .Lc5_transpose_low_body
.Lc5_transpose_low:
    push    {lr}
.Lc5_transpose_low_body:
    movs    r1, #4
    mov.w   r2, #0x0f0f0f0f
    mov.w   r3, #0x33333333
    mov.w   r12, #0x55555555
.Lc5_transpose_low_tile:
    ldmia   r0, {r4-r11}
    C5_SWAP r4, r8, 4, r2
    C5_SWAP r5, r9, 4, r2
    C5_SWAP r6, r10, 4, r2
    C5_SWAP r7, r11, 4, r2
    C5_SWAP r4, r6, 2, r3
    C5_SWAP r5, r7, 2, r3
    C5_SWAP r8, r10, 2, r3
    C5_SWAP r9, r11, 2, r3
    C5_SWAP r4, r5, 1, r12
    C5_SWAP r6, r7, 1, r12
    C5_SWAP r8, r9, 1, r12
    C5_SWAP r10, r11, 1, r12
    stmia   r0!, {r4-r11}
    subs    r1, #1
    bne     .Lc5_transpose_low_tile
    pop     {pc}
    .size dvbs2_ldpc_pack_c5_pic, .-dvbs2_ldpc_pack_c5_pic
dvbs2_ldpc_pack_c5_pic_end:

// C9 appends a q45-only entry after the unchanged C5 bundle. Mode3 copies the
// larger extent; other q values and alignments tail-call the original entry.
// Normal 3/4 starts each full tile with 24 occupied bits (48600 mod 32).
    .macro C9_PACK45 fill
    ldr     r5, [r0], #4
    ldr     r6, [r1], #4
    .if \fill == 0
    str     r5, [r2], #4
    mov     r4, r6
    .else
    orr     r7, r4, r5, lsr #\fill
    str     r7, [r2], #4
    lsl     r4, r5, #(32-\fill)
    .if \fill >= 19
    orr     r7, r4, r6, lsr #\fill
    str     r7, [r2], #4
    lsl     r4, r6, #(32-\fill)
    .else
    orr     r4, r4, r6, lsr #\fill
    .endif
    .endif
    .endm
    .balign 4
    .global dvbs2_ldpc_pack_c9_pic
    .global dvbs2_ldpc_pack_c9_pic_end
    .type dvbs2_ldpc_pack_c9_pic, %function
    .thumb_func
dvbs2_ldpc_pack_c9_pic:
    cmp     r3, #45
    bne     .Lc5_pack_entry
    ldr     r12, [sp, #4]
    ldr     r12, [r12, #4]
    cmp     r12, #24
    bne     .Lc5_pack_entry
    push    {r4-r11, lr}
    sub     sp, #284
    ldr     r4, [sp, #320]
    ldr     r5, [sp, #324]
    str     r1, [sp, #260]
    str     r4, [sp, #268]
    str     r5, [sp, #272]
    add     r0, r0, r4, lsl #2
    str     r0, [sp, #256]
    ldr     r2, [r2, r4, lsl #2]
    mov     r1, sp
    mov.w   r12, #0x00ff00ff
    movs    r3, #8
.Lc9_gather45_full:
    C5_LOAD4
    C5_GATHER_WIDE
    bne     .Lc9_gather45_full
    ldr     r0, [sp, #256]
    add     r0, #1536
    add     r1, sp, #128
    movs    r3, #5
.Lc9_gather45_tail2:
    ldr     r4, [r0]
    ldr     r5, [r0, #384]
    mov     r6, r2
    mov     r7, r2
    C5_GATHER_WIDE
    bne     .Lc9_gather45_tail2
    movs    r3, #3
.Lc9_gather45_tail1:
    ldr     r4, [r0]
    mov     r5, r2
    mov     r6, r2
    mov     r7, r2
    C5_GATHER_WIDE
    bne     .Lc9_gather45_tail1
    mov     r0, sp
    bl      .Lc5_transpose_low
    add     r0, sp, #128
    bl      .Lc5_transpose_low
    ldr     r11, [sp, #272]
    ldr     r10, [sp, #260]
    ldr     r2, [r11, #8]
    add     r2, r10, r2, lsl #2
    mov     r0, sp
    add     r1, sp, #128
    ldr     r4, [r11]
    C9_PACK45 24
    C9_PACK45 5
    C9_PACK45 18
    C9_PACK45 31
    C9_PACK45 12
    C9_PACK45 25
    C9_PACK45 6
    C9_PACK45 19
    ldr     r9, [sp, #268]
    cmp     r9, #11
    beq     .Lc9_pack45_last
    C9_PACK45 0
    C9_PACK45 13
    C9_PACK45 26
    C9_PACK45 7
    C9_PACK45 20
    C9_PACK45 1
    C9_PACK45 14
    C9_PACK45 27
    C9_PACK45 8
    C9_PACK45 21
    C9_PACK45 2
    C9_PACK45 15
    C9_PACK45 28
    C9_PACK45 9
    C9_PACK45 22
    C9_PACK45 3
    C9_PACK45 16
    C9_PACK45 29
    C9_PACK45 10
    C9_PACK45 23
    C9_PACK45 4
    C9_PACK45 17
    C9_PACK45 30
    C9_PACK45 11
    str     r4, [r11]
    b       .Lc5_written
.Lc9_pack45_last:
    movs    r4, #0
    str     r4, [r11]
    str     r4, [r11, #4]
    b       .Lc5_written
    .size dvbs2_ldpc_pack_c9_pic, .-dvbs2_ldpc_pack_c9_pic
dvbs2_ldpc_pack_c9_pic_end:
    .purgem C9_PACK45
    .purgem C5_SWAP
    .purgem C5_LOAD4
    .purgem C5_LOAD3
    .purgem C5_GATHER_WIDE
    .purgem C5_PACK60
    .purgem C5_PACK30_HEAD
    .purgem C5_PACK30_TAIL
#endif
