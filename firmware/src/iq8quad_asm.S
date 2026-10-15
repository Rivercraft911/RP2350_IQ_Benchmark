// Isolated fused 8PSK shaper. Native tests do not execute this Arm leaf.
// void iq8quad_kernel_interp_asm(uint32_t*out,const iq8_input_t*in,uint32_t groups,
//                      const iq8_input_t*prev);
// Each group has 16 symbols; input/prev use iq8.h's 8-byte group format.
// Output is PAIRS without masking or flags. PIO discards upper bits and supplies IQ.
// Both core-local interpolators must be exclusively owned and configured by caller:
// lane0 SHIFT4 MASK4..15 CROSS_INPUT; lane1 SHIFT4 MASK0..31;
// BASE0 points to the respective A/B 64 KiB table; BASE1=0, no other flags.
// The caller configures and restores peripheral state.
    .syntax unified
    .thumb
    .section .scratch_y.iq8quad_asm, "ax"
    .p2align 2
    .macro QUAD2AXIS
    ldr r3,[r1,#20]                    // interpA POP0 => rowpointer
    ldr lr,[r2,#20]                    // interpB POP0 => rowpointer
    ldm r3,{r4-r7}
    ldm lr,{r8-r11}
    sadd16 r4,r4,r8
    sadd16 r5,r5,r9
    sadd16 r6,r6,r10
    sadd16 r7,r7,r11
    str r4,[r0,#0]                     // everyotherword inPAIRS
    str r5,[r0,#8]
    str r6,[r0,#16]
    str r7,[r0,#24]
    add r0,#32
    .endm
    .global iq8quad_kernel_interp_asm
    .type iq8quad_kernel_interp_asm,%function
    .thumb_func
iq8quad_kernel_interp_asm:
    push {r4-r11,lr}
    sub sp,#24
    add r2,r1,r2,lsl #3
    strd r1,r2,[sp,#0]                 // in,end
    cbz r3,3f
    ldm r3,{r4,r5}
    b 4f
3:  movs r4,#0
    movs r5,#0
    movt r5,#0xffff
4:  strd r4,r5,[sp,#8]                 // prevA,prevB
    movs r1,#0x80
    movt r1,#0xd000
    add r2,r1,#0x40
1:  ldrd r3,r12,[sp,#0]
    cmp r3,r12
    beq 2f
    ldm r3!,{r4,r5}                    // curA,curB
    str r3,[sp,#0]
    ldrd r6,r7,[sp,#8]
    strd r4,r5,[sp,#8]
    pkhbt r8,r6,r4,lsl #16             // I-A
    pkhbt r9,r7,r5,lsl #16             // I-B
    pkhtb r10,r4,r6,asr #16            // Q-A
    pkhtb r11,r5,r7,asr #16            // Q-B
    strd r10,r11,[sp,#16]
    str r8,[r1,#4]
    str r9,[r2,#4]
    .rept 4
    QUAD2AXIS
    .endr
    ldrd r3,r12,[sp,#16]
    str r3,[r1,#4]
    str r12,[r2,#4]
    sub r0,#124                        // originalout+4 => Q half ofPAIRS
    .rept 4
    QUAD2AXIS
    .endr
    sub r0,#4                          // originalout+128 => next16symbolgroup
    b 1b
2:  add sp,#24
    pop {r4-r11,pc}
    .size iq8quad_kernel_interp_asm,.-iq8quad_kernel_interp_asm
    .purgem QUAD2AXIS
