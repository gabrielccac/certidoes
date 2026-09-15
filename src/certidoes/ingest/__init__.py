"""Generic table-driven certificate ingest (Airtable in, PDFs back).

The engine is source-agnostic: a TOML mapping file declares which table,
which columns feed which source request, and which columns receive status,
summary lines, and attachments. A new base or a renamed field only needs
a new mapping file, never code changes.
"""
