# Test Fixtures

Synthetic input data for end-to-end testing of the records import workflow.

## Design Goals

1. **Test real code paths**: These tests exercise production code end-to-end. PDF reading, text extraction, YAML parser loading, pattern matching, validation, and record storage all run exactly as they would in production.

2. **Minimal mocking**: The only thing stubbed is network I/O (Gemini OCR). Everything else runs for real.

3. **Synthetic inputs only**: The difference from production is purely the input data:
   - Generic PDF documents (not real employer pay stubs)
   - Generic YAML parsers (patterns that match the generic PDFs)

4. **No dependency on real employer data**: Tests can run in CI, on any machine, without access to actual financial documents or employer-specific parser configurations.

## Why This Approach

Real integration tests require real inputs. By creating synthetic PDFs and matching YAML parsers, we get true end-to-end coverage without exposing personal financial data or employer-specific document formats.

The test validates that the entire pipeline works: PDF → text extraction → parser matching → field extraction → validation → storage → retrieval.

## Files

### PDF Documents

Valid PDF files generated once using `fpdf2`. They contain extractable text in a simple, generic format.

| File | Type | Expected Behavior |
|------|------|-------------------|
| `stub_2025-06-15.pdf` | Pay stub | Parsed by `acme_stub_parser.yaml`, imported |
| `stub_2025-06-30.pdf` | Pay stub | Parsed by `acme_stub_parser.yaml`, imported |
| `w2_2024.pdf` | W-2 | Text parsing fails, triggers OCR fallback (mocked) |
| `w4_2025.pdf` | W-4 | No matching parser, discarded |

**Content is entirely synthetic:**
- "Acme Corp" employer (classic placeholder name)
- Arbitrary round-number amounts
- Standard payroll terminology (Gross Pay, Federal Tax, etc.)
- Simple Current/YTD column layout

This structure is intentionally generic - not modeled after any real employer's document format.

### YAML Parser

`acme_stub_parser.yaml` defines patterns that match the synthetic stub PDFs. It uses the same schema as production parsers - only the patterns are different because the input documents are different.

## Test Execution Flow

```
1. Copy fixtures to isolated temp directory
2. Configure environment:
   - PAY_CALC_CONFIG_PATH → temp config dir
   - settings.json → temp data dir
   - profile.yaml → defines "Acme Corp" employer
   - parsers/ → contains acme_stub_parser.yaml
3. Stub Gemini (network I/O only)
4. Run production import code
5. Assert results
```

The production code runs unchanged - it reads the PDFs, extracts text, loads whatever YAML parsers it finds, matches patterns, validates against schemas, and saves records. The test just verifies the outcomes.
