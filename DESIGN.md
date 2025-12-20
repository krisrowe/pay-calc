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

## Alternative Data Management Approaches

This section documents alternatives to the current flat JSON file approach, with detailed pros/cons and migration guidance.

### Option 1: SQLite Database (Recommended for 10k+ files)

**Use case:** When file count exceeds 10,000 or need complex queries.

**Structure:**
```sql
CREATE TABLE records (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    year TEXT,
    party TEXT,
    data JSON NOT NULL,
    meta JSON NOT NULL,
    imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_year_party ON records(year, party);
CREATE INDEX idx_type ON records(type);
```

**Pros:**
- ✅ Instant lookups with indexes (O(log n) vs O(n))
- ✅ No memory overhead (doesn't load all records)
- ✅ Complex queries (joins, aggregations, date ranges)
- ✅ ACID transactions (atomic updates)
- ✅ Built into Python (no dependencies)
- ✅ Scales to millions of records

**Cons:**
- ❌ Binary format (not human-readable)
- ❌ Terrible for git (binary diffs bloat history)
- ❌ Requires export for manual inspection
- ❌ Schema migrations needed for structure changes

**Git strategy:**
- Store SQLite DB **outside git** (`~/.local/share/pay-calc/records.db`)
- Export to JSON for backup/versioning
- Or use separate data repo (not version-controlled)

**Migration path:**
```python
# One-time migration
import sqlite3
import json
from pathlib import Path

conn = sqlite3.connect('records.db')
conn.execute('''CREATE TABLE records (...)''')

for json_file in Path('records').glob('*.json'):
    with open(json_file) as f:
        record = json.load(f)
    conn.execute(
        'INSERT INTO records VALUES (?, ?, ?, ?, ?, ?)',
        (json_file.stem, record['meta']['type'], 
         record['meta'].get('year'), record['meta'].get('party'),
         json.dumps(record['data']), json.dumps(record['meta']))
    )
conn.commit()
```

### Option 2: JSONL (JSON Lines) for Tracking

**Use case:** Tracking Drive file IDs (append-heavy workload).

**Structure:**
```jsonl
{"id": "1abc...", "reason": "discarded", "date": "2024-12-20"}
{"id": "2def...", "reason": "unrelated", "date": "2024-12-21"}
```

**Pros:**
- ✅ Append-only (no file rewrites)
- ✅ Git-friendly (small deltas per addition)
- ✅ Streamable (process line-by-line)
- ✅ Partial corruption doesn't affect whole file
- ✅ Human-readable

**Cons:**
- ❌ Slower lookups (must scan entire file)
- ❌ Deletions require rewriting file
- ❌ No built-in indexing

**Implementation:**
```python
# Append new entry
with open('tracking.jsonl', 'a') as f:
    f.write(json.dumps({"id": file_id, "reason": reason}) + '\n')

# Check if tracked (load into set at startup)
tracked = set()
with open('tracking.jsonl') as f:
    for line in f:
        entry = json.loads(line)
        tracked.add(entry['id'])
```

**When to use:**
- Tracking grows beyond 5,000 entries
- Want git-friendly tracking
- Rarely need to remove entries

### Option 3: Single JSON File for Tracking

**Use case:** Small tracking dataset (< 5,000 entries), need fast lookups.

**Structure:**
```json
{
  "tracked_files": {
    "1abc...": {"reason": "discarded", "date": "2024-12-20"},
    "2def...": {"reason": "unrelated", "date": "2024-12-21"}
  }
}
```

**Pros:**
- ✅ Fast lookups (hash table in memory)
- ✅ Simple implementation
- ✅ Human-readable
- ✅ Easy to edit manually

**Cons:**
- ❌ Must load entire file into memory
- ❌ Must rewrite entire file on update
- ❌ Git diffs show entire file changed
- ❌ Concurrent access issues

**When to use:**
- Tracking < 5,000 entries
- Single-user, no concurrent access
- Don't care about git history for tracking

### Option 4: Hybrid Approach (Recommended)

**Use different storage for different data types:**

| Data Type | Storage | Rationale |
|-----------|---------|-----------|
| **Pay stubs** | Individual JSON files | Git-friendly, human-readable, audit trail |
| **W-2s** | Individual JSON files | Same as stubs |
| **Form 1040** | Individual JSON files | Important to version, rarely changes |
| **Tracking** | SQLite or JSONL | Append-heavy, don't need git history |
| **Indexes** | SQLite (optional) | Fast lookups, generated from JSON |

**Benefits:**
- ✅ Best tool for each job
- ✅ Critical data in git (stubs, W-2s)
- ✅ Performance data outside git (tracking, indexes)
- ✅ Can rebuild indexes from JSON source of truth

**Implementation:**
```
~/.local/share/pay-calc/
├── records/              # Git-tracked JSON files
│   ├── abc123.json       # Stubs, W-2s, Form 1040
│   └── ...
├── tracking.db           # SQLite (not in git)
└── indexes.db            # SQLite (not in git, optional)
```

### Option 5: Separate Data Repository

**Use case:** Want version control but keep data separate from code.

**Structure:**
```
personal-pay-calc/        # Code repo (public or private)
personal-pay-calc-data/   # Data repo (private, git or GCS)
```

**Pros:**
- ✅ Clean separation of code and data
- ✅ Can make code repo public
- ✅ Data repo can use different backup strategy
- ✅ Easier to manage data size

**Cons:**
- ❌ More complex setup (two repos)
- ❌ Need to configure data path
- ❌ Harder to keep in sync

**When to use:**
- Want to open-source the tool
- Data repo grows very large (> 1GB)
- Need different access controls for code vs data

## Migration Decision Tree

```
Start: Current flat JSON files (< 1,000 records)
│
├─ Need faster lookups? (> 10,000 records)
│  └─ YES → Migrate to SQLite (store outside git)
│
├─ Git history too large? (> 100MB)
│  └─ YES → Move to separate data repo or GCS
│
├─ Tracking files growing? (> 5,000 entries)
│  ├─ Want git history? → Use JSONL
│  └─ Don't care about git? → Use SQLite
│
└─ Everything working fine?
   └─ YES → Keep current approach!
```

## Recommendations by Scale

| Record Count | Tracking Count | Recommendation |
|--------------|----------------|----------------|
| < 1,000 | < 1,000 | **Current approach** (flat JSON) |
| 1,000 - 10,000 | 1,000 - 5,000 | Current approach, consider JSONL for tracking |
| 10,000 - 50,000 | 5,000 - 10,000 | SQLite for tracking, keep JSON for records |
| 50,000+ | 10,000+ | **Full SQLite migration**, export JSON for backup |

## Performance Comparison

Benchmarks for "check if file is tracked" operation:

| Approach | 1,000 entries | 10,000 entries | 100,000 entries |
|----------|---------------|----------------|-----------------|
| Individual files | 60ms | 600ms | 6s |
| Single JSON | 10ms | 100ms | 1s |
| JSONL | 20ms | 200ms | 2s |
| SQLite | 0.1ms | 0.1ms | 0.1ms |

**Conclusion:** SQLite wins at scale, but current approach is fine for < 10k files.

