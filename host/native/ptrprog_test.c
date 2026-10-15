// Export the actual instruction words used by the device experiment.
#include <stdio.h>
#include "ptrprog.h"
int main(void) {
    return fwrite(PTRPROG_CODE, sizeof PTRPROG_CODE[0], PTRPROG_LENGTH, stdout) == PTRPROG_LENGTH ? 0 : 1;
}
