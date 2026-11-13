#ifndef LFT3_FBH5_WRITER_DETAIL_TIMELINE_HH
#define LFT3_FBH5_WRITER_DETAIL_TIMELINE_HH

// Recovers the time axis of the integrated spectra from what the writer can observe.
//
// Each time the integrator emits a spectrum, the receiver's "timestamp" attribute holds the first
// spectrum counter t of the *last* block that went into it, and lft3.totalMissingPackets the
// cumulative count of zero-filled packets. From two consecutive emissions:
//     interval (spectra per integration) = lastBlockT[k] - lastBlockT[k-1]
//     start of integration k             = lastBlockT[k] - interval + spectraPerBlock
// so the integration length never has to be configured twice. A change of interval (firmware re-sync,
// whole blocks lost, integrator reconfigured) is reported as a discontinuity: the writer closes its
// files and starts again.

#include <cstdint>

namespace Lft3 {

class IntegrationTimeline {
 public:
    enum class Step { First, Next, Discontinuity };

    struct Spectrum {
        std::uint64_t startT = 0;
        std::uint64_t missing = 0;
    };

    explicit IntegrationTimeline(std::uint64_t spectraPerBlock) : spectraPerBlock_(spectraPerBlock) {}

    // First:          nothing can be written yet; hold the data (interval unknown).
    // Next:           `current` is valid; if this is the second spectrum, firstSpectrum() is valid too.
    // Discontinuity:  drop the time base; hold this spectrum as a new First.
    Step push(std::uint64_t lastBlockT, std::uint64_t totalMissing, Spectrum& current) {
        if (!haveFirst_) {
            startOver(lastBlockT, totalMissing, baselineMissing_);
            return Step::First;
        }
        const bool forward = lastBlockT > prevT_;
        const std::uint64_t delta = forward ? lastBlockT - prevT_ : 0;
        if (!forward || delta % spectraPerBlock_ != 0 || (interval_ != 0 && delta != interval_)) {
            startOver(lastBlockT, totalMissing, prevMissing_);
            return Step::Discontinuity;
        }
        if (interval_ == 0) {
            interval_ = delta;
            first_.startT = prevT_ + spectraPerBlock_ - interval_;
            first_.missing = prevMissing_ - firstBaseline_;
        }
        current.startT = lastBlockT + spectraPerBlock_ - interval_;
        current.missing = totalMissing - prevMissing_;
        prevT_ = lastBlockT;
        prevMissing_ = totalMissing;
        return Step::Next;
    }

    std::uint64_t interval() const { return interval_; }
    const Spectrum& firstSpectrum() const { return first_; }

 private:
    void startOver(std::uint64_t lastBlockT, std::uint64_t totalMissing, std::uint64_t baseline) {
        haveFirst_ = true;
        interval_ = 0;
        prevT_ = lastBlockT;
        prevMissing_ = totalMissing;
        firstBaseline_ = baseline;
    }

    std::uint64_t spectraPerBlock_;
    bool haveFirst_ = false;
    std::uint64_t interval_ = 0;
    std::uint64_t prevT_ = 0;
    std::uint64_t prevMissing_ = 0;
    std::uint64_t firstBaseline_ = 0;
    std::uint64_t baselineMissing_ = 0;
    Spectrum first_;
};

}  // namespace Lft3

#endif  // LFT3_FBH5_WRITER_DETAIL_TIMELINE_HH
