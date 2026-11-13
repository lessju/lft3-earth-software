catch2_dep = dependency(
    'catch2',
    version: '>=3.0',
    fallback: ['catch2', 'catch2_dep'],
    default_options: ['default_library=static', 'tests=false', 'cpp_std=c++20'],
)

test_main = static_library('lft3-test-main', 'main.cc', dependencies: [plugin_code_dep, catch2_dep])

fs = import('fs')
foreach test_source : plugin_test_lst
    component = fs.name(fs.parent(test_source))
    exe = executable(
        'lft3-' + component + '-tests',
        test_source,
        dependencies: [plugin_code_dep, catch2_dep],
        link_with: test_main,
        link_language: 'cpp',
        link_args: ['-Wl,--no-as-needed'],
    )
    test(component, exe, suite: 'lft3', is_parallel: false, timeout: 120)
endforeach
