# Working in runmanager

`runmanager/__main__.py` builds the `Splash` and `QApplication` at module scope,
so importing it anywhere but a real start puts a banner on the screen that is
never hidden. Import a leaf module instead (`runmanager.queueing`,
`globals_file`, `analysis_submission`). A test that must borrow a method of
`__main__` takes it from `tests/fixtures.py`, which stubs the splash first.

Workspace conventions are in `../AGENTS.md`.
