#include "handler.h"

int handle_request(int fd)
{
    int result = read_request(fd);
    return result;
}
