/* C90FL flash controller — MPC5674F dual array.
 *
 * Layout matches the public SSD (AN4521 / ssd_c90fl.h).
 * Bit positions are C90FL, not MPC57xx.
 */

#ifndef SCPB_W_C90FL_H
#define SCPB_W_C90FL_H

#include <stdint.h>

#define FMC0            0xC3F88000u
#define FMC1            0xC3F8C000u

#define FMC_MCR         0x00u
#define FMC_LMLR        0x04u
#define FMC_HLR         0x08u
#define FMC_SLMLR       0x0Cu
#define FMC_LMSR        0x10u
#define FMC_HSR         0x14u

#define MCR_DONE        0x00000400u  /* bit 10 */
#define MCR_PEG         0x00000200u  /* bit 9 — 1 = last op good */
#define MCR_PGM         0x00000010u  /* bit 4 */
#define MCR_ERS         0x00000004u  /* bit 2 */
#define MCR_EHV         0x00000001u  /* bit 0 */

#define LMLR_KEY        0xA1A11111u
#define HLR_KEY         0xB2B22222u
#define SLMLR_KEY       0xC3C33333u

#define SWT_SR          0xFFF38000u
#define SWT_SR_SVC      0xFFF38010u

#define HAS_BASE        0x00100000u
#define HAS_STRIDE      0x00080000u  /* 512 KiB logical; HSR index = >> 19 */

static inline volatile uint32_t *fmc(uint32_t base, uint32_t off)
{
    return (volatile uint32_t *)(base + off);
}

#endif
