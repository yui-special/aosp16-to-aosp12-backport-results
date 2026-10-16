#include "handler.h"

int main(int argc, char **argv)
{
    int fd = open_channel(argv[1]);
    handle_request(fd);
    return 0;
}
