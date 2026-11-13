catch2_dep = dependency(
    'catch2-with-main',
    version: '>=3.0',
    fallback: ['catch2', 'catch2_with_main_dep'],
    default_options: ['default_library=static', 'tests=false', 'cpp_std=c++20'],
)

tests = executable(
    'lft3-tests',
    ['test_capture.cc', 'test_finespec.cc', 'test_fbh5.cc', 'test_ring.cc', 'test_guppi.cc'],
    dependencies: [capture_dep, spec_dep, dump_dep, catch2_dep],
    link_language: 'cpp',
)
test('lft3', tests, timeout: 300)
