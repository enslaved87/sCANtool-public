/* sCANtool Public — read-only SRAM kernel for MPC5674F E92.
 *
 * Written from the public UDS / FlexCAN / SIU maps and from bench facts
 * (load address, inherited stack, $23 framing). No flash erase or program.
 *
 * Host handshake:
 *   $1A $BB  ->  $5A $BB 'S' 'C' 'P' 'B'
 *   $23 addr32 len16 -> $63 addr32 data[len]  (ISO-TP)
 *   $11 xx   ->  $51 xx then SIU software reset
 */

#include <stdint.h>
#include "mpc5674_flexcan.h"

#define RX_ID     0x7E0u
#define TX_ID     0x7E8u
#define MB_RX     0u
#define MB_TX     8u
#define SIU_SRCR  ((volatile uint32_t *)0xC3F90010u)
#define SRCR_SSR  0x80000000u

/* Bounce sits past the image so an inherited stack just above .text
 * cannot land inside a 2 KiB .bss copy buffer. */
#define BLK       ((uint8_t *)0x40002000u)
#define BLK_MAX   2048u
#define CTS_SPINS 0x00200000u

__attribute__((used, section(".rodata")))
static const char k_id[] = "SCPB-R2";

static void spin(uint32_t n)
{
    volatile uint32_t i;
    for (i = 0; i < n; i++) {
    }
}

static void mb_idle_all(void)
{
    unsigned i;
    for (i = 0; i < 64u; i++) {
        *cana_mb(i, 0) = 0;
    }
    *cana(CANA_IFLAG1) = 0xFFFFFFFFu;
}

static void leave_halt(void)
{
    /* Only HALT/MDIS are safe outside freeze. SRXDIS is a freeze-mode
     * MCR bit — writing it here is ignored or upsets the module. The
     * bootloader already brought FlexCAN up to upload us. */
    volatile uint32_t *mcr = cana(CANA_MCR);
    uint32_t v = *mcr;
    v &= ~(MCR_HALT | MCR_MDIS);
    *mcr = v;
    {
        uint32_t n;
        for (n = 0; n < 0x10000u; n++) {
            if (((*mcr) & (MCR_FRZACK | MCR_NOTRDY)) == 0u) {
                break;
            }
        }
    }
}

static void arm_rx(void)
{
    *cana_mb(MB_RX, 0) = 0;
    *cana_mb(MB_RX, 4) = (uint32_t)RX_ID << 18;
    *cana_mb(MB_RX, 0) = (uint32_t)MB_CODE_RX_EMPTY << 24;
    *cana(CANA_IFLAG1) = (1u << MB_RX);
}

static int unpack_mb(uint8_t out[8])
{
    uint32_t cs, w0, w1;
    uint8_t dlc;

    cs = *cana_mb(MB_RX, 0);
    w0 = *cana_mb(MB_RX, 8);
    w1 = *cana_mb(MB_RX, 12);
    (void)*cana_mb(MB_RX, 4);
    (void)*cana(CANA_TIMER);

    dlc = (uint8_t)((cs >> 16) & 0xFu);
    if (dlc > 8u) {
        dlc = 8u;
    }
    out[0] = (uint8_t)(w0 >> 24);
    out[1] = (uint8_t)(w0 >> 16);
    out[2] = (uint8_t)(w0 >> 8);
    out[3] = (uint8_t)w0;
    out[4] = (uint8_t)(w1 >> 24);
    out[5] = (uint8_t)(w1 >> 16);
    out[6] = (uint8_t)(w1 >> 8);
    out[7] = (uint8_t)w1;

    *cana(CANA_IFLAG1) = (1u << MB_RX);
    arm_rx();
    return (int)dlc;
}

static uint8_t recv8(uint8_t out[8])
{
    while (((*cana(CANA_IFLAG1)) & (1u << MB_RX)) == 0u) {
    }
    return (uint8_t)unpack_mb(out);
}

static int recv8_to(uint8_t out[8], uint32_t spins)
{
    uint32_t n;
    for (n = 0; n < spins; n++) {
        if ((*cana(CANA_IFLAG1)) & (1u << MB_RX)) {
            return unpack_mb(out);
        }
    }
    return -1;
}

static void send8(const uint8_t in[8], uint8_t dlc)
{
    uint32_t w0, w1, n;

    if (dlc > 8u) {
        dlc = 8u;
    }
    *cana_mb(MB_TX, 0) = (uint32_t)MB_CODE_TX_INACTIVE << 24;
    *cana_mb(MB_TX, 4) = (uint32_t)TX_ID << 18;
    w0 = ((uint32_t)in[0] << 24) | ((uint32_t)in[1] << 16)
       | ((uint32_t)in[2] << 8)  | (uint32_t)in[3];
    w1 = ((uint32_t)in[4] << 24) | ((uint32_t)in[5] << 16)
       | ((uint32_t)in[6] << 8)  | (uint32_t)in[7];
    *cana_mb(MB_TX, 8) = w0;
    *cana_mb(MB_TX, 12) = w1;
    *cana_mb(MB_TX, 0) = ((uint32_t)MB_CODE_TX_ONCE << 24) | ((uint32_t)dlc << 16);

    for (n = 0; n < 0x100000u; n++) {
        if ((*cana(CANA_IFLAG1)) & (1u << MB_TX)) {
            break;
        }
    }
    *cana(CANA_IFLAG1) = (1u << MB_TX);
}

static int wait_cts(uint8_t *bs, uint8_t *stmin)
{
    uint32_t tries;
    for (tries = 0; tries < 8u; tries++) {
        uint8_t f[8];
        int n = recv8_to(f, CTS_SPINS);
        uint8_t pci, fs, svc;
        if (n < 3) {
            return 0;
        }
        pci = (uint8_t)(f[0] & 0xF0u);
        if (pci == 0x30u) {
            fs = (uint8_t)(f[0] & 0x0Fu);
            if (fs == 0u) {
                *bs = f[1];
                *stmin = f[2];
                return 1;
            }
            if (fs == 1u) {
                continue;
            }
            return 0;
        }
        /* A new UDS SF while we wait for CTS — host retried. Abort
         * so the main loop can hear the next command. */
        svc = ((f[0] & 0xF0u) == 0u) ? f[1] : f[0];
        if (svc == 0x23u || svc == 0x1Au || svc == 0x11u) {
            return 0;
        }
    }
    return 0;
}

static void pace(uint8_t stmin)
{
    /* Tester FC is BS=0 / STmin=0 after FF. Only delay if asked. */
    if (stmin > 0u && stmin <= 0x7Fu) {
        spin((uint32_t)stmin * 10000u);
    }
}

/* Last 2 KiB of each 64 KiB window in the 4 MiB main image. Metal
 * 2026-09-06: $23 of 0x2F800 machine-checked this SRAM reader. Shadow
 * @ 0x00FFC000 (NVPWD @ 0x00FFFDD8) is a different array — do not skip
 * it or the shadow-password extract returns 0xFF. */
static int flash_hole(uint32_t addr)
{
    if (addr >= 0x00400000u) {
        return 0;
    }
    return (addr >= 0x10000u) && ((addr & 0xFFFFu) >= 0xF800u);
}

static void copy_mem(uint8_t *dst, uint32_t addr, uint16_t len)
{
    uint16_t i;
    for (i = 0; i < len; i++) {
        uint32_t a = addr + i;
        dst[i] = flash_hole(a) ? 0xFFu : *(const volatile uint8_t *)(a);
    }
}

static void reply_23(uint32_t addr, uint16_t len)
{
    uint16_t total;
    uint8_t frame[8];
    uint16_t off;
    uint8_t sn, in_blk, bs, stmin;

    if (len > BLK_MAX) {
        len = BLK_MAX;
    }
    copy_mem(BLK, addr, len);
    total = (uint16_t)(5u + len);

    if (total <= 7u) {
        frame[0] = (uint8_t)total;
        frame[1] = 0x63u;
        frame[2] = (uint8_t)(addr >> 24);
        frame[3] = (uint8_t)(addr >> 16);
        frame[4] = (uint8_t)(addr >> 8);
        frame[5] = (uint8_t)addr;
        frame[6] = (len > 0u) ? BLK[0] : 0u;
        frame[7] = (len > 1u) ? BLK[1] : 0u;
        send8(frame, 8);
        return;
    }

    /* FF: 18 05 63 + addr + b0  (2053 = 5 + 2048) */
    frame[0] = (uint8_t)(0x10u | ((total >> 8) & 0x0Fu));
    frame[1] = (uint8_t)total;
    frame[2] = 0x63u;
    frame[3] = (uint8_t)(addr >> 24);
    frame[4] = (uint8_t)(addr >> 16);
    frame[5] = (uint8_t)(addr >> 8);
    frame[6] = (uint8_t)addr;
    frame[7] = BLK[0];
    send8(frame, 8);

    if (!wait_cts(&bs, &stmin)) {
        return;
    }

    off = 1u;
    sn = 1u;
    in_blk = 0u;
    while (off < len) {
        uint8_t i;
        frame[0] = (uint8_t)(0x20u | (sn & 0x0Fu));
        for (i = 1; i < 8u; i++) {
            frame[i] = (off < len) ? BLK[off] : 0u;
            if (off < len) {
                off++;
            }
        }
        send8(frame, 8);
        sn = (uint8_t)((sn + 1u) & 0x0Fu);
        pace(stmin);
        in_blk++;
        if (bs > 0u && in_blk >= bs && off < len) {
            in_blk = 0u;
            if (!wait_cts(&bs, &stmin)) {
                return;
            }
        }
    }
}

static void reply_alive(uint8_t pid)
{
    uint8_t f[8] = { 0x07u, 0x5Au, pid, 'S', 'C', 'P', 'B', 0 };
    send8(f, 8);
}

static void do_reset(uint8_t sub)
{
    uint8_t f[8] = { 0x02u, 0x51u, sub, 0, 0, 0, 0, 0 };
    send8(f, 8);
    spin(40000u);
    *SIU_SRCR = SRCR_SSR;
    for (;;) {
    }
}

void scpb_main(void)
{
    leave_halt();
    mb_idle_all();
    arm_rx();

    for (;;) {
        uint8_t raw[8];
        uint8_t *p;
        uint8_t n = recv8(raw);
        if (n == 0u) {
            continue;
        }

        p = raw;
        if ((raw[0] & 0xF0u) == 0u && (raw[0] & 0x0Fu) >= 1u && (raw[0] & 0x0Fu) <= 7u) {
            p = &raw[1];
        }

        switch (p[0]) {
        case 0x1Au:
            reply_alive(p[1]);
            break;
        case 0x23u: {
            uint32_t addr = ((uint32_t)p[1] << 24) | ((uint32_t)p[2] << 16)
                          | ((uint32_t)p[3] << 8) | (uint32_t)p[4];
            uint16_t len = (uint16_t)(((uint16_t)p[5] << 8) | p[6]);
            reply_23(addr, len);
            break;
        }
        case 0x11u:
            do_reset(p[1]);
            break;
        default:
            /* Drop ISO-TP FC (0x30) and anything else. Do not NAK. */
            break;
        }
    }
}

void scpb_touch_id(void)
{
    (void)k_id[0];
}
