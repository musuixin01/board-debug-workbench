#include <stdio.h>
#include <stdint.h>
#include <stdbool.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

volatile uint32_t sample_count = 0;
volatile float simulated_voltage = 3.3f;
volatile bool alarm_state = false;

void app_main(void) {
    while (1) {
        sample_count++;
        simulated_voltage = 3.0f + (sample_count % 10) * 0.05f;
        alarm_state = simulated_voltage < 3.10f;
        printf("{\"count\":%lu,\"voltage\":%.2f,\"alarm\":%d}\n", (unsigned long)sample_count, (double)simulated_voltage, alarm_state ? 1 : 0);
        vTaskDelay(pdMS_TO_TICKS(500));
    }
}
