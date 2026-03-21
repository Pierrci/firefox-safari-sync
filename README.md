# firefox-safari-sync

One-way sync from Firefox on macOS to Safari — bookmarks, open tabs, and history.

Runs as a macOS LaunchAgent every 5 minutes. iCloud propagates Safari changes to iOS automatically.

## Requirements

- macOS 13 (Ventura) or later
- Python 3.10+
- Firefox with at least one profile
- iCloud Drive enabled with Safari sync active

## Installation

Clone the repository:

```sh
git clone https://github.com/Pierrci/firefox-safari-sync.git
cd firefox-safari-sync
```

Run the installer:

```sh
bash install.sh
```

The installer will:

1. Detect Python 3.10+ (prefers Homebrew)
2. Create a venv and install `lz4`
3. Write the LaunchAgent plist to `~/Library/LaunchAgents/`
4. Bootstrap the agent via `launchctl`
5. Print the exact Python interpreter path for Full Disk Access

## Full Disk Access (required)

The daemon reads Safari's files, which macOS protects behind Full Disk Access (FDA). The installer prints the exact path to grant. Follow these steps:

1. Open **System Settings → Privacy & Security → Full Disk Access**
2. Click **+**
3. Press **Cmd+Shift+G** and paste the path printed by `install.sh`
4. Click **Open** and enable the toggle

> **Security note:** FDA is granted to the Python interpreter binary, not the script. Any script run by that binary inherits FDA. This is an acceptable tradeoff for a personal, single-purpose venv.

## Verify

Tail the daemon log to confirm it is running:

```sh
tail -f ~/Library/Logs/firefox-safari-sync/stdout.log
```

A successful cycle ends with:

```
2026-03-21 10:00:00 [INFO] Sync cycle completed successfully.
```

## What gets synced

| Data | Destination in Safari | Cadence |
|---|---|---|
| Firefox open tabs | "Firefox Tabs" bookmarks folder | Every cycle |
| Firefox bookmarks | "Firefox" bookmarks folder (full hierarchy) | Every cycle |
| Browsing history | Safari History.db (incremental) | Every cycle |

Tabs are only synced while Firefox is running. If Firefox is closed, the cycle completes silently without them.

History sync is forward-only from the first install. No historical visits are seeded.

## How it works

Each 5-minute cycle, `sync.py`:

1. Reads open tabs from Firefox's `recovery.jsonlz4` session file
2. Reads bookmarks and history from `places.sqlite` (read-only, no lock contention)
3. Writes tabs and bookmarks into `~/Library/Safari/Bookmarks.plist` atomically
4. Writes new history visits into `~/Library/Safari/History.db`
5. Advances a watermark in `~/.config/firefox-safari-sync/state.json`

Bookmark nodes use deterministic UUIDs derived from Firefox GUIDs. Unchanged nodes produce the same UUID every cycle, preventing iCloud sync churn.

iCloud propagation to iOS is non-deterministic. The daemon's responsibility ends when it writes to Safari's local files.

## File layout

```
firefox-safari-sync/
├── sync.py                               # daemon script
├── install.sh                            # setup
├── uninstall.sh                          # teardown
├── requirements.txt                      # lz4
└── com.user.firefox-safari-sync.plist   # LaunchAgent template
```

Runtime paths (outside the repo):

```
~/.config/firefox-safari-sync/state.json
~/Library/Logs/firefox-safari-sync/stdout.log
~/Library/Logs/firefox-safari-sync/stderr.log
~/Library/LaunchAgents/com.user.firefox-safari-sync.plist
```

## Known limitations

- **iCloud propagation latency** is non-deterministic. Changes appear on iOS within seconds to minutes when iCloud is healthy.
- **Safari in-memory state** can overwrite a daemon write on quit. The next 5-minute cycle self-heals.
- **iCloud race condition**: a remote change arriving from iOS can overwrite `Bookmarks.plist`. The next cycle re-applies Firefox data.
- **History duplicates**: if a history write fails mid-cycle, the next cycle retries the same window. Duplicate visits for the same URL within a 5-second window are possible but rare.

## After a Homebrew Python upgrade

The LaunchAgent stores an absolute path to the Python interpreter. If Homebrew replaces it, the daemon stops. Re-run the installer to update the path:

```sh
bash install.sh
```

## Uninstall

```sh
bash uninstall.sh
```

This stops the LaunchAgent and removes the plist. The following are **not** removed — delete manually if desired:

- `~/.config/firefox-safari-sync/` (state)
- `~/Library/Logs/firefox-safari-sync/` (logs)
- `./venv/` (Python environment)

Also remove the Full Disk Access entry from **System Settings → Privacy & Security → Full Disk Access**.

## Contributing

Bug fixes and improvements are welcome. Open an issue before starting large changes.

## License

[MIT](LICENSE)
