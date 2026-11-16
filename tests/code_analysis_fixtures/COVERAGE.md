# Analysis corpus ownership

- `host_c` and `host_cpp` are real clangd semantic inputs for navigation, symbols,
  diagnostics and saved-file changes. `test_code_analysis_real_clangd.py` owns
  those assertions through `code_analysis_support.py`.
- `arm_firmware` supplies real Cortex-M C/C++ sources, generated include, linker
  script, ABI file and compilation database template. Native cases in
  `test_code_analysis_managed_runtime_native.py` build it with the explicitly
  supplied pinned vendor compiler. Missing native prerequisites are unverified.
- The installed ARM/build/Cppcheck workflow now lives in Installer
  `release/scripts/test_installed_analysis.py`, with genuine bundles, source
  companion and compiler inputs. It owns build failure, clean/defect/restored
  verdicts, selected-source scope and the actual installed transport boundary.
- Synthetic error replies and negotiated UTF-8/16/32 coordinates use the small
  `fake_clangd.py` peer. Normal language semantics require the real program.

These sources are inputs, not generated reports or a self-testing fixture
platform. Standard runner logs and retained owned inputs preserve failures.
Cross-host source qualification does not prove compiled, installed, model/client
or hardware acceptance; report each missing prerequisite explicitly.
