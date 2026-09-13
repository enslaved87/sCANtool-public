/* Core TSR + DSPI_D companion watchdog pets.
 *
 * The bootloader services both from its flash-resident eMIOS ch11 ISR
 * (vector 62, ~12.5 ms). A SRAM kernel that leaves EE on cannot erase
 * that handler — or run from bootmode where it is absent.
 *
 * EE is cleared in start.S. This header pets from C wait loops so the
 * flash ISR is never required. Sequence matches FL0WL0W KernelMPC5674F
 * ServiceCoreWatchdog / ServiceCompanionWatchdog (two 3-word DSPI_D
 * commands, CONT within each, PCS released between them).
 *
 * Dest-gate still refuses boot / VIN / 0x1F000. This only removes the
 * flash-ISR dependency.
 */
#ifndef SCPB_MPC5674_WATCHDOG_H
#define SCPB_MPC5674_WATCHDOG_H

#include <stdint.h>

#define SCPB_DSPI_D          0xFFF9C000u
#define SCPB_DSPI_MCR        0x00u
#define SCPB_DSPI_TCR        0x08u
#define SCPB_DSPI_CTAR0      0x0Cu
#define SCPB_DSPI_SR         0x2Cu
#define SCPB_DSPI_RSER       0x30u
#define SCPB_DSPI_PUSHR      0x34u
#define SCPB_DSPI_POPR       0x38u
#define SCPB_DSPI_RFDF       0x00020000u
#define SCPB_DSPI_CTAS1      0x10000000u
#define SCPB_DSPI_PCS0       0x00010000u
#define SCPB_DSPI_CONT       0x80000000u
#define SCPB_DSPI_MCR_INIT   0x813F1900u
#define SCPB_DSPI_CTAR1_BOOT 0x78175561u
#define SCPB_DSPI_SR_INIT    0x90020000u

#define SCPB_STM_CR          ((volatile uint32_t *)0xFFF3C000u)
#define SCPB_STM_CNT         ((volatile uint32_t *)0xFFF3C004u)
#define SCPB_STM_1MHZ        0x0000FF01u
#define SCPB_WD_PERIOD_TICKS 12500u
#define SCPB_WD_SPIN_FALLBACK 20000u
#define SCPB_RFDF_SPINS      0x00040000u

#define SCPB_TSR_WIS         0x40000000u

static uint32_t scpb_stm_last;
static uint32_t scpb_wd_spins;

static inline volatile uint32_t *scpb_dspi(uint32_t off)
{
    return (volatile uint32_t *)(SCPB_DSPI_D + off);
}

static inline void scpb_irq_off(void)
{
    __asm__ volatile ("wrteei 0" ::: "memory");
}

static inline void scpb_service_core_watchdog(void)
{
    const uint32_t wis = SCPB_TSR_WIS;
    __asm__ volatile (
        "isync\n"
        "mtspr 336, %0\n"
        "isync\n"
        :
        : "r"(wis)
        : "memory"
    );
}

static inline uint16_t scpb_companion_word(uint16_t word, int keep_pcs)
{
    uint32_t n;
    uint32_t push = SCPB_DSPI_CTAS1 | SCPB_DSPI_PCS0 | (uint32_t)word;
    if (keep_pcs) {
        push |= SCPB_DSPI_CONT;
    }
    *scpb_dspi(SCPB_DSPI_SR) = SCPB_DSPI_RFDF;
    *scpb_dspi(SCPB_DSPI_PUSHR) = push;
    for (n = 0; n < SCPB_RFDF_SPINS; n++) {
        if ((*scpb_dspi(SCPB_DSPI_SR)) & SCPB_DSPI_RFDF) {
            return (uint16_t)(*scpb_dspi(SCPB_DSPI_POPR));
        }
    }
    return 0;
}

static inline void scpb_service_companion_watchdog(void)
{
    /* Two 3-word commands. CONT on words 0,1 / 3,4; release on 2 and 5. */
    static const uint16_t msg[6] = {
        0x6AA4u, 0xA1F0u, 0x0000u,
        0x6944u, 0xA1F0u, 0x0000u,
    };
    unsigned i;
    for (i = 0; i < 6u; i++) {
        (void)scpb_companion_word(msg[i], (i % 3u) != 2u);
    }
}

static inline void scpb_wd_kick(void)
{
    scpb_service_core_watchdog();
    scpb_service_companion_watchdog();
}

static inline void scpb_wd_init(void)
{
    scpb_irq_off();
    *scpb_dspi(SCPB_DSPI_MCR) = SCPB_DSPI_MCR_INIT;
    *scpb_dspi(SCPB_DSPI_TCR) = (*scpb_dspi(SCPB_DSPI_TCR)) & 0x0000FFFFu;
    *scpb_dspi(SCPB_DSPI_RSER) = SCPB_DSPI_RFDF;
    *scpb_dspi(SCPB_DSPI_CTAR0) = 0;
    *scpb_dspi(SCPB_DSPI_CTAR0 + 4u) = SCPB_DSPI_CTAR1_BOOT;
    *scpb_dspi(SCPB_DSPI_CTAR0 + 8u) = 0x3AFC3879u;
    *scpb_dspi(SCPB_DSPI_CTAR0 + 12u) = 0x3ADC3B79u;
    *scpb_dspi(SCPB_DSPI_CTAR0 + 16u) = 0x3AEC3C09u;
    *scpb_dspi(SCPB_DSPI_SR) = SCPB_DSPI_SR_INIT;
    *SCPB_STM_CR = SCPB_STM_1MHZ;
    scpb_stm_last = *SCPB_STM_CNT;
    scpb_wd_spins = 0;
    scpb_wd_kick();
}

static inline void scpb_wd_poll(void)
{
    uint32_t now = *SCPB_STM_CNT;
    scpb_wd_spins++;
    if ((now - scpb_stm_last) >= SCPB_WD_PERIOD_TICKS
        || scpb_wd_spins >= SCPB_WD_SPIN_FALLBACK) {
        scpb_stm_last = now;
        scpb_wd_spins = 0;
        scpb_wd_kick();
    }
}

#endif
