# LFT3-Earth CyberEther plugin.
#
# Provides two blocks:
#   lft3_receiver     DAQIRI (raw ibverbs) receiver for the RFSoC 4x2 channel stream -> GPU tensor
#   lft3_fbh5_writer  Writes integrated spectra to filterbank-HDF5 (FBH5) with real LFT3 metadata
#
# Build inside the CyberEther CUDA container (see scripts/build.sh). The resulting lft3.cep is loaded
# next to Stelline's stelline.cep, whose channelizer/detector/integrator blocks sit between ours.

project(
    'lft3',
    ['c', 'cpp', 'cuda'],
    version: '0.1.0',
    meson_version: '>=1.11.0',
    default_options: [
        'cpp_std=c++20',
        'warning_level=2',
        'buildtype=release',
        'b_ndebug=if-release',
        'cuda_std=c++20',
    ],
)

minimum_jetstream_version = '1.16.0'

if not target_machine.system().startswith('linux')
    error('The LFT3 plugin only supports Linux.')
endif

py = import('python').find_installation(required: true)

#
# Dependencies
#

jetstream_dep = dependency(
    'jetstream',
    version: '>=' + minimum_jetstream_version,
    fallback: ['cyberether', 'libjetstream_dep'],
    required: true,
    default_options: ['tests=false', 'examples=false', 'remote=disabled'],
)

cuda_dep = dependency(
    'cuda',
    version: '>=12.0',
    modules: ['cudart_static', 'cuda'],
    required: true,
)

# DAQIRI only compiles its raw-ibverbs engine when these are present; without it the receiver fails at
# run time with "Engine type 'ibverbs' is not available". Fail the build instead.
cpp = meson.get_compiler('cpp')
foreach lib : ['ibverbs', 'mlx5']
    if not cpp.find_library(lib, required: false).found()
        error('lib' + lib + ' not found: build in the lft3-cyberether image (pipeline/docker/Dockerfile).')
    endif
endforeach
if not cpp.has_header('infiniband/mlx5dv.h')
    error('infiniband/mlx5dv.h not found: install libibverbs-dev (see pipeline/docker/Dockerfile).')
endif

daqiri_dep = subproject(
    'daqiri',
    default_options: ['default_library=static', 'b_staticpic=true'],
).get_variable('daqiri_dep')

# HDF5 upstream only builds with CMake (cmake must be on PATH).
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
hdf5_dep = cmake.subproject('hdf5', options: hdf5_options).dependency('hdf5-static')
meson.override_dependency('hdf5', hdf5_dep)

h5dsc99_dep = subproject(
    'h5dsc99',
    default_options: ['default_library=static', 'b_staticpic=true'],
).get_variable('lib_h5dsc99_dep')

filterbankc99_dep = subproject(
    'filterbankc99',
    default_options: ['default_library=static', 'b_staticpic=true'],
).get_variable('lib_filterbankc99_dep')

dep_lst = [jetstream_dep, cuda_dep, daqiri_dep, hdf5_dep, h5dsc99_dep, filterbankc99_dep]

#
# Sources
#

inc_lst = [include_directories('include', 'src')]
src_lst = []
cuda_lst = []
plugin_test_lst = []

conf = configuration_data()
conf.set_quoted('LFT3_PLUGIN_VERSION', meson.project_version())
configure_file(output: 'lft3_config.hh', configuration: conf)
inc_lst += include_directories('.')

subdir('src')

device_library = static_library(
    'lft3_device',
    cuda_lst,
    include_directories: inc_lst,
    dependencies: [cuda_dep],
    cuda_args: ['-Xcompiler=-fPIC', '--expt-relaxed-constexpr'],
    install: false,
)

plugin_library = static_library(
    'lft3_code',
    src_lst,
    include_directories: inc_lst,
    dependencies: dep_lst,
    link_with: device_library,
    gnu_symbol_visibility: 'hidden',
    pic: true,
    install: false,
)

plugin_code_dep = declare_dependency(
    include_directories: inc_lst,
    dependencies: dep_lst,
    link_whole: plugin_library,
)

if get_option('tests')
    subdir('tests')
endif

#
# Packaging: lft3.cep
#

plugin_module = shared_module(
    'lft3',
    [],
    dependencies: plugin_code_dep,
    gnu_symbol_visibility: 'hidden',
    link_language: 'cpp',
    link_args: ['-Wl,-Bsymbolic', '-Wl,--no-as-needed'],
    name_prefix: '',
    build_by_default: false,
    install: false,
)

plugin_examples = files('examples/lft3-spectrometer.yml', 'examples/lft3-synthetic.yml',
                        'examples/lft3-stelline-chain.yml', 'examples/lft3-monitor.yml',
                        'examples/lft3-monitor-synthetic.yml')

custom_target(
    'lft3_cep',
    input: [plugin_module] + plugin_examples,
    output: 'lft3.cep',
    command: [
        py, files('tools/bundler.py'),
        '--output', '@OUTPUT@',
        '--name', meson.project_name(),
        '--version', meson.project_version(),
        '--minimum-jetstream-version', minimum_jetstream_version,
        '--target', 'path=@INPUT0@,system=' + host_machine.system() + ',device=cuda,arch=' + host_machine.cpu_family(),
        '--example', '@INPUT1@',
        '--example', '@INPUT2@',
        '--example', '@INPUT3@',
        '--example', '@INPUT4@',
        '--example', '@INPUT5@',
    ],
    build_by_default: true,
    install: false,
)
