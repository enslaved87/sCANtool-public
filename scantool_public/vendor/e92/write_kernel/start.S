/* Same handoff rule as the read kernel: do not rewrite r1. */

    .section .text.entry, "ax"
    .globl  _start
    .type   _start, @function

_start:
    lis     %r3, __bss_beg@h
    ori     %r3, %r3, __bss_beg@l
    lis     %r4, __bss_end@h
    ori     %r4, %r4, __bss_end@l
    li      %r0, 0
.Lzero:
    cmpw    %r3, %r4
    bge     .Lgo
    stw     %r0, 0(%r3)
    addi    %r3, %r3, 4
    b       .Lzero
.Lgo:
    bl      scpb_write_main
.Lpark:
    b       .Lpark
    .size   _start, . - _start
