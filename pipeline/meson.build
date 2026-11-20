# LFT3-Earth real-time runtime (docs/runtime.md, docs/runtime_guide.md).
#
#   lft3-capture       one link (DAQIRI raw ibverbs) or a synthetic / pcap source -> the voltage ring
#   lft3-spectrometer  ring reader: fused fine spectrometer or coarse power -> FBH5 (+ latest snapshot)
#   lft3-dump          ring reader: triggered raw-voltage window -> GUPPI RAW
#
# Build inside the lft3-runtime image (scripts/build.sh): CUDA 13, rdma-core, meson, cmake.

project(
    'lft3',
    ['c', 'cpp', 'cuda'],
    version: '0.2.0',
    meson_version: '>=1.4.0',
    default_options: [
        'cpp_std=c++20',
        'warning_level=2',
        'buildtype=release',
        'b_ndebug=if-release',
        'cuda_std=c++20',
    ],
)

add_project_arguments('-DLFT3_VERSION="' + meson.project_version() + '"', language: ['cpp', 'cuda'])
cuda_arch_args = ['-arch=' + get_option('gpu_arch'), '--expt-relaxed-constexpr']

cuda_dep = dependency('cuda', version: '>=12.0', modules: ['cudart_static', 'cuda'], required: true)

# DAQIRI only compiles its raw-ibverbs engine when these are present; without it capture would fail at run time
# with "Engine type 'ibverbs' is not available". Fail the build instead.
cpp = meson.get_compiler('cpp')
foreach lib : ['ibverbs', 'mlx5']
    if not cpp.find_library(lib, required: false).found()
        error('lib' + lib + ' not found: build in the lft3-runtime image (pipeline/docker/Dockerfile).')
    endif
endforeach
if not cpp.has_header('infiniband/mlx5dv.h')
    error('infiniband/mlx5dv.h not found: install libibverbs-dev (see pipeline/docker/Dockerfile).')
endif

daqiri_dep = subproject('daqiri', default_options: ['default_library=static', 'b_staticpic=true']).get_variable('daqiri_dep')

# HDF5 upstream only builds with CMake.
cmake = import('cmake')
hdf5_options = cmake.subproject_options()
hdf5_options.add_cmake_defines({
    'BUILD_SHARED_LIBS': false,
    'BUILD_STATIC_LIBS': true,
    'CMAKE_POSITION_INDEPENDENT_CODE': true,
    'HDF5_BUILD_CPP_LIB': false,
    'HDF5_BUILD_EXAMPLES': false,
    'HDF5_BUILD_FORTRAN': false,
    'HDF5_BUILD_HL_LIB': false,
    'HDF5_BUILD_JAVA': false,
    'HDF5_BUILD_TOOLS': false,
    'BUILD_TESTING': false,
})
# The static library needs libm / libdl / threads from the consumer.
hdf5_dep = declare_dependency(dependencies: [
    cmake.subproject('hdf5', options: hdf5_options).dependency('hdf5-static'),
    cpp.find_library('m'), cpp.find_library('dl'), dependency('threads'),
])
meson.override_dependency('hdf5', hdf5_dep)
h5dsc99_dep = subproject('h5dsc99', default_options: ['default_library=static', 'b_staticpic=true']).get_variable('lib_h5dsc99_dep')
filterbankc99_dep = subproject('filterbankc99', default_options: ['default_library=static', 'b_staticpic=true']).get_variable('lib_filterbankc99_dep')

inc = include_directories('src')

common_lib = static_library('lft3_common', ['src/common/ring.cc'], include_directories: inc,
                            dependencies: [cuda_dep])
common_dep = declare_dependency(include_directories: inc, link_with: common_lib, dependencies: [cuda_dep])

capture_lib = static_library(
    'lft3_capture',
    ['src/capture/assembler.cc', 'src/capture/daqiri_config.cc', 'src/capture/pcap.cc', 'src/capture/scatter.cu',
     'src/capture/synth.cu'],
    include_directories: inc,
    dependencies: [cuda_dep, daqiri_dep],
    cuda_args: cuda_arch_args,
)
capture_dep = declare_dependency(link_with: capture_lib, dependencies: [common_dep, daqiri_dep])

zlib_dep = dependency('zlib')
spec_lib = static_library(
    'lft3_spectrometer',
    ['src/spectrometer/finespec.cu', 'src/spectrometer/fbh5_file.cc'],
    include_directories: inc,
    dependencies: [cuda_dep, hdf5_dep, h5dsc99_dep, filterbankc99_dep, zlib_dep],
    cuda_args: cuda_arch_args,
)
spec_dep = declare_dependency(link_with: spec_lib, dependencies: [common_dep, hdf5_dep, h5dsc99_dep, filterbankc99_dep,
                                                                   zlib_dep])

dump_lib = static_library('lft3_dump', ['src/dump/guppi.cc'], include_directories: inc)
dump_dep = declare_dependency(link_with: dump_lib, dependencies: [common_dep])

executable('lft3-capture', 'src/capture/main.cc', dependencies: [capture_dep], link_language: 'cpp')
executable('lft3-spectrometer', 'src/spectrometer/main.cc', dependencies: [spec_dep], link_language: 'cpp')
executable('lft3-dump', 'src/dump/main.cc', dependencies: [dump_dep, spec_dep], link_language: 'cpp')

if get_option('tests')
    subdir('tests')
endif
