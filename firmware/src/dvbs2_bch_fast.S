// C7 position-independent BCH continuation leaves. All branches stay within the
// copied function; there are no literals, external references or mutable state.
// r0=input, r1=six-word remainder, r2=table, r3=input word count. Byte tails are
// handled by the existing caller. The table3 layout is described below.
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
    .syntax unified
    .thumb

// Two adjacent words from each byte table. LDM still takes one cycle/register
// on M33, but reduces instruction fetches without increasing table traffic.
// The first XOR separates the last loaded register (lr) from its consumer.
    .macro BCH_PAIR ptr, dst0, dst1, old0, old1
    ldmia   \ptr!, {r1, lr}
    eor     \dst0, \old0, r1
    eor     \dst1, \old1, lr
    .endm
    .macro BCH_PAIR4 dst0, dst1, old0, old1
    BCH_PAIR r9, \dst0, \dst1, \old0, \old1
    BCH_PAIR r10, \dst0, \dst1, \dst0, \dst1
    BCH_PAIR r11, \dst0, \dst1, \dst0, \dst1
    BCH_PAIR r12, \dst0, \dst1, \dst0, \dst1
    .endm

    .macro BCH_BYTE_LEAF name, words
    .section .text.\name, "ax", %progbits
    .align 2
    .global \name
    .global \name\()_end
    .type \name, %function
    .thumb_func
\name:
    push    {r4-r11, lr}
    push    {r1}
    .if \words == 4
    ldmia   r1, {r4-r7}
    // The final paired update shifts in a zero word at degree128.
    movs    r8, #0
    .else
    ldmia   r1, {r4-r8}
    .endif
    cmp     r3, #0
    beq     .Lbyte_store\@
    .balign 4
.Lbyte_word\@:
    ldr     lr, [r0], #4
    subs    r3, r3, #1
    eor     r4, r4, lr
    ubfx    r9, r4, #24, #8
    add     r9, r2, r9, lsl #5
    add     r9, r9, #24576
    ubfx    r10, r4, #16, #8
    add     r10, r2, r10, lsl #5
    add     r10, r10, #16384
    ubfx    r11, r4, #8, #8
    add     r11, r2, r11, lsl #5
    add     r11, r11, #8192
    uxtb    r12, r4
    add     r12, r2, r12, lsl #5
    BCH_PAIR4 r4, r5, r5, r6
    BCH_PAIR4 r6, r7, r7, r8
    .if \words == 5
    ldr     r8, [r9]
    ldr     lr, [r10]
    ldr     r1, [r11]
    eor     r8, r8, lr
    ldr     lr, [r12]
    eor     r8, r8, r1
    eor     r8, r8, lr
    .endif
    bne     .Lbyte_word\@
.Lbyte_store\@:
    pop     {r0}
    .if \words == 4
    stmia   r0!, {r4-r7}
    .else
    stmia   r0!, {r4-r8}
    .endif
    movs    r1, #0
    str     r1, [r0]
    .if \words == 4
    str     r1, [r0, #4]
    .endif
    pop     {r4-r11, pc}
    .size \name, .-\name
\name\()_end:
    .endm

    BCH_BYTE_LEAF dvbs2_bch128_fast_pic, 4
    BCH_BYTE_LEAF dvbs2_bch160_fast_pic, 5
    .purgem BCH_BYTE_LEAF
    .purgem BCH_PAIR4

// t12 has six remainder registers. Its fourth row replaces r2; preserve the
// table base in the same single stack slot as the existing scratch-Y leaf.
    .section .text.dvbs2_bch192_fast_pic, "ax", %progbits
    .align 2
    .global dvbs2_bch192_fast_pic
    .global dvbs2_bch192_fast_pic_end
    .type dvbs2_bch192_fast_pic, %function
    .thumb_func
dvbs2_bch192_fast_pic:
    push    {r4-r11, lr}
    push    {r1-r3}
    ldmia   r1, {r4-r9}
    cmp     r3, #0
    beq     .Lbyte192_store
    .balign 4
.Lbyte192_word:
    ldr     lr, [r0], #4
    ldr     r2, [sp, #4]
    eor     r4, r4, lr
    ubfx    r10, r4, #24, #8
    add     r10, r2, r10, lsl #5
    add     r10, r10, #24576
    ubfx    r11, r4, #16, #8
    add     r11, r2, r11, lsl #5
    add     r11, r11, #16384
    ubfx    r12, r4, #8, #8
    add     r12, r2, r12, lsl #5
    add     r12, r12, #8192
    uxtb    r1, r4
    add     r2, r2, r1, lsl #5
    BCH_PAIR r10, r4, r5, r5, r6
    BCH_PAIR r11, r4, r5, r4, r5
    BCH_PAIR r12, r4, r5, r4, r5
    BCH_PAIR r2, r4, r5, r4, r5
    BCH_PAIR r10, r6, r7, r7, r8
    BCH_PAIR r11, r6, r7, r6, r7
    BCH_PAIR r12, r6, r7, r6, r7
    BCH_PAIR r2, r6, r7, r6, r7
    // No old word follows the last remainder word.
    ldmia   r10!, {r1, lr}
    eor     r8, r9, r1
    mov     r9, lr
    BCH_PAIR r11, r8, r9, r8, r9
    BCH_PAIR r12, r8, r9, r8, r9
    BCH_PAIR r2, r8, r9, r8, r9
    subs    r3, r3, #1
    bne     .Lbyte192_word
.Lbyte192_store:
    pop     {r0-r2}
    stmia   r0, {r4-r9}
    pop     {r4-r11, pc}
    .size dvbs2_bch192_fast_pic, .-dvbs2_bch192_fast_pic
dvbs2_bch192_fast_pic_end:
    .purgem BCH_PAIR

// Three planes: 2048 entries indexed by x[31:21], 2048 by x[20:10],
// then 1024 by x[9:0]. Rows hold only the 4/5/6 live remainder words.
// Each entry is (index << input_shift) * x^degree modulo the BCH generator.
// Sizes: degree128=81920 B, degree160=102400 B, degree192=122880 B.

    .section .text.dvbs2_bch128_table3_pic, "ax", %progbits
    .align 2
    .global dvbs2_bch128_table3_pic
    .global dvbs2_bch128_table3_pic_end
    .type dvbs2_bch128_table3_pic, %function
    .thumb_func
dvbs2_bch128_table3_pic:
    push    {r4-r11, lr}
    push    {r1}
    ldmia   r1, {r4-r7}
    cmp     r3, #0
    beq     .Ltable128_store
    .balign 4
.Ltable128_word:
    ldr     lr, [r0], #4
    subs    r3, r3, #1
    eor     r8, r4, lr
    ubfx    r1, r8, #21, #11
    add     r1, r2, r1, lsl #4
    ldmia   r1, {r9-r12}
    eor     r4, r5, r9
    eor     r5, r6, r10
    ubfx    r1, r8, #10, #11
    add     r1, r2, r1, lsl #4
    eor     r6, r7, r11
    add     r1, r1, #32768
    mov     r7, r12
    ldmia   r1, {r9-r12}
    eor     r4, r4, r9
    eor     r5, r5, r10
    ubfx    r1, r8, #0, #10
    add     r1, r2, r1, lsl #4
    eor     r6, r6, r11
    add     r1, r1, #65536
    eor     r7, r7, r12
    ldmia   r1, {r9-r12}
    eor     r4, r4, r9
    eor     r5, r5, r10
    eor     r6, r6, r11
    eor     r7, r7, r12
    bne     .Ltable128_word
.Ltable128_store:
    pop     {r0}
    stmia   r0!, {r4-r7}
    movs    r1, #0
    str     r1, [r0]
    str     r1, [r0, #4]
    pop     {r4-r11, pc}
    .size dvbs2_bch128_table3_pic, .-dvbs2_bch128_table3_pic
dvbs2_bch128_table3_pic_end:

    .section .text.dvbs2_bch160_table3_pic, "ax", %progbits
    .align 2
    .global dvbs2_bch160_table3_pic
    .global dvbs2_bch160_table3_pic_end
    .type dvbs2_bch160_table3_pic, %function
    .thumb_func
dvbs2_bch160_table3_pic:
    push    {r4-r11, lr}
    push    {r1}
    ldmia   r1, {r4-r8}
    cmp     r3, #0
    beq     .Ltable160_store
    .balign 4
.Ltable160_word:
    ldr     lr, [r0], #4
    subs    r3, r3, #1
    eor     r9, r4, lr
    ubfx    r1, r9, #21, #11
    add     r1, r1, r1, lsl #2
    add     r1, r2, r1, lsl #2
    ldmia   r1, {r1, r10-r12, lr}
    eor     r4, r5, r1
    eor     r5, r6, r10
    eor     r6, r7, r11
    eor     r7, r8, r12
    ubfx    r1, r9, #10, #11
    add     r1, r1, r1, lsl #2
    add     r1, r2, r1, lsl #2
    add     r1, r1, #40960
    mov     r8, lr
    ldmia   r1, {r1, r10-r12, lr}
    eor     r4, r4, r1
    eor     r5, r5, r10
    eor     r6, r6, r11
    eor     r7, r7, r12
    ubfx    r1, r9, #0, #10
    add     r1, r1, r1, lsl #2
    add     r1, r2, r1, lsl #2
    add     r1, r1, #81920
    eor     r8, r8, lr
    ldmia   r1, {r1, r10-r12, lr}
    eor     r4, r4, r1
    eor     r5, r5, r10
    eor     r6, r6, r11
    eor     r7, r7, r12
    eor     r8, r8, lr
    bne     .Ltable160_word
.Ltable160_store:
    pop     {r0}
    stmia   r0!, {r4-r8}
    movs    r1, #0
    str     r1, [r0]
    pop     {r4-r11, pc}
    .size dvbs2_bch160_table3_pic, .-dvbs2_bch160_table3_pic
dvbs2_bch160_table3_pic_end:

    .section .text.dvbs2_bch192_table3_pic, "ax", %progbits
    .align 2
    .global dvbs2_bch192_table3_pic
    .global dvbs2_bch192_table3_pic_end
    .type dvbs2_bch192_table3_pic, %function
    .thumb_func
dvbs2_bch192_table3_pic:
    push    {r4-r11, lr}
    add     r3, r0, r3, lsl #2
    push    {r1-r3}
    ldmia   r1, {r4-r9}
    cmp     r0, r3
    beq     .Ltable192_store
    .balign 4
.Ltable192_word:
    ldr     lr, [r0], #4
    ldr     r2, [sp, #4]
    eor     r10, r4, lr
    ubfx    r1, r10, #21, #11
    add     r1, r1, r1, lsl #1
    add     r1, r2, r1, lsl #3
    ldmia   r1, {r1-r3, r11-r12, lr}
    eor     r4, r5, r1
    eor     r5, r6, r2
    eor     r6, r7, r3
    eor     r7, r8, r11
    eor     r8, r9, r12
    ldr     r2, [sp, #4]
    ubfx    r1, r10, #10, #11
    add     r1, r1, r1, lsl #1
    add     r1, r2, r1, lsl #3
    add     r1, r1, #49152
    mov     r9, lr
    ldmia   r1, {r1-r3, r11-r12, lr}
    eor     r4, r4, r1
    eor     r5, r5, r2
    eor     r6, r6, r3
    eor     r7, r7, r11
    eor     r8, r8, r12
    ldr     r2, [sp, #4]
    ubfx    r1, r10, #0, #10
    add     r1, r1, r1, lsl #1
    add     r1, r2, r1, lsl #3
    add     r1, r1, #98304
    eor     r9, r9, lr
    ldmia   r1, {r1-r3, r11-r12, lr}
    eor     r4, r4, r1
    eor     r5, r5, r2
    eor     r6, r6, r3
    ldr     r3, [sp, #8]
    eor     r7, r7, r11
    eor     r8, r8, r12
    eor     r9, r9, lr
    cmp     r0, r3
    bne     .Ltable192_word
.Ltable192_store:
    pop     {r0-r2}
    stmia   r0, {r4-r9}
    pop     {r4-r11, pc}
    .size dvbs2_bch192_table3_pic, .-dvbs2_bch192_table3_pic
dvbs2_bch192_table3_pic_end:
#endif
