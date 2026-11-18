// Tiny test runner for the decoder library: no framework, just CHECK and a failure count.
#include <cstdio>
#include <string>

#include "../decoder/can.hpp"

static int failures = 0, checks = 0;
#define CHECK(cond)                                                       \
    do {                                                                  \
        ++checks;                                                         \
        if (!(cond)) {                                                    \
            ++failures;                                                   \
            std::printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);   \
        }                                                                 \
    } while (0)

int main() {
    using namespace can;

    // 1. Published check value for CRC-15/CAN (reveng CRC catalogue): "123456789" -> 0x059E.
    const std::string s = "123456789";
    CHECK(crc15(bytes_to_bits(std::vector<uint8_t>(s.begin(), s.end()))) == 0x059E);

    // 2. Residue property: appending the CRC to the bits makes the CRC of the whole thing 0.
    //    This is exactly the check a CAN receiver does.
    const std::vector<uint8_t> data{0x3C, 0x25, 0xC8, 0x7C};
    std::vector<uint8_t> bits = frame_bits(0x0C0, 4, data);
    CHECK(bits.size() == 19 + 32);
    const uint16_t crc = crc15(bits);
    std::vector<uint8_t> with_crc = bits;
    for (int i = 14; i >= 0; --i) with_crc.push_back((crc >> i) & 1);
    CHECK(crc15(with_crc) == 0);

    // 3. Every single-bit flip in the covered bits changes the CRC (CRC-15 detects all of them).
    int missed = 0;
    for (size_t i = 0; i < bits.size(); ++i) {
        std::vector<uint8_t> b = bits;
        b[i] ^= 1;
        if (crc15(b) == crc) ++missed;
    }
    CHECK(missed == 0);

    // 4. Same frame as the first line of the generated log (Python implementation): CRC must agree.
    CHECK(crc == 0x430E);

    // 5. Parsing: good frame, error frame, and malformed lines that must be rejected.
    Frame f;
    CHECK(parse_line("0.010003 0C0 4 3C25C87C 430E", f) && f.id == 0x0C0 && f.dlc == 4 && f.data[1] == 0x25 &&
          f.crc == 0x430E);
    CHECK(parse_line("1.5 ERR", f) && f.is_error);
    CHECK(!parse_line("", f));
    CHECK(!parse_line("0.1 0C0 4 3C25C8 430E", f));    // data shorter than DLC
    CHECK(!parse_line("0.1 800 1 00 0000", f));        // ID wider than 11 bits
    CHECK(!parse_line("0.1 0C0 9 00 0000", f));        // DLC > 8
    CHECK(!parse_line("0.1 0C0 1 ZZ 0000", f));        // not hex
    CHECK(!parse_line("abc 0C0 1 00 0000", f));        // bad timestamp

    // 6. Signal extraction is little-endian: bytes 3C 25 -> 0x253C = 9532 rpm.
    Signal rpm{"rpm", 0, 2, 1.0, 0.0};
    CHECK(extract_raw(rpm, data) == 0x253C);

    // 7. Timing classification for a 10 ms message.
    CHECK(check_timing(0.0100, 0.010) == Timing::Ok);
    CHECK(check_timing(0.0135, 0.010) == Timing::Jitter);
    CHECK(check_timing(0.0065, 0.010) == Timing::Jitter);
    CHECK(check_timing(0.0200, 0.010) == Timing::Gap);

    std::printf("%d/%d checks passed\n", checks - failures, checks);
    return failures ? 1 : 0;
}
