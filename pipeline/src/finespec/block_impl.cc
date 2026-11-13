#include <lft3/finespec/block.hh>
#include <lft3/finespec/module.hh>
#include <jetstream/detail/block_impl.hh>

namespace Jetstream::Blocks {

struct Lft3FineSpecImpl : public Block::Impl, public DynamicConfig<Blocks::Lft3FineSpec> {
    Result configure() override {
        moduleConfig->mode = mode;
        moduleConfig->beams = beams;
        moduleConfig->integrationBlocks = integrationBlocks;
        moduleConfig->binsPerOutput = binsPerOutput;
        return Result::SUCCESS;
    }

    Result define() override {
        JST_CHECK(defineInterfaceInput("buffer", "Input", "Voltages [beams*segments, channels, fftLength, 2] (CI8/CF32)."));
        JST_CHECK(defineInterfaceOutput("buffer", "Output", "Integrated Stokes I [beams, channels*fftLength, 1, 1] F32."));
        JST_CHECK(defineInterfaceConfig("mode", "Mode", "fine: FFT each coarse channel; coarse: power per coarse channel.",
                                        {{"type", "dropdown"}, {"options", Parser::Sequence{
                                            Parser::Map{{"label", "Fine (FFT)"}, {"value", "fine"}},
                                            Parser::Map{{"label", "Coarse (no FFT)"}, {"value", "coarse"}},
                                        }}}));
        JST_CHECK(defineInterfaceConfig("beams", "Beams", "Beams folded into the first input axis.",
                                        {{"type", "uint"}}));
        JST_CHECK(defineInterfaceConfig("integrationBlocks", "Integration", "Input blocks per output spectrum.",
                                        {{"type", "uint"}, {"unit", "blocks"}}));
        JST_CHECK(defineInterfaceConfig("binsPerOutput", "Bins Per Output",
                                        "Channels summed per output value: fine channels in fine mode, coarse channels in coarse mode.",
                                        {{"type", "uint"}}));
        return Result::SUCCESS;
    }

    Result create() override {
        JST_CHECK(moduleCreate("lft3_finespec", moduleConfig, {{"buffer", inputs().at("buffer")}}));
        JST_CHECK(moduleExposeOutput("buffer", {"lft3_finespec", "buffer"}));
        return Result::SUCCESS;
    }

 protected:
    std::shared_ptr<Modules::Lft3FineSpec> moduleConfig = std::make_shared<Modules::Lft3FineSpec>();
};

JST_REGISTER_BLOCK(Lft3FineSpecImpl, {"lft3_finespec"});

}  // namespace Jetstream::Blocks
