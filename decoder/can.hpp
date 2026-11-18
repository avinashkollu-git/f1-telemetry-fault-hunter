// CAN 2.0A log decoding: CRC-15, a small DBC-like signal table, timing checks.
#pragma once
#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace can {

struct Signal {
    std::string name;
    int start_byte = 0, num_bytes = 1;  // byte-aligned, little-endian (Intel) only
    double scale = 1, offset = 0;
};

struct Message {
    uint16_t id = 0;
    std::string name;
    double period_s = 0;
    int dlc = 0;
    std::vector<Signal> signals;
};

struct Frame {
    double t = 0;
    bool is_error = false;  // "ERR" line: an error frame seen on the bus
    uint16_t id = 0;
    int dlc = 0;
    std::vector<uint8_t> data;
    uint16_t crc = 0;
};

// CRC-15/CAN: poly 0x4599, init 0, no reflection, no final XOR. Input is one bit per element.
uint16_t crc15(const std::vector<uint8_t>& bits);
std::vector<uint8_t> bytes_to_bits(const std::vector<uint8_t>& bytes);
// Bits the CRC covers: SOF, 11-bit ID, RTR, IDE, r0, DLC, data (before bit stuffing).
std::vector<uint8_t> frame_bits(uint16_t id, int dlc, const std::vector<uint8_t>& data);

// Parse "<t> <ID> <DLC> <DATA> <CRC>" or "<t> ERR". Returns false on any malformed input.
bool parse_line(const std::string& line, Frame& out);

std::map<uint16_t, Message> load_table(const std::string& csv_path);
uint64_t extract_raw(const Signal& s, const std::vector<uint8_t>& data);

enum class Timing { Ok, Jitter, Gap };
// dt = time since the previous good frame with the same ID.
Timing check_timing(double dt, double period);

}  // namespace can
