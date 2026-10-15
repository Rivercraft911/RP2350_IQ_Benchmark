// L8 old3 contributes nothing to the fourth output symbol. Keep that lane in
// r7 from the middle row, and omit both its old-row load and its zero add.
    .syntax unified
    .thumb
    .text
    .p2align 2
    .macro AXIS shift, offset, source
    ldr r4,[r9,#\source+-8]
    ubfx r4,r4,#\shift+3,#9
    add r4,r11,r4,lsl #4
    ldm r4,{r0-r2}
    ldr r4,[r9,#\source+-4]
    ubfx r4,r4,#\shift,#12
    add r4,r12,r4,lsl #4
    ldm r4,{r4-r7}
    sadd16 r0,r0,r4
    sadd16 r1,r1,r5
    sadd16 r2,r2,r6
    ldr r4,[r9,#\source]
    ubfx r4,r4,#\shift,#12
    add r4,lr,r4,lsl #4
    ldm r4,{r3-r6}
    sadd16 r0,r0,r3
    sadd16 r1,r1,r4
    sadd16 r2,r2,r5
    sadd16 r7,r7,r6
    str r0,[r8,#\offset+0]
    str r1,[r8,#\offset+8]
    str r2,[r8,#\offset+16]
    str r7,[r8,#\offset+24]
    .endm

    .global iq16fast_sparse_asm
    .type iq16fast_sparse_asm,%function
    .thumb_func
iq16fast_sparse_asm:
    cmp r2,#0
    beq 3f
    push {r4-r11,lr}
    sub sp,#12
    mov r8,r0
    mov r9,r1
    mov r10,r2
    ldr r4,[sp,#48]
    str r4,[sp,#0]
    movs r5,#2
    cmp r10,#2
    it lo
    movlo r5,r10
    mov r2,r5
    bl iq16native8_kernel_c
    add r8,r8,r5,lsl #5
    add r9,r9,r5,lsl #2
    subs r10,r10,r5
    beq 2f
    ldm r4,{r11,r12,lr}
1:
    AXIS 0,0,0
    AXIS 16,4,0
    add r9,#4
    add r8,#32
    subs r10,#1
    bne 1b
2:  add sp,#12
    pop {r4-r11,pc}
3:  bx lr
    .size iq16fast_sparse_asm,.-iq16fast_sparse_asm

    .p2align 2
    .global iq16fast_pair_asm
    .type iq16fast_pair_asm,%function
    .thumb_func
iq16fast_pair_asm:
    cmp r2,#0
    beq 6f
    push {r4-r11,lr}
    sub sp,#12
    mov r8,r0
    mov r9,r1
    mov r10,r2
    ldr r4,[sp,#48]
    str r4,[sp,#0]
    ubfx r5,r10,#0,#1
    add r5,#2
    cmp r10,r5
    it lo
    movlo r5,r10
    mov r2,r5
    bl iq16native8_kernel_c
    add r8,r8,r5,lsl #5
    add r9,r9,r5,lsl #2
    subs r10,r10,r5
    beq 5f
    ldm r4,{r11,r12,lr}
4:
    AXIS 0,0,0
    AXIS 16,4,0
    AXIS 0,32,4
    AXIS 16,36,4
    add r9,#8
    add r8,#64
    subs r10,#2
    bne 4b
5:  add sp,#12
    pop {r4-r11,pc}
6:  bx lr
    .size iq16fast_pair_asm,.-iq16fast_pair_asm
    .purgem AXIS
