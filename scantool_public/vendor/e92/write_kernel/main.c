/* sCANtool Public — SRAM write kernel SCPB-W1 (MPC5674F).
 *
 * Written here from:
 *   - C90FL public SSD map (AN4521): DONE@10, PEG@9, ERS@2, EHV@0
 *   - Dual FMC @ 0xC3F88000 / 0xC3F8C000
 *   - HAS HSR index = (addr - 0x100000) >> 19   // 512 KiB logical
 *   - Dual-module HAS erase + program; MOD1 interlock at base+16
 *
 * Uploaded only for EARLY dest-gate writes. Transfer ACK is not
 * programmed flash — the host waits for the SCPB dest ack.
 * Program is EHV every 16 B (HAS: every 32 B dual-mod). One EHV per
 * 4 KiB folds to the last 16 B and is refused.
 *
 * EE is cleared at _start. C wait loops pet Book-E TSR + DSPI_D
 * companion so the flash eMIOS11 ISR is not required.
 */

#include <stdint.h>
#include "mpc5674_flexcan.h"
#include "mpc5674_c90fl.h"
#include "mpc5674_watchdog.h"

#define RX_ID      0x7E0u
#define TX_ID      0x7E8u
#define MB_RX      0u
#define MB_TX      8u
#define SIU_SRCR   ((volatile uint32_t *)0xC3F90010u)
#define CHUNK      0x1000u

__attribute__((used, section(".rodata")))
static const char k_id[] = "SCPB-W1 HAS>>19 dual-mod C90FL";

static uint8_t  g_buf[CHUNK];
static uint32_t g_addr;
static uint16_t g_need;
static uint16_t g_got;
static uint8_t  g_filling;

static void sync(void)
{
    __asm__ volatile ("msync" ::: "memory");
}

static void spin(uint32_t n)
{
    volatile uint32_t i;
    for (i = 0; i < n; i++) {
    }
}

/* ---- FlexCAN (same silicon rules as the read kernel) ---- */

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
    volatile uint32_t *mcr = cana(CANA_MCR);
    *mcr = (*mcr) & ~(MCR_HALT | MCR_MDIS);
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

static uint8_t recv8(uint8_t out[8])
{
    uint32_t cs, w0, w1;
    uint8_t dlc;

    while (((*cana(CANA_IFLAG1)) & (1u << MB_RX)) == 0u) {
        scpb_wd_poll();
    }
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
    return dlc;
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
       | ((uint32_t)in[2] << 8) | (uint32_t)in[3];
    w1 = ((uint32_t)in[4] << 24) | ((uint32_t)in[5] << 16)
       | ((uint32_t)in[6] << 8) | (uint32_t)in[7];
    *cana_mb(MB_TX, 8) = w0;
    *cana_mb(MB_TX, 12) = w1;
    *cana_mb(MB_TX, 0) = ((uint32_t)MB_CODE_TX_ONCE << 24) | ((uint32_t)dlc << 16);
    for (n = 0; n < 0x100000u; n++) {
        if ((*cana(CANA_IFLAG1)) & (1u << MB_TX)) {
            break;
        }
        if ((n & 0x3FFu) == 0u) {
            scpb_wd_poll();
        }
    }
    *cana(CANA_IFLAG1) = (1u << MB_TX);
}

static void ack_ok(uint32_t addr)
{
    uint8_t f[8];
    f[0] = (uint8_t)(addr >> 24);
    f[1] = (uint8_t)(addr >> 16);
    f[2] = (uint8_t)(addr >> 8);
    f[3] = (uint8_t)addr;
    f[4] = 'S';
    f[5] = 'C';
    f[6] = 'P';
    f[7] = 'B';
    send8(f, 8);
}

static void ack_err(uint8_t why, uint32_t snap)
{
    uint8_t f[8];
    f[0] = 'E';
    f[1] = 'R';
    f[2] = 'R';
    f[3] = why;
    f[4] = (uint8_t)(snap >> 24);
    f[5] = (uint8_t)(snap >> 16);
    f[6] = (uint8_t)(snap >> 8);
    f[7] = (uint8_t)snap;
    send8(f, 8);
}

/* ---- Geometry: firmware op table + HAS 512 KiB ---- */

static int is_shadow(uint32_t addr)
{
    return (addr >= 0x00FFC000u && addr < 0x01000000u)
        || (addr >= 0x00EFC000u && addr < 0x00F00000u);
}

static uint32_t select_bits(uint32_t addr)
{
    /* SSD FlashErase shadowFlag: LMSR/HSR stay 0; interlock is shadowRowBase. */
    if (is_shadow(addr)) {
        return 0;
    }
    if (addr < 0x00020000u) {
        return 1u << (addr >> 14);          /* 8 × 16 KiB */
    }
    if (addr < 0x00030000u) {
        return 1u << 8;                     /* 64 KiB */
    }
    if (addr < 0x00040000u) {
        return 1u << 9;                     /* 64 KiB */
    }
    if (addr < 0x00060000u) {
        return 1u << 16;                    /* 128 KiB (LMSR carry) */
    }
    if (addr < 0x00080000u) {
        return 1u << 17;                    /* 128 KiB */
    }
    if (addr < 0x000C0000u) {
        return 1u;                          /* MID0: MOD1 LMSR bit 0 */
    }
    if (addr < HAS_BASE) {
        /* Flash B M0 @ 0xC0000 = MSEL0 (LMSR bit 16). */
        return 1u << 16;
    }
    /* H0..H5: 512 KiB logical. The 256 KiB shift is wrong. */
    return 1u << ((addr - HAS_BASE) >> 19);
}

static uint32_t primary_fmc(uint32_t addr)
{
    if (addr >= 0x00FFC000u && addr < 0x01000000u) {
        return FMC0;
    }
    if (addr >= 0x00EFC000u && addr < 0x00F00000u) {
        return FMC1;
    }
    if (addr < 0x00080000u) {
        return FMC0;
    }
    if (addr < HAS_BASE) {
        return FMC1;
    }
    return FMC0;
}

static int is_has(uint32_t addr)
{
    return addr >= HAS_BASE && !is_shadow(addr);
}

/* ---- C90FL ---- */

static void unlock_both(void)
{
    uint32_t mods[2];
    unsigned i;

    *(volatile uint32_t *)SWT_SR_SVC = 0x0000C520u;
    *(volatile uint32_t *)SWT_SR_SVC = 0x0000D928u;
    *(volatile uint32_t *)SWT_SR = 0xFF00000Au;
    sync();

    mods[0] = FMC0;
    mods[1] = FMC1;
    for (i = 0; i < 2u; i++) {
        *fmc(mods[i], FMC_MCR) = 0x00008000u;
        *fmc(mods[i], FMC_LMLR) = LMLR_KEY;
        *fmc(mods[i], FMC_LMLR) = 0;
        *fmc(mods[i], FMC_HLR) = HLR_KEY;
        *fmc(mods[i], FMC_HLR) = 0;
        *fmc(mods[i], FMC_SLMLR) = SLMLR_KEY;
        *fmc(mods[i], FMC_SLMLR) = 0;
    }
    sync();
}

static void mcr_idle(uint32_t base)
{
    volatile uint32_t *mcr = fmc(base, FMC_MCR);
    *mcr = (*mcr & ~(MCR_ERS | MCR_PGM | MCR_EHV)) | MCR_DONE;
    sync();
}

static int wait_done(uint32_t base)
{
    volatile uint32_t *mcr = fmc(base, FMC_MCR);
    uint32_t n;
    for (n = 0; n < 0x20000000u; n++) {
        if ((*mcr) & MCR_DONE) {
            return 1;
        }
        scpb_wd_poll();
    }
    return 0;
}

static int wait_erase_done(uint32_t base)
{
    volatile uint32_t *mcr = fmc(base, FMC_MCR);
    uint32_t n;
    int saw_busy = (((*mcr) & MCR_DONE) == 0u);

    if (!saw_busy) {
        for (n = 0; n < 0x800000u; n++) {
            if (((*mcr) & MCR_DONE) == 0u) {
                saw_busy = 1;
                break;
            }
            scpb_wd_poll();
        }
    }
    if (!saw_busy) {
        return 0;
    }
    return wait_done(base);
}

static int erase_one(uint32_t base, uint32_t sector)
{
    volatile uint32_t *mcr = fmc(base, FMC_MCR);
    volatile uint32_t *sel = fmc(base, is_has(sector) ? FMC_HSR : FMC_LMSR);
    uint32_t ilock;

    mcr_idle(base);
    /* Only one select register may be live. Leftover LMSR from MID/LAS
     * plus HAS HSR latches both and over-erases neighbors. */
    *fmc(base, FMC_LMSR) = 0;
    *fmc(base, FMC_HSR) = 0;
    sync();
    *sel = select_bits(sector);
    sync();
    *mcr = MCR_ERS;
    sync();

    /* Interlock must land on an address this module owns.
     * HAS is 16-byte interleaved: MOD0 at aligned base, MOD1 at +16. */
    ilock = sector & ~0xFu;
    if (base == FMC1) {
        ilock += 16u;
    }
    *(volatile uint32_t *)ilock = 0xFFFFFFFFu;
    sync();

    *mcr = MCR_ERS | MCR_EHV;
    sync();

    if (!wait_erase_done(base)) {
        return 0;
    }
    if (((*mcr) & MCR_PEG) == 0u) {
        return 0;
    }
    mcr_idle(base);
    return 1;
}

static int erase_sector(uint32_t sector)
{
    unlock_both();
    if (is_has(sector)) {
        /* HAS is 16-byte interleaved. Erase MOD1 then MOD0 — MOD0-first
         * left a half-blank bank on metal (same C90FL rule as OEM FE). */
        if (!erase_one(FMC1, sector) || !erase_one(FMC0, sector)) {
            return 0;
        }
        return 1;
    }
    return erase_one(primary_fmc(sector), sector);
}

static int arm_pgm(uint32_t mods[], unsigned nmod, uint32_t addr)
{
    unsigned i;
    for (i = 0; i < nmod; i++) {
        volatile uint32_t *mcr = fmc(mods[i], FMC_MCR);
        volatile uint32_t *sel = fmc(mods[i], is_has(addr) ? FMC_HSR : FMC_LMSR);
        if ((*mcr) & (MCR_PGM | MCR_ERS)) {
            if (!wait_done(mods[i])) {
                return 0;
            }
        }
        mcr_idle(mods[i]);
        *fmc(mods[i], FMC_LMSR) = 0;
        *fmc(mods[i], FMC_HSR) = 0;
        sync();
        *sel = select_bits(addr);
        sync();
        *mcr = MCR_PGM;
        sync();
    }
    return 1;
}

static int fire_ehv(uint32_t mods[], unsigned nmod)
{
    unsigned i;
    for (i = 0; i < nmod; i++) {
        *fmc(mods[i], FMC_MCR) = MCR_PGM | MCR_EHV;
        sync();
    }
    for (i = 0; i < nmod; i++) {
        volatile uint32_t *mcr = fmc(mods[i], FMC_MCR);
        if (!wait_done(mods[i])) {
            mcr_idle(mods[i]);
            return 0;
        }
        if (((*mcr) & MCR_PEG) == 0u) {
            mcr_idle(mods[i]);
            return 0;
        }
        mcr_idle(mods[i]);
    }
    return 1;
}

static void store8(uint32_t addr, const uint8_t *src)
{
    uint32_t d0 = ((uint32_t)src[0] << 24) | ((uint32_t)src[1] << 16)
                | ((uint32_t)src[2] << 8) | (uint32_t)src[3];
    uint32_t d1 = ((uint32_t)src[4] << 24) | ((uint32_t)src[5] << 16)
                | ((uint32_t)src[6] << 8) | (uint32_t)src[7];
    *(volatile uint32_t *)addr = d0;
    *(volatile uint32_t *)(addr + 4u) = d1;
    sync();
}

/* C90FL programs 16 B per EHV. One EHV after 4 KiB of stores folds
 * to payload[-16:] at the start address. HAS is 16-byte A/B, so one
 * dual EHV after 32 B (both lanes). EHV the page just stored — never
 * the next page (0x1F000 + 4 KiB must not interlock 0x20000). */
static int program_chunk(uint32_t addr, const uint8_t *src, uint16_t len)
{
    uint32_t mods[2];
    unsigned nmod, off, k;
    unsigned page;

    unlock_both();

    mods[0] = primary_fmc(addr);
    nmod = 1u;
    if (is_has(addr)) {
        mods[0] = FMC0;
        mods[1] = FMC1;
        nmod = 2u;
    }
    page = is_has(addr) ? 32u : 16u;

    for (off = 0; off < len; off += page) {
        if (!arm_pgm(mods, nmod, addr + off)) {
            return 0;
        }
        for (k = 0; k < page; k += 8u) {
            store8(addr + off + k, src + off + k);
        }
        if (!fire_ehv(mods, nmod)) {
            return 0;
        }
    }
    return 1;
}

/* ---- UDS-ish surface ---- */

static void reply_alive(uint8_t pid)
{
    uint8_t f[8] = { 0x07u, 0x5Au, pid, 'S', 'C', 'P', 'B', 'W' };
    send8(f, 8);
}

static void do_reset(uint8_t sub)
{
    uint8_t f[8] = { 0x02u, 0x51u, sub, 0, 0, 0, 0, 0 };
    send8(f, 8);
    spin(40000u);
    *SIU_SRCR = 0x80000000u;
    for (;;) {
    }
}

static int wait_cts(uint8_t *bs, uint8_t *stmin)
{
    for (;;) {
        uint8_t f[8];
        uint8_t n = recv8(f);
        uint8_t fs;
        if (n < 3u || (f[0] & 0xF0u) != 0x30u) {
            continue;
        }
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
}

static void reply_23(uint32_t addr, uint16_t len)
{
    const volatile uint8_t *src = (const volatile uint8_t *)addr;
    uint16_t total, off;
    uint8_t frame[8], sn, in_blk, bs, stmin;

    if (len > 4090u) {
        len = 4090u;
    }
    total = (uint16_t)(5u + len);
    if (total <= 7u) {
        frame[0] = (uint8_t)total;
        frame[1] = 0x63u;
        frame[2] = (uint8_t)(addr >> 24);
        frame[3] = (uint8_t)(addr >> 16);
        frame[4] = (uint8_t)(addr >> 8);
        frame[5] = (uint8_t)addr;
        frame[6] = (len > 0u) ? src[0] : 0u;
        frame[7] = (len > 1u) ? src[1] : 0u;
        send8(frame, 8);
        return;
    }
    frame[0] = (uint8_t)(0x10u | ((total >> 8) & 0x0Fu));
    frame[1] = (uint8_t)total;
    frame[2] = 0x63u;
    frame[3] = (uint8_t)(addr >> 24);
    frame[4] = (uint8_t)(addr >> 16);
    frame[5] = (uint8_t)(addr >> 8);
    frame[6] = (uint8_t)addr;
    frame[7] = src[0];
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
            frame[i] = (off < len) ? src[off] : 0u;
            if (off < len) {
                off++;
            }
        }
        send8(frame, 8);
        sn = (uint8_t)((sn + 1u) & 0x0Fu);
        if (stmin > 0u && stmin <= 0x7Fu) {
            spin((uint32_t)stmin * 8000u);
        }
        in_blk++;
        if (bs > 0u && in_blk >= bs && off < len) {
            in_blk = 0u;
            if (!wait_cts(&bs, &stmin)) {
                return;
            }
        }
    }
}

static void finish_program(void)
{
    if (!program_chunk(g_addr, g_buf, g_need)) {
        ack_err(0x02u, *fmc(primary_fmc(g_addr), FMC_MCR));
    } else {
        ack_ok(g_addr);
    }
    g_filling = 0;
    g_got = 0;
    g_need = 0;
}

void scpb_write_main(void)
{
    scpb_wd_init();
    leave_halt();
    mb_idle_all();
    arm_rx();
    g_filling = 0;

    for (;;) {
        uint8_t raw[8];
        uint8_t *p;
        uint8_t n = recv8(raw);
        if (n == 0u) {
            continue;
        }

        if (g_filling) {
            uint8_t i;
            for (i = 0; i < 8u && g_got < g_need; i++) {
                g_buf[g_got++] = raw[i];
            }
            if (g_got >= g_need) {
                finish_program();
            }
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
        case 0x6Bu: {
            uint32_t sector = ((uint32_t)p[1] << 24) | ((uint32_t)p[2] << 16)
                            | ((uint32_t)p[3] << 8) | (uint32_t)p[4];
            if (!erase_sector(sector)) {
                ack_err(0x10u, *fmc(primary_fmc(sector), FMC_MCR));
            } else {
                ack_ok(sector);
            }
            break;
        }
        case 0x6Cu:
            g_addr = ((uint32_t)p[1] << 24) | ((uint32_t)p[2] << 16)
                   | ((uint32_t)p[3] << 8) | (uint32_t)p[4];
            g_need = CHUNK;
            g_got = 0;
            g_filling = 1;
            break;
        default:
            break;
        }
    }
}

void scpb_touch_id(void)
{
    (void)k_id[0];
}
