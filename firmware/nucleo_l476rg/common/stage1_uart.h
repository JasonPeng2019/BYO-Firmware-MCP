#pragma once

#include <stdbool.h>

#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/sys/printk.h>

#define STAGE1_UART_NODE DT_CHOSEN(zephyr_console)

#if !DT_NODE_HAS_STATUS(STAGE1_UART_NODE, okay)
#error "nucleo_l476rg Stage 1 firmware requires an enabled zephyr,console node"
#endif

static inline bool stage1_uart_ready(void)
{
    return device_is_ready(DEVICE_DT_GET(STAGE1_UART_NODE));
}

static inline void stage1_uart_write_line(const char *text)
{
    if (!stage1_uart_ready()) {
        return;
    }

    printk("%s", text);
}
