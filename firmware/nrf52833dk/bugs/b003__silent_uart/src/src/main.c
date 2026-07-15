#include <stdint.h>

#include <zephyr/kernel.h>

const uint32_t stage1_known_value = 0x1234ABCD; /* BENCHMARK-DEFINED (symbol stays healthy) */

int main(void)
{
    while (1) {
        (void)*(const volatile uint32_t *)&stage1_known_value;
        k_msleep(1000);
    }

    return 0;
}
