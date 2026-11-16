# lft3-view: fast viewer for the pipeline's filterbank-HDF5 (FBH5) spectra.
#
# Build inside the lft3-runtime container (scripts/build.sh): it has meson/ninja/cmake and zlib, and HDF5
# is built from source as a static library (same wrap as pipeline/). The binaries only need glibc/libstdc++/libz,
# so they run on the host (Ubuntu 24.04) as well as in the container.

project(
    'lft3-view',
    'cpp',
    version: '0.1.0',
    meson_version: '>=1.3.0',
    default_options: [
        'cpp_std=c++20',
        'warning_level=2',
        'buildtype=release',
        'b_ndebug=if-release',
        # always use the cpp-httplib wrap (built without TLS); a system copy may want OpenSSL headers
        'force_fallback_for=cpp-httplib',
    ],
)

cpp = meson.get_compiler('cpp')
add_project_arguments(cpp.get_supported_arguments(['-mcpu=native', '-fopenmp-simd']), language: 'cpp')

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
zlib_dep = dependency('zlib', static: true)
threads_dep = dependency('threads')

core = static_library(
    'lft3view_core',
    ['src/fbh5_file.cc', 'src/fbh5_sequence.cc', 'src/reduce.cc'],
    dependencies: [hdf5_dep, zlib_dep, threads_dep],
)
core_dep = declare_dependency(
    link_with: core,
    include_directories: include_directories('src'),
    dependencies: [hdf5_dep, zlib_dep, threads_dep],
)

executable('lft3-view-bench', 'src/bench.cc', dependencies: [core_dep])

httplib_dep = dependency(
    'cpp-httplib',
    allow_fallback: true,
    default_options: ['cpp-httplib_openssl=disabled', 'cpp-httplib_zlib=disabled', 'cpp-httplib_brotli=disabled'],
)
executable(
    'lft3-view',
    'src/server.cc',
    dependencies: [core_dep, httplib_dep],
    cpp_args: [
        '-DLFT3VIEW_WEB_DIR="' + meson.current_source_dir() / 'web' + '"',
        '-DLFT3VIEW_RFI_FILE="' + meson.current_source_dir() / '..' / '..' / 'config' / 'known_rfi.json' + '"',
    ],
)
