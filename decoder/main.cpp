// can_decode: read a CAN log, check every frame's CRC-15, decode signals, flag protocol and
// timing anomalies. Single-threaded on purpose: the log is small and order matters for timing.
//
// usage: can_decode <can_log.txt> <signals.csv> <out_dir>
// writes: <out_dir>/decoded.csv   t,can_id,signal,raw,value
//         <out_dir>/anomalies.csv t,can_id,type,detail
//         <out_dir>/decode_stats.json
#include <chrono>
#include <cmath>
#include <cstdio>
#include <fstream>
#include <iostream>
#include <map>

#include "can.hpp"

int main(int argc, char** argv) {
    if (argc != 4) {
        std::cerr << "usage: can_decode <can_log.txt> <signals.csv> <out_dir>\n";
        return 2;
    }
    std::map<uint16_t, can::Message> table;
    try {
        table = can::load_table(argv[2]);
    } catch (const std::exception& e) {
        std::cerr << "error: " << e.what() << "\n";
        return 1;
    }
    std::ifstream log(argv[1]);
    const std::string out_dir = argv[3];
    std::ofstream decoded(out_dir + "/decoded.csv"), anomalies(out_dir + "/anomalies.csv");
    if (!log || !decoded || !anomalies) {
        std::cerr << "error: cannot open input log or output files\n";
        return 1;
    }
    decoded << "t,can_id,signal,raw,value\n";
    anomalies << "t,can_id,type,detail\n";

    char buf[160];
    auto flag = [&](double t, int id, const char* type, const std::string& detail) {
        std::snprintf(buf, sizeof buf, "%.6f,%03X,%s,", t, id, type);
        anomalies << buf << detail << '\n';
    };

    std::map<std::string, long> count;
    std::map<uint16_t, double> last_seen;  // last good frame time per ID, for timing checks
    double prev_t = -1;
    long lines = 0, frames = 0;

    const auto start = std::chrono::steady_clock::now();
    std::string line;
    can::Frame f;
    while (std::getline(log, line)) {
        ++lines;
        if (!can::parse_line(line, f)) {
            flag(prev_t, 0, "PARSE_ERROR", "line " + std::to_string(lines));
            ++count["PARSE_ERROR"];
            continue;
        }
        if (f.t < prev_t) {
            flag(f.t, f.id, "TIME_REVERSAL", "");
            ++count["TIME_REVERSAL"];
        }
        prev_t = f.t;
        if (f.is_error) {
            flag(f.t, 0, "ERROR_FRAME", "");
            ++count["ERROR_FRAME"];
            continue;
        }
        ++frames;
        const uint16_t crc = can::crc15(can::frame_bits(f.id, f.dlc, f.data));
        if (crc != f.crc) {
            // Corrupted frame: a real controller discards it, so we do too (no decode, no timing).
            std::snprintf(buf, sizeof buf, "got %04X want %04X", f.crc, crc);
            flag(f.t, f.id, "CRC_ERROR", buf);
            ++count["CRC_ERROR"];
            continue;
        }
        auto it = table.find(f.id);
        if (it == table.end()) {
            flag(f.t, f.id, "UNKNOWN_ID", "");
            ++count["UNKNOWN_ID"];
            continue;
        }
        const can::Message& m = it->second;
        if (f.dlc != m.dlc) {
            flag(f.t, f.id, "DLC_MISMATCH", std::to_string(f.dlc) + " != " + std::to_string(m.dlc));
            ++count["DLC_MISMATCH"];
            continue;
        }
        auto seen = last_seen.find(f.id);
        if (seen != last_seen.end()) {
            const double dt = f.t - seen->second;
            const can::Timing tm = can::check_timing(dt, m.period_s);
            if (tm == can::Timing::Gap) {
                // Detail is the number of frames missing, e.g. dt = 3 periods -> 2 missing.
                flag(f.t, f.id, "GAP", std::to_string(std::lround(dt / m.period_s) - 1));
                ++count["GAP"];
            } else if (tm == can::Timing::Jitter) {
                std::snprintf(buf, sizeof buf, "dt %.3f ms", dt * 1000);
                flag(f.t, f.id, "JITTER", buf);
                ++count["JITTER"];
            }
        }
        last_seen[f.id] = f.t;
        for (const can::Signal& s : m.signals) {
            const uint64_t raw = can::extract_raw(s, f.data);
            std::snprintf(buf, sizeof buf, "%.6f,%03X,", f.t, f.id);
            decoded << buf << s.name << ',' << raw << ',';
            std::snprintf(buf, sizeof buf, "%.10g\n", raw * s.scale + s.offset);
            decoded << buf;
        }
        ++count["DECODED"];
    }
    decoded.flush();
    anomalies.flush();
    const double secs = std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count();

    std::ofstream stats(out_dir + "/decode_stats.json");
    stats << "{\n  \"lines\": " << lines << ",\n  \"frames\": " << frames << ",\n  \"seconds\": " << secs
          << ",\n  \"frames_per_second\": " << std::lround(frames / secs);
    for (const auto& kv : count) stats << ",\n  \"" << kv.first << "\": " << kv.second;
    stats << "\n}\n";
    std::cout << "decoded " << frames << " frames in " << secs * 1000 << " ms (" << std::lround(frames / secs)
              << " frames/s), CRC errors " << count["CRC_ERROR"] << ", error frames " << count["ERROR_FRAME"]
              << ", gaps " << count["GAP"] << ", jitter " << count["JITTER"] << "\n";
    return 0;
}
