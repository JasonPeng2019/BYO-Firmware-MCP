#include <stdint.h>

#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>

const uint32_t stage1_known_value = 0x1234ABCD; /* BENCHMARK-DEFINED (bug case keeps the symbol healthy) */

int main(void)
{
    while (1) {
        if (*(const volatile uint32_t *)&stage1_known_value == 0x1234ABCDu) {
            printk("boot nope\n"); /* BENCHMARK-DEFINED (intentional wrong UART signature) */
        } else {
            printk("boot nope\n");
        }
        k_msleep(1000);
    }

    return 0;
}
