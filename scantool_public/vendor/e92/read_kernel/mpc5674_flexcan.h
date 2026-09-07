/* FlexCAN_A on MPC5674F — register offsets from the public reference manual.
 * Chapter 30. Not copied from any third-party kernel. */

#ifndef SCPB_MPC5674_FLEXCAN_H
#define SCPB_MPC5674_FLEXCAN_H

#include <stdint.h>

#define CANA_BASE        0xFFFC0000u
#define CANA_MCR         0x0000u
#define CANA_CTRL        0x0004u
#define CANA_TIMER       0x0008u
#define CANA_ESR         0x0020u
#define CANA_IMASK1      0x0028u
#define CANA_IFLAG1      0x0030u
#define CANA_MB0         0x0080u
#define CANA_MB_BYTES    16u

#define MCR_MDIS         (1u << 31)
#define MCR_FRZ          (1u << 30)
#define MCR_HALT         (1u << 28)
#define MCR_NOTRDY       (1u << 27)
#define MCR_FRZACK       (1u << 24)
/* SRXDIS (bit 17) is freeze-mode only — do not poke it from normal mode. */

#define MB_CODE_RX_EMPTY  0x4u
#define MB_CODE_RX_FULL   0x2u
#define MB_CODE_TX_ABORT  0x9u
#define MB_CODE_TX_INACTIVE 0x8u
#define MB_CODE_TX_ONCE   0xCu

static inline volatile uint32_t *cana(uint32_t off)
{
    return (volatile uint32_t *)(CANA_BASE + off);
}

static inline volatile uint32_t *cana_mb(unsigned idx, unsigned word)
{
    return (volatile uint32_t *)(CANA_BASE + CANA_MB0 + idx * CANA_MB_BYTES + word);
}

#endif
