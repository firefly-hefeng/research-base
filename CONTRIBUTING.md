# Contributing

Use Python 3.11 or newer and install the editable package:

```text
python -m pip install -e ".[mcp,dev]"
python -m pytest -q -m "not live"
```

On Windows, a project-local temporary directory can avoid restrictions on the
system temporary folder. Use a new directory for each test run, for example
`--basetemp=.research-base/check-1`.

Keep changes focused on an observable research workflow. Source adapters should
retain provider identifiers, cursors, errors, and actual request counts. Unknown
paper identities, incomplete searches, and missing full text must remain visible.
For relevant behavior changes, add a regression that exercises the shared core
or an actual tool boundary rather than duplicating implementation details.

Tests with `live` markers and the scripts under `scripts/` may contact literature
services, download papers, or use a separately configured model account. They are
explicit local checks and are not part of the offline CI suite. Do not put API
keys, browser leases, cookies, downloaded papers, or local library files in a pull
request. Use environment variables or a local ignored `.env.local` file.

Before publishing files, run:

```text
python scripts/check_public_export.py --strict-links
```

After staging a release, repeat with `--git-index` to inspect the selected files.
The checker is a local pattern and file-selection check; review its findings and
the staged diff before pushing. It does not read environment files or upload data.

Public case reports should include the task and query parameters, source and
request counts, screening decisions by bibliographic identifier, available and
missing full-text counts, and the concrete changes made to a research plan.
Separate retrieved candidates, papers actually read, and experiments actually
run. Summarize results in your own words and link to paper or repository pages;
keep downloaded articles and machine-specific paths in the local library.
