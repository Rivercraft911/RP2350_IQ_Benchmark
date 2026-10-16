// Byte offsets fit16-bit lanes: UXT(A)H and shifted ADD form each row address.
    .syntax unified
    .thumb
    .text
    .p2align 2
    .global iq32offsets_kernel_asm
    .type iq32offsets_kernel_asm,%function
    .thumb_func
iq32offsets_kernel_asm:
    cmp r2,#0
    beq 3f
    push {r4-r11,lr}
    sub sp,#12
    mov r8,r0
    mov r9,r1
    mov r10,r2
    ldr r4,[sp,#48]
    str r4,[sp,#0]
    movs r5,#3
    cmp r10,#3
    it lo
    movlo r5,r10
    mov r2,r5
    bl iq32offsets_kernel_c
    add r8,r8,r5,lsl #4
    add r9,r9,r5,lsl #3
    subs r10,r10,r5
    beq 2f
    ldm r4,{r11,r12,lr}
5:
    ldr r0,[r9,#-20]
    ldr r1,[r9,#-8]
    ldr r2,[r9,#4]
    add r6,r11,r0,lsr #16
    uxtah r0,r11,r0
    ldm r0,{r3,r5}
    ldm r6,{r4,r6}
    uxtah r0,r12,r1
    add r1,r12,r1,lsr #16
    ldm r0,{r0,r7}
    sadd16 r3,r3,r0
    sadd16 r5,r5,r7
    ldm r1,{r0,r7}
    sadd16 r4,r4,r0
    sadd16 r6,r6,r7
    uxtah r0,lr,r2
    add r2,lr,r2,lsr #16
    ldm r0,{r0,r7}
    sadd16 r3,r3,r0
    sadd16 r5,r5,r7
    ldm r2,{r0,r7}
    sadd16 r4,r4,r0
    sadd16 r6,r6,r7
    stm r8!,{r3-r6}
    add r9,#8
    subs r10,#1
    bne 5b
2:  add sp,#12
    pop {r4-r11,pc}
3:  bx lr
    .size iq32offsets_kernel_asm,.-iq32offsets_kernel_asm
