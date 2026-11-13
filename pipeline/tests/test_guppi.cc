#include <catch2/catch_test_macros.hpp>

#include <map>
#include <string>

#include "dump/guppi.hh"

using namespace Lft3;

TEST_CASE("GUPPI RAW header: 80-character cards, END, round trip", "[lft3][dump][guppi]") {
    GuppiHeader h;
    h.set("BLOCSIZE", static_cast<std::int64_t>(25165824));
    h.set("OBSFREQ", 1562.5);
    h.set("SRC_NAME", std::string("LFT3"));
    h.set("TRIGLBL", std::string("it's a test"));
    h.set("OBSFREQ", 2000.25);   // replaces, keeps position
    const std::string s = h.render();
    REQUIRE(s.size() % 80 == 0);
    REQUIRE(s.size() == 5 * 80);
    REQUIRE(s.substr(0, 10) == "BLOCSIZE= ");
    REQUIRE(s.substr(80, 8) == "OBSFREQ ");
    REQUIRE(s.substr(s.size() - 80, 3) == "END");
    REQUIRE(s.find("'LFT3    '") != std::string::npos);   // strings padded to 8 characters
    std::map<std::string, std::string> kv;
    REQUIRE(GuppiHeader::Parse(s.data(), s.size(), kv) == s.size());
    REQUIRE(std::stoll(kv["BLOCSIZE"]) == 25165824);
    REQUIRE(std::stod(kv["OBSFREQ"]) == 2000.25);
    REQUIRE(kv["SRC_NAME"] == "LFT3");
}
