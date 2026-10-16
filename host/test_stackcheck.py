"""Exercise the real scratch-code integrity report with mutable trailing data."""
import json
from pathlib import Path
import subprocess
import tempfile

from test_dual_faults import function_body

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = (ROOT / "firmware/src/app/main.c").read_text()
    paint = next(line for line in source.splitlines() if line.startswith("#define STACK_PAINT "))
    fixture = "#include <stdint.h>\n#include <stdio.h>\n" + paint + "\n"
    fixture += "static uint32_t *paint_floor(uint32_t *code_end, uint32_t *bottom) {\n"
    fixture += function_body(source, "paint_floor") + "\n}\n"
    fixture += ("static void stack_report(const char *name, uint32_t *start, uint32_t *end, "
                "const uint32_t *source, const uint32_t *code_end, uint32_t *bottom, uint32_t *top) {\n")
    fixture += function_body(source, "stack_report") + "\n}\n"
    fixture += r'''
static void report(uint32_t *image, const uint32_t *flash, unsigned immutable_words) {
    putchar('{');
    stack_report("test", image, image+16, flash, image+immutable_words, image+24, image+32);
    puts("}");
}
int main(void) {
    uint32_t image[32], flash[16];
    for(unsigned n=0;n<16;n++)image[n]=flash[n]=0x12340000u+n;
    for(unsigned n=16;n<32;n++)image[n]=n<28?STACK_PAINT:0;
    for(unsigned n=12;n<16;n++)image[n]^=0xabcdef01u;
    report(image,flash,12); // every mutable state word may change
    image[3]^=1;report(image,flash,12); // early instruction corruption
    image[3]^=1;image[11]^=1;report(image,flash,12); // final immutable word
    image[11]^=1;report(image,flash,16); // default layout checks the entire bank
}
'''
    with tempfile.TemporaryDirectory(prefix="stackcheck-native-") as temporary:
        test, executable = Path(temporary) / "stack.c", Path(temporary) / "stack"
        test.write_text(fixture)
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror",
                        str(test), "-o", str(executable)], check=True)
        rows = [json.loads(line)["test"] for line in subprocess.check_output(
            [str(executable)], text=True).splitlines()]
    assert [row["code_bytes"] for row in rows] == [48, 48, 48, 64]
    assert [row["code_matches_flash"] for row in rows] == [True, False, False, False]
    assert [row["first_corrupt_code_offset"] for row in rows] == [-1, 12, 44, 48]
    for row in rows:
        assert row["stack_bytes"] == 32 and row["stack_in_code_bank"]
        assert row["deepest_use_bytes"] == 16 and not row["paint_exhausted"]
    print("stackcheck: PASS (mutable state excluded, first/last code corruption detected, total allocation still bounds paint, default full-bank comparison retained)")


if __name__ == "__main__":
    main()
