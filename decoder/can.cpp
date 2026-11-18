#include "can.hpp"

#include <cmath>
#include <cstdlib>
#include <fstream>
#include <sstream>
#include <stdexcept>

namespace can {

uint16_t crc15(const std::vector<uint8_t>& bits) {
    uint16_t crc = 0;
    for (uint8_t b : bits) {
        const bool next = (b & 1) ^ ((crc >> 14) & 1);
        crc = (crc << 1) & 0x7FFF;
        if (next) crc ^= 0x4599;
    }
    return crc;
}

std::vector<uint8_t> bytes_to_bits(const std::vector<uint8_t>& bytes) {
    std::vector<uint8_t> bits;
    bits.reserve(bytes.size() * 8);
    for (uint8_t byte : bytes)
        for (int i = 7; i >= 0; --i) bits.push_back((byte >> i) & 1);
    return bits;
}

std::vector<uint8_t> frame_bits(uint16_t id, int dlc, const std::vector<uint8_t>& data) {
    std::vector<uint8_t> bits{0};                                // SOF (dominant)
    for (int i = 10; i >= 0; --i) bits.push_back((id >> i) & 1);  // identifier, MSB first
    bits.insert(bits.end(), {0, 0, 0});                          // RTR, IDE, r0: data frame, standard ID
    for (int i = 3; i >= 0; --i) bits.push_back((dlc >> i) & 1);
    for (uint8_t b : bytes_to_bits(data)) bits.push_back(b);
    return bits;
}

// Parse a whole token as an unsigned hex number; false if any character is not hex.
static bool parse_hex(const std::string& s, unsigned long& out) {
    if (s.empty()) return false;
    char* end = nullptr;
    out = std::strtoul(s.c_str(), &end, 16);
    return *end == '\0';
}

bool parse_line(const std::string& line, Frame& out) {
    std::istringstream in(line);
    std::string ts, id;
    if (!(in >> ts >> id)) return false;
    char* end = nullptr;
    out = Frame{};
    out.t = std::strtod(ts.c_str(), &end);
    if (*end != '\0' || !std::isfinite(out.t)) return false;
    if (id == "ERR") {
        out.is_error = true;
        return true;
    }
    std::string dlc, data, crc;
    if (!(in >> dlc >> data >> crc)) return false;
    unsigned long v = 0;
    if (!parse_hex(id, v) || v > 0x7FF) return false;  // 11-bit identifier
    out.id = static_cast<uint16_t>(v);
    if (dlc.size() != 1 || dlc[0] < '0' || dlc[0] > '8') return false;
    out.dlc = dlc[0] - '0';
    if (data.size() != static_cast<size_t>(2 * out.dlc)) return false;
    for (int i = 0; i < out.dlc; ++i) {
        if (!parse_hex(data.substr(2 * i, 2), v)) return false;
        out.data.push_back(static_cast<uint8_t>(v));
    }
    if (!parse_hex(crc, v) || v > 0x7FFF) return false;
    out.crc = static_cast<uint16_t>(v);
    return true;
}

std::map<uint16_t, Message> load_table(const std::string& csv_path) {
    std::ifstream f(csv_path);
    if (!f) throw std::runtime_error("cannot open " + csv_path);
    std::map<uint16_t, Message> table;
    std::string line;
    std::getline(f, line);  // header
    while (std::getline(f, line)) {
        if (line.empty()) continue;
        std::vector<std::string> col;
        std::stringstream ss(line);
        std::string c;
        while (std::getline(ss, c, ',')) col.push_back(c);
        if (col.size() < 9) throw std::runtime_error("bad signal table row: " + line);
        // msg_id,msg_name,period_ms,dlc,signal,start_byte,num_bytes,scale,offset,...
        auto id = static_cast<uint16_t>(std::stoul(col[0], nullptr, 16));
        Message& m = table[id];
        m.id = id;
        m.name = col[1];
        m.period_s = std::stod(col[2]) / 1000.0;
        m.dlc = std::stoi(col[3]);
        m.signals.push_back({col[4], std::stoi(col[5]), std::stoi(col[6]), std::stod(col[7]), std::stod(col[8])});
    }
    return table;
}

uint64_t extract_raw(const Signal& s, const std::vector<uint8_t>& data) {
    uint64_t raw = 0;
    for (int i = 0; i < s.num_bytes; ++i) raw |= uint64_t(data.at(s.start_byte + i)) << (8 * i);
    return raw;
}

Timing check_timing(double dt, double period) {
    // > 1.5 periods: at least one frame is missing. Otherwise anything more than 25% off the
    // nominal period is jitter (normal clock jitter here is ~0.2% of a 10 ms period).
    if (dt > 1.5 * period) return Timing::Gap;
    if (std::fabs(dt - period) > 0.25 * period) return Timing::Jitter;
    return Timing::Ok;
}

}  // namespace can
