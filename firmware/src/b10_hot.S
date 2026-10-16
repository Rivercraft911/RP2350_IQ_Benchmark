// B9-only bulk GCS copy. The caller's 512-byte pattern repeats after 256
// bytes; a slice of <=64 words cannot run past that duplicated reservation.
// No global addresses, external branches, or literal pool: safe to copy.
    .syntax unified
    .cpu cortex-m33
    .thumb
    .section .text.dvbs2_gcs_words_pic, "ax", %progbits
    .align 2
    .global dvbs2_gcs_words_pic
    .global dvbs2_gcs_words_pic_end
    .type dvbs2_gcs_words_pic, %function
    .thumb_func
dvbs2_gcs_words_pic:
    push    {r4, r5}
    cmp     r3, #0
    beq     .Lgcs_done
    .balign 4
.Lgcs_word:
    ldr     r4, [r1], #4
    ldr     r5, [r2], #4
    rev     r4, r4
    eors    r4, r5
    str     r4, [r0], #4
    subs    r3, #1
    bne     .Lgcs_word
.Lgcs_done:
    pop     {r4, r5}
    bx      lr
    .size dvbs2_gcs_words_pic, .-dvbs2_gcs_words_pic
dvbs2_gcs_words_pic_end:
