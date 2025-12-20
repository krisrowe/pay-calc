# Design Decisions

This document captures key architectural decisions and their rationale.

## Data Storage: Flat JSON Files vs Database

**Decision:** Use flat JSON files in a single directory, not a database.

### Rationale

**Performance is not a concern:**
- 129 files (current): 0.004s to list all records
- 3,000 files (10-year projection): ~90ms
- 10,000 files (extreme case): ~300ms
- All well within acceptable CLI response times

**Advantages of flat files:**
- **Git-friendly**: Each record is a separate file with full history
- **Human-readable**: Can inspect/edit records with any text editor
- **Simple**: No schema migrations, no database setup
- **Portable**: Just copy the directory, no export/import needed
- **Debuggable**: Easy to see exactly what's stored

**When to reconsider:**
- 50,000+ files (50+ years of biweekly stubs)
- Complex queries (joins, aggregations)
- Concurrent multi-user access
- Real-time updates from multiple sources

### Directory Structure Evolution

**Original structure (nested):**
```
records/
├── 2024/
│   ├── him/
│   │   └── abc123.json
│   └── her/
│       └── def456.json
└── 2025/
    └── him/
        └── ghi789.json
```

**Current structure (flat):**
```
records/
├── abc123.json  (meta: {year: "2024", party: "him"})
├── def456.json  (meta: {year: "2024", party: "her"})
├── ghi789.json  (meta: {year: "2025", party: "him"})
└── _tracking/
    └── xyz.json (discarded/unrelated files)
```

**Why we flattened:**
- **Single source of truth**: Metadata in JSON, not folder structure
- **Eliminates sync issues**: Can't have file in wrong folder
- **Supports all record types**: Form 1040 has no party, doesn't fit nested structure
- **Simpler code**: No complex directory traversal logic
- **Same performance**: 0.004s for both approaches (tested)

**Migration:** Used `git mv` to preserve file history (105 files moved).

## Import Tracking: Individual Files vs Single Database

**Decision:** Use individual JSON files in `_tracking/` directory.

### Comparison

| Approach | Pros | Cons |
|----------|------|------|
| **Individual files** (current) | Git-friendly, no locking, partial corruption isolated, parallel processing | More inodes, slower "is tracked?" check |
| **Single `tracking.json`** | Faster lookups, atomic updates, less FS overhead | Concurrent access issues, git diffs show entire file, corruption affects all |
| **SQLite database** | Fast queries, ACID transactions, indexes | Not git-friendly, requires export for backup, opaque binary format |

### Current Approach

**Individual tracking files** in `_tracking/` subdirectory:
- Each discarded/unrelated file gets one JSON file
- Filename is hash of Drive file ID
- Contains: `meta.type`, `meta.drive_file_id`, `meta.discard_reason`

**Performance:**
- 2,000 tracking files: ~60ms to check all (acceptable)
- Checking if single file is tracked: ~0.03ms (hash lookup in memory after initial scan)

### When to Reconsider

**Switch to single `tracking.json` if:**
- Tracking file count exceeds 10,000
- Need atomic "mark all files in folder as tracked" operation
- Concurrent imports from multiple processes

**Switch to SQLite if:**
- Tracking file count exceeds 50,000
- Need complex queries ("show all files discarded in last 30 days")
- Need transactional guarantees across multiple operations

### Recommendation

Keep individual files for now. The git-friendliness and simplicity outweigh the minor performance cost. Revisit if tracking grows beyond 5,000 files.

## Record Type Detection

**Decision:** Use `meta.type` field, not folder structure or filename patterns.

### Supported Types

| Type | Description | Year Source | Party Source |
|------|-------------|-------------|--------------|
| `stub` | Pay stub | `data.pay_date[:4]` | `meta.party` |
| `w2` | W-2 form | `data.tax_year` | `meta.party` |
| `form_1040` | IRS Form 1040 | `data.tax_year` | N/A (joint filing) |
| `discarded` | Unprocessable file | N/A | N/A |
| `unrelated` | Non-pay document | N/A | N/A |

### Extensibility

Adding new record types (e.g., `1099`, `state_return`, `schedule_c`) requires:
1. Add type to `RecordType` enum in SDK
2. Add schema validation in `paycalc/schemas/`
3. Add display formatting in `format_record_row()`
4. Add year extraction logic in `records_list()` grouping

No directory structure changes needed.

## Performance Benchmarks

All benchmarks on Linux with SSD, 129 JSON files (~400KB total).

### List All Records (Python)
- **Nested structure**: 0.004s
- **Flat structure**: 0.004s
- **Conclusion**: No performance difference

### List All Records (Shell + jq)
- **129 files**: 2.4s
- **Per file overhead**: ~18ms (spawning jq process)
- **Conclusion**: Python is 600x faster than shell+jq

### Projected Performance
| File Count | Time | Use Case |
|------------|------|----------|
| 129 | 0.004s | Current (2024-2025 data) |
| 3,000 | 0.09s | 10 years of data |
| 10,000 | 0.3s | Extreme case (50+ years) |
| 50,000 | 1.5s | Time to consider SQLite |

## Future Considerations

### If Performance Becomes an Issue

1. **Add in-memory cache**: Load all records once, cache in memory
2. **Add indexes**: Separate index files for year/party lookups
3. **Lazy loading**: Only load records when accessed
4. **SQLite migration**: Preserve JSON as backup, use SQLite for queries

### If Concurrent Access Needed

1. **File locking**: Use `fcntl` for write locks
2. **Optimistic locking**: Add version field, detect conflicts
3. **SQLite**: Built-in ACID transactions

### If Git History Becomes Unwieldy

1. **Separate data repo**: Keep code and data in different repos
2. **Git LFS**: Store JSON files in Git Large File Storage
3. **Periodic archival**: Move old years to archive repo
