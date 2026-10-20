"""Internal modules for the gobo-agent harness (imported by ``gsdev.py``).

Kept as a package so an adopted runtime ships one directory, while ``gsdev.py``
remains the CLI entrypoint and imports these with aliases. Every module here is
standard-library only and must not import ``gsdev`` (no cycles).
"""
