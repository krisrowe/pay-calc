"""
Records management for pay stubs and W-2s.

Design Rationale
----------------

Discard vs Import distinction:
    When importing records from Drive, files that can't be processed are "discarded"
    rather than deleted. A discarded record is stored locally with meta.type="discarded"
    so we know not to re-download it on subsequent imports.

    IMPORTANT: "already imported" and "previously discarded" are different outcomes:
    - "already imported" → record exists, visible in `records list`
    - "previously discarded" → marker exists, hidden from list, use --force to retry

Why we don't distinguish "not a record" from "unsupported format":
    It's impossible to automatically tell whether a PDF is:
    - Genuinely not a pay stub or W-2 (random document in the folder)
    - A pay stub/W-2 from a provider whose format we don't support yet

    Therefore, --force retries ALL discards without trying to be clever about
    which ones are "retriable". If this causes waste (re-processing lots of
    irrelevant files), the user should organize their Drive folder to only
    contain relevant documents.

Discard reasons (meta.discard_reason):
    - "not_recognized": couldn't identify as stub or W-2 (includes unknown formats)
    - "unknown_party": detected type but employer doesn't match config
    - "unreadable": couldn't extract text from PDF
    - "parse_failed": detected type but couldn't extract structured data
"""

# TODO: Implement records import/list/export functionality
