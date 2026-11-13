#ifndef LFT3_DUMP_GUPPI_HH
#define LFT3_DUMP_GUPPI_HH

// GUPPI RAW (Breakthrough Listen / hpguppi_daq convention) for raw-beam dumps.
//
// A file is a sequence of blocks, each = an ASCII header of 80-character "KEY     = value" cards ending
// with an END card, followed by BLOCSIZE bytes of data (DIRECTIO = 0: no padding). Data order per block:
// [channel][time][pol][re, im] int8, i.e. exactly one beam of a ring block (Layout: [beam][channel][t][pol]).
// NPOL = 4 means two complex polarisations, NBITS = 8. rawspec, blimpy and hyperseti read this.

#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace Lft3 {

class GuppiHeader {
 public:
    void set(const std::string& key, const std::string& value);   // string card (quoted)
    void set(const std::string& key, double value);
    void set(const std::string& key, std::int64_t value);
    // Header bytes: cards in insertion order + END, each exactly 80 characters.
    std::string render() const;
    // Parses a rendered header (for tests and tools); returns the number of bytes consumed or 0.
    static std::size_t Parse(const char* data, std::size_t size, std::map<std::string, std::string>& out);

 private:
    void put(const std::string& key, const std::string& card);
    std::vector<std::pair<std::string, std::string>> cards_;
};

}  // namespace Lft3

#endif  // LFT3_DUMP_GUPPI_HH
