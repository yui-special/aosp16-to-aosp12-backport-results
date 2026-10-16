#include "process.h"

static int factor = 2;

int process(int x)
{
    int scaled = x * factor;
    return scaled;
}
