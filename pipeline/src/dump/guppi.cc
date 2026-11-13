#include "dump/guppi.hh"

#include <cstdio>
#include <cstring>

namespace Lft3 {

namespace {
std::string Key(const std::string& k) {
    std::string s = k.substr(0, 8);
    s.resize(8, ' ');
    return s;
}
std::string Pad80(std::string s) {
    s.resize(80, ' ');
    return s;
}
}  // namespace

void GuppiHeader::put(const std::string& key, const std::string& card) {
    for (auto& c : cards_) {
        if (c.first == key) {
            c.second = card;
            return;
        }
    }
    cards_.emplace_back(key, card);
}

void GuppiHeader::set(const std::string& key, const std::string& value) {
    std::string v = value.substr(0, 68);
    std::string quoted;
    for (char c : v) quoted += (c == '\'') ? std::string("''") : std::string(1, c);
    if (quoted.size() < 8) quoted.resize(8, ' ');   // FITS: string values at least 8 characters
    put(key, Pad80(Key(key) + "= '" + quoted + "'"));
}

void GuppiHeader::set(const std::string& key, double value) {
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%20.12G", value);
    put(key, Pad80(Key(key) + "= " + buf));
}

void GuppiHeader::set(const std::string& key, std::int64_t value) {
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%20lld", static_cast<long long>(value));
    put(key, Pad80(Key(key) + "= " + buf));
}

std::string GuppiHeader::render() const {
    std::string out;
    out.reserve((cards_.size() + 1) * 80);
    for (const auto& c : cards_) out += c.second;
    out += Pad80("END");
    return out;
}

std::size_t GuppiHeader::Parse(const char* data, std::size_t size, std::map<std::string, std::string>& out) {
    for (std::size_t pos = 0; pos + 80 <= size; pos += 80) {
        std::string card(data + pos, 80);
        if (card.compare(0, 3, "END") == 0 && card.find_first_not_of(' ', 3) == std::string::npos) return pos + 80;
        std::string key = card.substr(0, 8);
        key.erase(key.find_last_not_of(' ') + 1);
        std::string val = card.substr(10);
        val.erase(0, val.find_first_not_of(' '));
        if (!val.empty() && val[0] == '\'') {
            const auto end = val.find('\'', 1);
            val = val.substr(1, end == std::string::npos ? std::string::npos : end - 1);
            val.erase(val.find_last_not_of(' ') + 1);
        } else {
            val.erase(val.find_last_not_of(' ') + 1);
        }
        out[key] = val;
    }
    return 0;
}

}  // namespace Lft3
