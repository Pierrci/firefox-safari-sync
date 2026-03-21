---
date: 2026-03-21
topic: firefox-safari-sync
---

# Firefox → Safari Continuous Sync

## Problem Frame

The user uses Firefox on macOS as their primary desktop browser but prefers Safari on iOS for its native integration and iCloud sync. There is no built-in bridge between the two. The goal is a lightweight background service that continuously pushes Firefox data (open tabs, bookmarks, history) into Safari on macOS, where iCloud then propagates it to Safari on iOS — giving the user a seamless cross-device experience without abandoning Firefox on desktop.

## Requirements

- R1. A macOS launchd daemon runs continuously in the background, triggering a sync cycle every 5 minutes with no user interaction required.
- R2. Firefox's currently open tabs are synced to a dedicated Safari bookmarks folder named "Firefox Tabs". iCloud then propagates this folder to Safari on iOS.
- R3. Firefox bookmarks are synced to Safari bookmarks, preserving folder structure.
- R4. Firefox browsing history is synced to Safari's browsing history.
- R5. Sync is one-directional: Firefox → Safari only. Safari data is never read or modified in ways that would affect Firefox.

## Success Criteria

- Open a new tab in Firefox; within 5 minutes it appears in the "Firefox Tabs" folder in Safari iOS.
- A Firefox bookmark added to any folder appears in the corresponding Safari folder within 5 minutes.
- A URL visited in Firefox appears in Safari's history within 5 minutes, making it searchable from Safari iOS's address bar.
- The daemon runs silently after initial setup with no ongoing manual steps required.

## Scope Boundaries

- No bidirectional sync — Safari → Firefox is out of scope.
- No Safari iOS data (tabs opened on iPhone) syncs to Firefox.
- Firefox tabs appear as a bookmarks folder, not as a native "iCloud Tabs" device entry (which would require private Apple APIs).
- No GUI application or menubar icon — daemon only.
- No cross-machine sync (only local Mac Firefox → local Mac Safari → iCloud).

## Key Decisions

- **Tabs as bookmarks folder**: The "Firefox Tabs" bookmarks folder approach is reliable and iCloud-native. True iCloud Tabs injection requires private APIs and is not feasible.
- **5-minute sync interval**: Balances freshness with system resource usage.
- **launchd daemon**: Preferred over a GUI app or manual CLI for always-on, low-overhead operation.
- **One-way sync**: Keeps conflict resolution out of scope and avoids accidental data loss in Firefox.
- **Mirror Firefox folder structure**: Firefox bookmark subfolders are recreated under a top-level "Firefox" folder in Safari, preserving the user's existing organization.
- **Incremental history sync**: Only new history entries since the last sync cycle are pushed — no bulk seeding on first run.

## Dependencies / Assumptions

- User is on macOS with iCloud Drive and Safari iCloud sync enabled.
- Firefox is the user's primary desktop browser with an accessible local profile.
- Safari on iOS is signed into the same iCloud account as the Mac.

## Outstanding Questions

### Resolve Before Planning

_(none — all product decisions resolved)_

### Deferred to Planning

- [Affects R2][Technical] Best method for reading Firefox open tabs: `sessionstore.jsonlz4` / `recovery.jsonlz4` file parsing vs. Firefox Remote Debugging Protocol.
- [Affects R2, R3, R4][Technical] Whether to manipulate Safari's `Bookmarks.plist` and `History.db` directly or use `osascript` — and how to handle Safari holding file locks while running.
- [Affects R1][Technical] Whether the daemon should be written in Python, Swift, or shell — driven by available parsing libraries for Firefox's lz4-compressed session files and Safari's binary plist format.
- [Affects R3, R4][Technical] Deduplication strategy to avoid creating duplicate bookmarks or history entries on each sync cycle.
- [Affects R1][Technical] How to handle multi-profile Firefox installs — which profile to target.

## Next Steps

→ `/ce:plan` for structured implementation planning.
