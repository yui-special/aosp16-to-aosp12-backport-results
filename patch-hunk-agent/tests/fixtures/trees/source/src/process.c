#include "process.h"

static int factor = 3;

static int check_bounds(int value)
{
    if (value > 1000) {
        return 1000;
    }
    return value;
}

static int clamp_value(int value, int limit)
{
    return value > limit ? limit : value;
}

int process(int x)
{
    int scaled = x * factor;
    return scaled;
}
