import sys

if sys.version_info < (3, 10):
    sys.exit(
        f"job-hunter needs Python 3.10 or newer; this is Python {sys.version.split()[0]}.\n"
        "On a Mac, install Python from https://www.python.org/downloads/ and recreate the\n"
        "virtual environment with it (see 'Setup' in README.md)."
    )

from jobhunter.cli import main  # noqa: E402

raise SystemExit(main())
