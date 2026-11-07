# Codex Telegram Bridge 0.9.23

- Windows: a log line that the output's code page cannot show no longer stops the bridge. When the bridge's output went to a file or pipe that uses a legacy code page such as cp1252, logging a Korean message, for example the reason a /clear was refused, raised an encoding error that could stop the bridge before the request finished. Those characters are now written to the log as escape sequences, and the request finishes as usual: a refused /clear ends its turn and tells you in Telegram why it did not run. UTF-8 output, such as a console window, is unchanged.
- The test suite now passes on native Windows. Two question-pin tests read the UTF-8 state file explicitly and use the same session key as the bridge, and a new test runs a refused /clear against a cp1252 output on every platform.

Validation: public export with the internal-symbol audit, private-data and token scans with two independent scanners, manifest parity, 434 tests on Python 3.14, the same suite on native Windows Python 3.12 with cp1252 and cp949 output encodings, twine check, and an installed-wheel import check from a neutral folder in an isolated environment. Source bundle, wheel and source distribution are included.

This release is published on GitHub and PyPI. It does not restart an installed bridge.
