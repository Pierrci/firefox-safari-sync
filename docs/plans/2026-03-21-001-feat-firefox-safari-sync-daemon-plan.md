---
title: Firefox → Safari Continuous Sync Daemon
type: feat
status: active
date: 2026-03-21
origin: docs/brainstorms/2026-03-21-firefox-safari-sync-requirements.md
---

# Firefox → Safari Continuous Sync Daemon

## Overview

A lightweight macOS LaunchAgent (Python) that runs every 5 minutes and pushes three types of data from a local Firefox profile into Safari's on-disk files: open tabs (as a Safari bookmarks folder), bookmarks (mirroring folder structure), and browsing history (incremental). iCloud then propagates the Safari-side changes to Safari on iOS, giving the user a seamless cross-device experience while keeping Firefox as their primary desktop browser.

---

## Problem Statement

The user uses Firefox on macOS but prefers Safari on iOS for its native integration and iCloud sync. There is no built-in bridge. The daemon closes this gap by acting as a one-way translation layer from Firefox's local data files into Safari's local data files, relying on iCloud to handle device propagation.

See origin: `docs/brainstorms/2026-03-21-firefox-safari-sync-requirements.md`

---

## Proposed Solution

A single Python script (`sync.py`) runs as a macOS LaunchAgent on a 5-minute `StartInterval`. Each cycle:

1. Reads Firefox open tabs from `recovery.jsonlz4`
2. Reads Firefox bookmarks and history from a copy of `places.sqlite`
3. Writes tabs as a "Firefox Tabs" bookmarks folder in `Bookmarks.plist`
4. Writes bookmarks as a "Firefox" bookmarks folder tree in `Bookmarks.plist`
5. Writes new history entries into Safari `History.db`
6. Updates a local watermark file to track sync progress

An `install.sh` script handles setup: writes the LaunchAgent plist, installs Python deps, and prints Full Disk Access instructions.

---

## Technical Approach

### Architecture

```
firefox-safari-sync/
├── sync.py                  # Main daemon script (single file)
├── install.sh               # Setup: venv, launchd plist, FDA instructions
├── uninstall.sh             # Teardown: bootout, remove plist
├── requirements.txt         # lz4
├── com.user.firefox-safari-sync.plist   # LaunchAgent template
└── docs/
    ├── brainstorms/
    └── plans/
```

State file (written by daemon, outside repo):
```
~/.config/firefox-safari-sync/state.json
```

Logs:
```
~/Library/Logs/firefox-safari-sync/stdout.log
~/Library/Logs/firefox-safari-sync/stderr.log
```

### Language and Dependencies

- **Python 3.10+** (stdlib-heavy; one external dependency)
- **`lz4`** — for decompressing Firefox's mozLz4 session files
- **`plistlib`** (stdlib) — reading and writing Safari's binary `Bookmarks.plist`
- **`sqlite3`** (stdlib) — reading `places.sqlite` (copy) and writing `History.db`
- **`configparser`** (stdlib) — parsing Firefox `profiles.ini`
- **`uuid`** (stdlib) — generating UUIDs for new Safari bookmark nodes

No Swift, no AppleScript, no third-party plist libraries (plistlib in Python 3 handles binary plist natively).

### Implementation Phases

#### Phase 1: Firefox Data Readers

**1a. Profile Detection (`profile.py` inline in `sync.py`)**

```python
# ~/Library/Application Support/Firefox/profiles.ini
# 1. Check [Install...] sections for Default= key (Firefox 67+)
# 2. Fall back to [Profile#] sections with Default=1
# 3. Resolve IsRelative=1 paths relative to profiles.ini parent directory
```

- Default to auto-detection; no user config required initially
- If zero or multiple installs resolve, log a warning and pick the first valid directory
- Profile path stored in `state.json` after first resolution to avoid repeated re-detection

**1b. mozLz4 Session Reader**

```python
MAGIC = b"mozLz40\0"

def read_mozlz4(path: Path) -> dict:
    with open(path, "rb") as fh:
        assert fh.read(8) == MAGIC
        raw = fh.read()          # includes 4-byte size prefix lz4.block expects
    return json.loads(lz4.block.decompress(raw))
```

- Read `<profile>/sessionstore-backups/recovery.jsonlz4`
- File only exists while Firefox is running; if absent, skip tabs sync for this cycle (not an error)
- Retry up to 3 times with 200ms delay if file size < 12 bytes (mid-write detection)
- `sessionstore.index` is **1-based**: `entries[index - 1]` is the active page

**1c. places.sqlite Reader (bookmarks + history)**

```python
# Primary: immutable=1 URI — bypasses Firefox's EXCLUSIVE WAL lock, no temp file needed
uri = f"file:{places_path}?immutable=1&mode=ro"
conn = sqlite3.connect(uri, uri=True)
conn.row_factory = sqlite3.Row
```

- Use the SQLite `immutable=1` URI flag to open `places.sqlite` read-only, bypassing Firefox's `PRAGMA locking_mode=EXCLUSIVE` lock
- `immutable=1` skips all locking and change detection — safe for a read-only daemon; may read data that is very slightly stale (WAL frames not yet checkpointed), which is acceptable for a 5-minute sync window
- **Do not** use `shutil.copy2` to copy the database files. `shutil.copy2` is not atomic: if Firefox commits a WAL transaction mid-copy, the copied `-wal` and main file will be mismatched, producing a corrupted SQLite file and a `DatabaseError`
- Wrap in `try/except sqlite3.DatabaseError` and skip the cycle if the DB is unreadable (rare, only if Firefox is in the middle of a checkpoint)
- Close the connection immediately after reading; never hold it open across operations
- All Firefox date values are **microseconds** since Unix epoch: divide by 1,000,000
- Bookmark tree query: recursive walk via `moz_bookmarks.parent` FK, `type=1` (bookmark) and `type=2` (folder)

#### Phase 2: Safari Writers

**2a. Bookmarks.plist Writer**

```python
PLIST_PATH = Path.home() / "Library/Safari/Bookmarks.plist"

with open(PLIST_PATH, "rb") as f:
    data = plistlib.load(f)

# Locate or create "Firefox Tabs" and "Firefox" folders in BookmarksBar
# Apply diff-based merge using deterministic UUIDs
# Write atomically:
with tempfile.NamedTemporaryFile(dir=PLIST_PATH.parent, delete=False, suffix=".plist") as tmp:
    plistlib.dump(data, tmp, fmt=plistlib.FMT_BINARY)
os.replace(tmp.name, PLIST_PATH)
```

**Merge strategy: deterministic UUIDs + diff-based in-place update.** The original "delete-and-recreate" approach is **not used** because iCloud tracks bookmark nodes by UUID. Deleting and recreating nodes with new random UUIDs on every 5-minute cycle would cause massive, continuous iCloud sync churn (288 delete+recreate cycles per day), potentially triggering account throttling and causing constant visual disruption on iOS devices.

Instead:
- **Deterministic UUIDs**: generate UUIDs via `uuid.uuid5(uuid.NAMESPACE_URL, stable_key)` where `stable_key` is the Firefox bookmark GUID (for bookmarks) or the URL (for tabs). The same Firefox bookmark always produces the same Safari UUID, so iCloud sees the node as unchanged if its content hasn't changed.
- **Diff-based merge**: on each cycle, compare the current Firefox state against the existing Safari plist nodes:
  - Node UUID in Firefox but not in Safari → add node
  - Node UUID in both, content unchanged → skip (no write, no iCloud event)
  - Node UUID in both, title or URL changed → update node in place
  - Node UUID in Safari "Firefox" folder but not in current Firefox → remove node

```python
import uuid

UUID_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # NAMESPACE_URL

def stable_uuid(key: str) -> str:
    """Deterministic UUID from a Firefox GUID or URL. Same input → same UUID always."""
    return str(uuid.uuid5(UUID_NAMESPACE, key)).upper()
```

**Folder structure:**
```
BookmarksBar
├── Firefox Tabs/      ← diff-updated every cycle with current open tabs
│   ├── Tab Title 1   (url)   UUID = uuid5(url)
│   └── Tab Title 2   (url)   UUID = uuid5(url)
└── Firefox/           ← diff-updated every cycle
    ├── Bookmarks Toolbar/    UUID = uuid5(firefox_folder_guid)
    │   └── ...
    └── Other Bookmarks/      UUID = uuid5(firefox_folder_guid)
        └── ...
```

**iCloud behavior with deterministic UUIDs:** If a bookmark's UUID and content are unchanged between cycles, the plist write still occurs (we always rewrite the file) but the `cloudd`/`bird` sync daemon will detect no node-level changes and will not push a sync event. iCloud churn is eliminated for unchanged bookmarks.

**Safari in-memory state:** Safari monitors `~/Library/Safari/` via FSEvents/kqueue and re-reads `Bookmarks.plist` when it changes on disk. The same mechanism is used by iCloud's `cloudd` daemon when syncing — Safari is designed to handle external writes to this file. The atomic write (`os.replace`) guarantees Safari never sees a torn file. If Safari's in-memory state is stale and overwrites the daemon's write on quit, the next 5-minute cycle will self-heal. This is a known, accepted limitation.

**iCloud race condition:** If `cloudd`/`bird` overwrites `Bookmarks.plist` after the daemon writes it (e.g., a remote change arriving from iOS), the next 5-minute cycle will re-apply Firefox's data. Eventual consistency via the sync cadence. Known limitation.

**2b. History.db Writer**

```python
DB_PATH = Path.home() / "Library/Safari/History.db"
COCOA_OFFSET = 978307200  # seconds between Unix epoch (1970) and Cocoa epoch (2001)

# visit_time in History.db = time.time() - COCOA_OFFSET  (REAL, not INTEGER)
```

- **Incremental sync:** Watermark stored in `~/.config/firefox-safari-sync/state.json` as `last_history_sync_unix` (Unix timestamp in seconds)
- On first run: set watermark to `time.time()` → zero historical seeding → forward-only
- Each cycle: query `moz_historyvisits WHERE visit_date > watermark_in_microseconds`
- Per URL: application-level upsert — `history_items.url` has no `UNIQUE` constraint, so SQLite's native `ON CONFLICT` upsert cannot be used here:
  1. `SELECT id FROM history_items WHERE url = ?`
  2. If found: `UPDATE history_items SET visit_count = visit_count + 1, should_recompute_derived_visit_counts = 1 WHERE id = ?`
  3. If not found: `INSERT INTO history_items (url, domain_expansion, visit_count, daily_visit_counts, weekly_visit_counts, autocomplete_triggers, should_recompute_derived_visit_counts, visit_count_score) VALUES (?, ?, 1, b'', NULL, NULL, 1, 0)`
- Per visit: `INSERT INTO history_visits (history_item, visit_time, title, ...) VALUES (...)`
- `daily_visit_counts`, `weekly_visit_counts`, `autocomplete_triggers`: insert as empty BLOB `b""`; `should_recompute_derived_visit_counts = 1` tells Safari to recompute
- Connection timeout: 5 seconds; if Safari holds the write lock, skip history sync for this cycle and log a warning
- Update watermark only after successful history write

#### Phase 3: LaunchAgent and Setup

**3a. LaunchAgent plist (`com.user.firefox-safari-sync.plist`)**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.firefox-safari-sync</string>
    <key>ProgramArguments</key>
    <array>
        <string>__PYTHON_PATH__</string>
        <string>__SYNC_SCRIPT_PATH__</string>
    </array>
    <key>StartInterval</key>
    <integer>300</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>__HOME__/Library/Logs/firefox-safari-sync/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>__HOME__/Library/Logs/firefox-safari-sync/stderr.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
</dict>
</plist>
```

- Uses **LaunchAgent** (not LaunchDaemon) — runs in user session, can access `~/Library`
- `StartInterval: 300` = 5 minutes
- `RunAtLoad: true` — runs immediately on bootstrap (login or first load)
- `ProgramArguments[0]` = **absolute path to Python interpreter** (not a command name — launchd has no shell PATH)
- `install.sh` substitutes `__PYTHON_PATH__`, `__SYNC_SCRIPT_PATH__`, and `__HOME__` at install time

**3b. `install.sh`**

Steps performed:
1. Check Python 3.10+ is available (`python3 --version`)
2. Create venv at `./venv` and install `lz4`
3. Determine real Python interpreter path (`python3 -c "import os,sys; print(os.path.realpath(sys.executable))"`)
4. Substitute placeholders in plist template → write to `~/Library/LaunchAgents/com.user.firefox-safari-sync.plist`
5. Create `~/Library/Logs/firefox-safari-sync/` and `~/.config/firefox-safari-sync/`
6. Bootstrap the agent: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.firefox-safari-sync.plist`
7. Print Full Disk Access instructions:
   ```
   REQUIRED: Grant Full Disk Access to the Python interpreter.
   Path: <resolved_python_path>

   Steps:
     1. Open System Settings → Privacy & Security → Full Disk Access
     2. Click the + button
     3. Press Cmd+Shift+G, paste: <resolved_python_path>
     4. Click Open and enable the toggle

   SECURITY NOTE: This grants Full Disk Access to the Python interpreter binary,
   which means any script run by that same Python binary will also have FDA.
   For a personal machine with a single-purpose venv, this is an acceptable tradeoff.
   If you prefer to scope the grant narrowly, see README for the Platypus .app wrapper
   approach, which limits FDA to this daemon's specific .app bundle.
   ```
8. Print verification instructions: `tail -f ~/Library/Logs/firefox-safari-sync/stdout.log`

**Security tradeoff — FDA scope:** macOS TCC grants Full Disk Access to the **interpreter binary** (e.g., `/opt/homebrew/bin/python3.12`), not to the `.py` script. This means any Python script run by that binary inherits FDA. For a personal tool on a personal machine with a dedicated venv, this is acceptable — the venv's Python is the real binary that runs the daemon and nothing else. For users who want tighter scoping, the alternative is to wrap the script in a macOS `.app` bundle (using [Platypus](https://sveinbjorn.org/platypus)) and grant FDA to the `.app` bundle instead. `install.sh` documents both paths but defaults to the simpler Python binary approach.

**3c. `uninstall.sh`**

1. `launchctl bootout gui/$(id -u)/com.user.firefox-safari-sync`
2. Remove `~/Library/LaunchAgents/com.user.firefox-safari-sync.plist`
3. Print note: "State file at ~/.config/firefox-safari-sync/state.json was not removed. Delete manually if desired."

#### Phase 4: Main Loop and Error Handling

**`sync.py` main structure:**

```python
def main():
    state = load_state()            # reads ~/.config/firefox-safari-sync/state.json
    profile = detect_firefox_profile()

    errors = []

    # R2: Tabs → "Firefox Tabs" bookmark folder
    try:
        tabs = read_open_tabs(profile)  # returns [] if Firefox not running
        write_tabs_to_safari(tabs)
    except Exception as e:
        errors.append(f"tabs: {e}")
        logging.warning("Tabs sync failed: %s", e)

    # R3: Bookmarks → "Firefox" bookmark folder tree
    try:
        bookmarks = read_firefox_bookmarks(profile)
        write_bookmarks_to_safari(bookmarks)
    except Exception as e:
        errors.append(f"bookmarks: {e}")
        logging.warning("Bookmarks sync failed: %s", e)

    # R4: History → Safari History.db (incremental)
    try:
        new_visits = read_new_history(profile, state["last_history_sync_unix"])
        write_history_to_safari(new_visits)
        state["last_history_sync_unix"] = time.time()
    except Exception as e:
        errors.append(f"history: {e}")
        logging.warning("History sync failed: %s", e)
        # Do NOT update watermark on failure

    save_state(state)

    if errors:
        logging.error("Sync cycle completed with %d error(s): %s", len(errors), "; ".join(errors))
    else:
        logging.info("Sync cycle completed successfully.")
```

**Error handling policy:**
- **Per-operation isolation**: tabs, bookmarks, and history sync are independent. One failure does not abort the others.
- **No retry within a cycle**: if an operation fails, it waits for the next 5-minute cycle.
- **History watermark is only advanced on success**: if history write fails, the next cycle will re-attempt the same window.
- **FDA check on startup**: attempt to open `~/Library/Safari/Bookmarks.plist`; if `PermissionError`, log a clear error message with FDA grant instructions and exit immediately (don't attempt Safari writes).
- **No user-facing alert**: all output goes to log files. The daemon is silent by design.

**State file schema (`state.json`):**
```json
{
  "last_history_sync_unix": 1742500000.0,
  "firefox_profile_path": "/Users/user/Library/Application Support/Firefox/Profiles/abc123.default-release",
  "schema_version": 1
}
```

---

## Alternative Approaches Considered

### 1. Firefox Remote Debugging Protocol for tabs
Requires enabling Firefox's remote debugger, adds a TCP dependency, and needs Firefox to be running with a specific flag. `recovery.jsonlz4` file parsing is simpler, requires no configuration, and is widely used by community forensic tools. Rejected.

### 2. `osascript` for Safari bookmark/history writes
Lets Safari manage its own files, avoiding file lock and iCloud in-memory conflicts. However, it requires Safari to be running, is slow (~seconds per bookmark), fragile across macOS versions, and can't easily batch 100+ bookmarks. Rejected in favor of direct file manipulation with deterministic UUIDs.

### 5. Delete-and-recreate bookmark folders (original approach, rejected)
Simple to implement but generates new random UUIDs every 5 minutes, causing continuous iCloud sync churn (288 full-tree delete+recreate cycles per day). Rejected in favor of deterministic UUIDs + diff-based merge.

### 3. Bidirectional sync
Significantly increases complexity (conflict resolution, two-way state tracking). Not needed per requirements (see origin: `docs/brainstorms/2026-03-21-firefox-safari-sync-requirements.md`).

### 4. Native Swift app
Better macOS integration and entitlement handling. Far more complex to build and maintain. Python is sufficient for a personal daemon. Rejected.

---

## System-Wide Impact

### Interaction Graph

Sync cycle (every 5 min via launchd) →
- Reads `~/Library/Application Support/Firefox/Profiles/<profile>/sessionstore-backups/recovery.jsonlz4` (read-only)
- Opens `~/Library/Application Support/Firefox/Profiles/<profile>/places.sqlite` read-only via `immutable=1` URI (bypasses exclusive WAL lock; no temp copy needed)
- Reads/writes `~/Library/Safari/Bookmarks.plist` → triggers `cloudd`/`bird` iCloud sync push within seconds
- Writes `~/Library/Safari/History.db` → Safari may read on next user interaction

iCloud pipeline (outside daemon scope):
- `cloudd`/`bird` detects `Bookmarks.plist` change → syncs to iCloud → propagates to Safari on iOS (latency: seconds to minutes, non-deterministic)

### Error & Failure Propagation

| Failure | Effect | Recovery |
|---|---|---|
| FDA not granted | `PermissionError` on first Safari write; daemon logs and exits | User grants FDA, daemon runs on next schedule |
| Firefox not running | `recovery.jsonlz4` absent; tabs sync skipped gracefully | Resolved on next cycle when Firefox is open |
| `places.sqlite` unreadable (`DatabaseError`) | `immutable=1` bypasses lock; `DatabaseError` means mid-checkpoint race (rare) | Logged as warning; next cycle retries |
| `Bookmarks.plist` write fails (e.g., disk full) | Atomic write leaves no partial file; logged as error | Next cycle retries |
| iCloud overwrites `Bookmarks.plist` after daemon write | Changes lost for current cycle | Next 5-min cycle rewrites |
| `History.db` locked (Safari holding write lock) | Connection timeout after 5s; history skipped; watermark not advanced | Next cycle retries same window |
| Python interpreter path changes (Homebrew upgrade) | LaunchAgent fails to start (binary not found) | Re-run `install.sh` |

### State Lifecycle Risks

- **Bookmarks.plist**: atomic write via `os.replace()` prevents torn reads. Safari never sees a partial file.
- **History.db**: SQLite WAL mode; each insert is wrapped in a `with con:` transaction block. Connection closed immediately after. No partial state possible.
- **Watermark**: advanced only after successful history write. On failure, next cycle re-processes the same window — possible duplicate prevention: `ON CONFLICT(url) DO UPDATE` for `history_items`; history_visits may get duplicates if the same visit is re-inserted (visit_time collision unlikely but possible within a 5-second window).
- **places.sqlite read**: `immutable=1` URI — no temp files to clean up. Connection closed immediately after reading.

### API Surface Parity

Only one interface: the `sync.py` script. No API surface. The daemon is a single executable with no importable library.

### Integration Test Scenarios

1. **Full cycle, Firefox running**: create a Firefox bookmark → run sync → verify bookmark appears in Safari `Bookmarks.plist` under the "Firefox" folder
2. **Firefox not running**: delete `recovery.jsonlz4` → run sync → verify bookmarks and history sync complete; verify no error about missing tabs file
3. **First-run watermark**: delete `state.json` → run sync → verify `last_history_sync_unix` is set to approximately `time.time()`; verify no history entries were written
4. **Incremental history**: sync once (set watermark) → visit 3 URLs in Firefox → sync again → verify exactly 3 new entries in `History.db`
5. **FDA missing**: revoke FDA → run sync → verify daemon logs `PermissionError` with FDA instructions and exits without partial writes

---

## Acceptance Criteria

### Functional

- [ ] R1: `install.sh` writes a valid `~/Library/LaunchAgents/com.user.firefox-safari-sync.plist` and bootstraps it via `launchctl bootstrap gui/$(id -u) ...`
- [ ] R1: Daemon runs automatically on login and every 5 minutes thereafter with no user interaction
- [ ] R2: Firefox open tabs appear in Safari's "Firefox Tabs" bookmarks folder within one sync cycle (~5 min) when Firefox is running
- [ ] R2: When Firefox is not running, the sync cycle completes without error (tabs sync silently skipped)
- [ ] R3: Firefox bookmarks appear in Safari's "Firefox" bookmarks folder, preserving folder hierarchy, within one sync cycle
- [ ] R3: Deleted Firefox bookmarks are removed from the Safari "Firefox" folder on the next sync cycle (diff-based merge)
- [ ] R3: Unchanged Firefox bookmarks produce the same UUID in the Safari plist on every cycle (deterministic UUID via `uuid5`), preventing iCloud sync churn
- [ ] R4: URLs visited in Firefox after the watermark is set appear in Safari history within one sync cycle
- [ ] R4: History watermark is not advanced when history write fails; the next cycle retries the same window
- [ ] R5: No Safari data is read into or written to any Firefox file

### Non-Functional

- [ ] Daemon exits within 30 seconds per cycle under normal conditions
- [ ] All Safari writes are atomic (temp file + `os.replace()` for plist; transactions for SQLite)
- [ ] Full Disk Access failure produces a clear log message with FDA grant instructions; no partial writes attempted

### Setup

- [ ] `install.sh` runs without error on a clean macOS Sequoia (15.x) system with Python 3.10+ and Homebrew
- [ ] `install.sh` prints exact FDA grant instructions with the resolved Python interpreter path
- [ ] `uninstall.sh` unloads the LaunchAgent and removes the plist; does not delete state file or logs without user action
- [ ] `requirements.txt` contains exactly one dependency: `lz4`

---

## Success Metrics

- Open a new tab in Firefox → within 5 minutes, that URL appears in the "Firefox Tabs" folder in Safari on macOS → iCloud propagates it to Safari iOS (propagation time is outside the daemon's SLA)
- A Firefox bookmark added to any folder → appears in the corresponding Safari folder within 5 minutes
- A URL visited in Firefox → appears in Safari history (searchable from address bar) within 5 minutes
- Daemon runs for 7 days without a crash or user intervention

**Note on iCloud SLA**: The daemon's responsibility ends when it writes to Safari's local files on macOS. iCloud propagation to iOS is non-deterministic and outside the daemon's scope. The success criterion "appears in Safari iOS within 5 minutes" assumes iCloud sync is active and operating normally.

---

## Dependencies & Prerequisites

- macOS 13 (Ventura) or later (Sequoia 15.x tested)
- Python 3.10+ installed (Homebrew or system)
- Firefox installed with at least one profile
- iCloud Drive enabled with Safari iCloud sync active (for iOS propagation)
- Safari iCloud sync enabled in System Settings → Apple ID → iCloud → Safari
- Full Disk Access granted to Python interpreter binary (guided by `install.sh`)

---

## Risk Analysis & Mitigation

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| iCloud overwrites `Bookmarks.plist` | Medium | Low | 5-min cycle self-heals; document as known limitation |
| iCloud sync churn from UUID regeneration | **Eliminated** | — | Deterministic UUIDs (`uuid5`) ensure unchanged nodes produce the same UUID every cycle |
| Safari in-memory state overwrites daemon write | Medium | Low | 5-min cycle self-heals; Safari uses FSEvents to re-read file, same as iCloud daemon does |
| Safari vacuums/rebuilds `History.db` | Low | Low | Well-formed rows survive routine vacuums; `should_recompute_derived_visit_counts=1` |
| Firefox changes `sessionstore` format | Low | Medium | Check for magic bytes; fail gracefully with log message |
| Python interpreter path changes after Homebrew upgrade | Medium | High | `uninstall.sh` + re-run `install.sh`; document in README |
| `places.sqlite` mid-checkpoint `DatabaseError` with `immutable=1` | Very Low | Low | Skip cycle, log warning; resolved on next cycle |
| User has multiple Firefox profiles | Low | Medium | Auto-detect default; log which profile was selected |
| macOS TCC resets FDA (after major OS upgrade) | Low | High | Daemon logs `PermissionError` clearly; user re-grants |

---

## Outstanding Questions (Deferred from Brainstorm)

These were in the requirements doc's `Deferred to Planning` section. All are resolved above:

- ✅ **Tab reading method**: `recovery.jsonlz4` file parsing (not Remote Debugging Protocol)
- ✅ **Safari file manipulation strategy**: direct file I/O (`plistlib` + `sqlite3`), atomic writes
- ✅ **Language**: Python 3.10+ with `lz4` as the only external dep
- ✅ **Deduplication**: deterministic UUIDs + diff-based in-place merge for bookmarks/tabs (avoids iCloud sync churn); `ON CONFLICT` upsert for history items; watermark for history visits
- ✅ **Multi-profile handling**: auto-detect via `profiles.ini` `[Install...]` → `Default=1` fallback

---

## Sources & References

### Origin

- **Origin document**: [docs/brainstorms/2026-03-21-firefox-safari-sync-requirements.md](../brainstorms/2026-03-21-firefox-safari-sync-requirements.md)
  Key decisions carried forward: one-way Firefox→Safari sync, tabs as bookmarks folder, 5-minute LaunchAgent interval, incremental history, mirror Firefox folder structure

### Internal References

- Requirements: `docs/brainstorms/2026-03-21-firefox-safari-sync-requirements.md`

### External References

- [mozLz4 decompression (Tblue gist)](https://gist.github.com/Tblue/62ff47bef7f894e92ed5)
- [Safari History.db schema (Velociraptor artifact)](https://docs.velociraptor.app/exchange/artifacts/pages/macos.applications.safari.history/)
- [SafariBookmarkEditor Python module (robperc)](https://github.com/robperc/SafariBookmarkEditor)
- [launchd.plist(5) man page](https://keith.github.io/xcode-man-pages/launchd.plist.5.html)
- [launchctl bootstrap/bootout reference (Alan Siu)](https://www.alansiu.net/2023/11/15/launchctl-new-subcommand-basics-for-macos/)
- [SQLite WAL mode documentation](https://sqlite.org/wal.html)
- [SQLite URI filenames (immutable flag)](https://sqlite.org/uri.html)
