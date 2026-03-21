#!/usr/bin/env python3
"""Firefox → Safari one-way sync daemon.

Reads open tabs, bookmarks, and history from a local Firefox profile and
writes them into Safari's on-disk files (Bookmarks.plist, History.db).
iCloud then propagates the Safari-side changes to iOS.

Designed to run as a macOS LaunchAgent on a 5-minute interval.
"""

import configparser
import json
import logging
import os
import plistlib
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path

import lz4.block

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MOZLZ4_MAGIC = b"mozLz40\0"
UUID_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # NAMESPACE_URL
COCOA_OFFSET = 978307200  # seconds between Unix epoch (1970) and Cocoa epoch (2001)

STATE_DIR = Path.home() / ".config" / "firefox-safari-sync"
STATE_FILE = STATE_DIR / "state.json"

SAFARI_BOOKMARKS = Path.home() / "Library" / "Safari" / "Bookmarks.plist"
SAFARI_HISTORY = Path.home() / "Library" / "Safari" / "History.db"

FIREFOX_BASE = Path.home() / "Library" / "Application Support" / "Firefox"
PROFILES_INI = FIREFOX_BASE / "profiles.ini"

TABS_FOLDER_TITLE = "Firefox Tabs"
BOOKMARKS_FOLDER_TITLE = "Firefox"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state() -> dict:
    """Load persisted state or return defaults."""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "last_history_sync_unix": None,
        "firefox_profile_path": None,
        "schema_version": 1,
    }


def save_state(state: dict) -> None:
    """Persist state to disk."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ---------------------------------------------------------------------------
# Firefox profile detection
# ---------------------------------------------------------------------------

def detect_firefox_profile(state: dict) -> Path:
    """Find the default Firefox profile directory.

    1. Use cached path from state if it still exists.
    2. Check [Install...] sections for Default= key (Firefox 67+).
    3. Fall back to [Profile#] sections with Default=1.
    """
    # Use cached path if valid
    cached = state.get("firefox_profile_path")
    if cached:
        p = Path(cached)
        if p.is_dir():
            return p

    if not PROFILES_INI.exists():
        raise FileNotFoundError(f"Firefox profiles.ini not found at {PROFILES_INI}")

    cfg = configparser.ConfigParser()
    cfg.read(PROFILES_INI)

    base = PROFILES_INI.parent

    # Strategy 1: [Install...] sections (Firefox 67+)
    for section in cfg.sections():
        if section.startswith("Install"):
            default = cfg.get(section, "Default", fallback=None)
            if default:
                profile_path = base / default
                if profile_path.is_dir():
                    state["firefox_profile_path"] = str(profile_path)
                    return profile_path

    # Strategy 2: [Profile#] with Default=1
    for section in cfg.sections():
        if section.startswith("Profile"):
            if cfg.get(section, "Default", fallback="0") == "1":
                path_val = cfg.get(section, "Path", fallback=None)
                is_relative = cfg.get(section, "IsRelative", fallback="1") == "1"
                if path_val:
                    profile_path = base / path_val if is_relative else Path(path_val)
                    if profile_path.is_dir():
                        state["firefox_profile_path"] = str(profile_path)
                        return profile_path

    # Strategy 3: first valid profile
    for section in cfg.sections():
        if section.startswith("Profile"):
            path_val = cfg.get(section, "Path", fallback=None)
            is_relative = cfg.get(section, "IsRelative", fallback="1") == "1"
            if path_val:
                profile_path = base / path_val if is_relative else Path(path_val)
                if profile_path.is_dir():
                    log.warning("No default profile found; using first valid: %s", profile_path)
                    state["firefox_profile_path"] = str(profile_path)
                    return profile_path

    raise FileNotFoundError("No valid Firefox profile directory found")

# ---------------------------------------------------------------------------
# Firefox data readers
# ---------------------------------------------------------------------------

def read_mozlz4(path: Path) -> dict:
    """Decompress a mozLz4 file and return parsed JSON."""
    with open(path, "rb") as fh:
        magic = fh.read(8)
        if magic != MOZLZ4_MAGIC:
            raise ValueError(f"Bad mozLz4 magic: {magic!r}")
        raw = fh.read()
    return json.loads(lz4.block.decompress(raw))


def read_open_tabs(profile: Path) -> list[dict]:
    """Read Firefox open tabs from recovery.jsonlz4.

    Returns a list of dicts with 'title' and 'url' keys.
    Returns [] if Firefox is not running (file absent).
    """
    recovery = profile / "sessionstore-backups" / "recovery.jsonlz4"
    if not recovery.exists():
        log.info("recovery.jsonlz4 not found (Firefox not running?); skipping tabs.")
        return []

    # Retry if file appears mid-write (< 12 bytes)
    for attempt in range(3):
        if recovery.stat().st_size >= 12:
            break
        time.sleep(0.2)
    else:
        log.warning("recovery.jsonlz4 too small after retries; skipping tabs.")
        return []

    session = read_mozlz4(recovery)
    tabs = []
    for window in session.get("windows", []):
        for tab in window.get("tabs", []):
            entries = tab.get("entries", [])
            index = tab.get("index", 1)
            if entries:
                # index is 1-based
                entry = entries[min(index - 1, len(entries) - 1)]
                url = entry.get("url", "")
                title = entry.get("title", url)
                if url and not url.startswith("about:"):
                    tabs.append({"title": title, "url": url})
    return tabs


def read_firefox_bookmarks(profile: Path) -> list[dict]:
    """Read Firefox bookmarks from places.sqlite.

    Returns a tree structure: list of root folders, each containing
    nested children with 'title', 'url' (for bookmarks), 'children' (for folders).
    """
    places = profile / "places.sqlite"
    if not places.exists():
        raise FileNotFoundError(f"places.sqlite not found at {places}")

    uri = f"file:{places}?immutable=1&mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.DatabaseError as e:
        raise RuntimeError(f"Cannot open places.sqlite: {e}") from e

    try:
        rows = conn.execute(
            """
            SELECT b.id, b.type, b.parent, b.title, b.guid,
                   COALESCE(p.url, '') AS url
            FROM moz_bookmarks b
            LEFT JOIN moz_places p ON b.fk = p.id
            ORDER BY b.parent, b.position
            """
        ).fetchall()
    finally:
        conn.close()

    # Build lookup: id -> node
    nodes = {}
    for row in rows:
        nodes[row["id"]] = {
            "id": row["id"],
            "type": row["type"],
            "parent": row["parent"],
            "title": row["title"] or "",
            "guid": row["guid"],
            "url": row["url"],
            "children": [],
        }

    # Link children to parents
    for node in nodes.values():
        parent = nodes.get(node["parent"])
        if parent and parent["id"] != node["id"]:
            parent["children"].append(node)

    # Firefox root structure: id=1 is "root", children are:
    #   id=2: "Bookmarks Menu"
    #   id=3: "Bookmarks Toolbar"
    #   id=5: "Other Bookmarks" (unfiled)
    #   id=6: "Tags" (skip)
    #   id=4: "Mobile Bookmarks" (skip)
    root = nodes.get(1)
    if not root:
        return []

    # Return the meaningful root folders
    result = []
    for child in root["children"]:
        # Skip tags and mobile
        if child["title"] in ("Tags",):
            continue
        result.append(_clean_tree(child))

    return result


def _clean_tree(node: dict) -> dict:
    """Recursively clean a bookmark tree node for output."""
    if node["type"] == 2:  # folder
        return {
            "type": "folder",
            "title": node["title"],
            "guid": node["guid"],
            "children": [_clean_tree(c) for c in node["children"]],
        }
    else:  # bookmark (type=1) or separator
        if node["type"] == 1 and node["url"] and not node["url"].startswith("place:"):
            return {
                "type": "bookmark",
                "title": node["title"],
                "guid": node["guid"],
                "url": node["url"],
            }
        return None  # separators and place: URIs are skipped


def read_new_history(profile: Path, last_sync_unix: float | None) -> list[dict]:
    """Read Firefox history entries newer than the watermark.

    Returns a list of dicts with 'url', 'title', 'visit_time_unix' keys.
    """
    places = profile / "places.sqlite"
    if not places.exists():
        raise FileNotFoundError(f"places.sqlite not found at {places}")

    uri = f"file:{places}?immutable=1&mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.DatabaseError as e:
        raise RuntimeError(f"Cannot open places.sqlite: {e}") from e

    try:
        if last_sync_unix is None:
            # First run: no historical seeding
            return []

        watermark_us = int(last_sync_unix * 1_000_000)
        rows = conn.execute(
            """
            SELECT p.url, p.title, v.visit_date
            FROM moz_historyvisits v
            JOIN moz_places p ON v.place_id = p.id
            WHERE v.visit_date > ?
            ORDER BY v.visit_date ASC
            """,
            (watermark_us,),
        ).fetchall()
    finally:
        conn.close()

    visits = []
    for row in rows:
        url = row["url"]
        if url and not url.startswith("about:"):
            visits.append({
                "url": url,
                "title": row["title"] or url,
                "visit_time_unix": row["visit_date"] / 1_000_000,
            })
    return visits

# ---------------------------------------------------------------------------
# Safari writers
# ---------------------------------------------------------------------------

def stable_uuid(key: str) -> str:
    """Deterministic UUID from a Firefox GUID or URL. Same input -> same UUID always."""
    return str(uuid.uuid5(UUID_NAMESPACE, key)).upper()


def _make_bookmark_node(title: str, url: str, uid: str) -> dict:
    """Create a Safari bookmark leaf node."""
    return {
        "URIDictionary": {"title": title},
        "URLString": url,
        "WebBookmarkType": "WebBookmarkTypeLeaf",
        "WebBookmarkUUID": uid,
    }


def _make_folder_node(title: str, uid: str, children: list[dict]) -> dict:
    """Create a Safari bookmark folder node."""
    return {
        "Title": title,
        "WebBookmarkType": "WebBookmarkTypeList",
        "WebBookmarkUUID": uid,
        "Children": children,
    }


def _build_safari_tree(bm_node: dict) -> dict | None:
    """Recursively convert a Firefox bookmark tree node to a Safari plist node."""
    if bm_node is None:
        return None
    if bm_node["type"] == "folder":
        children = []
        for child in bm_node.get("children", []):
            safari_child = _build_safari_tree(child)
            if safari_child:
                children.append(safari_child)
        uid = stable_uuid(bm_node["guid"])
        return _make_folder_node(bm_node["title"], uid, children)
    elif bm_node["type"] == "bookmark":
        uid = stable_uuid(bm_node["guid"])
        return _make_bookmark_node(bm_node["title"], bm_node["url"], uid)
    return None


def _find_or_create_folder(children: list[dict], title: str, uid: str) -> dict:
    """Find an existing folder by UUID in children list, or create and append it."""
    for child in children:
        if child.get("WebBookmarkUUID") == uid:
            return child
    folder = _make_folder_node(title, uid, [])
    children.append(folder)
    return folder


def _find_bookmarks_bar(plist_data: dict) -> list[dict]:
    """Find the BookmarksBar children list in the Safari plist."""
    for child in plist_data.get("Children", []):
        if child.get("Title") == "BookmarksBar":
            if "Children" not in child:
                child["Children"] = []
            return child["Children"]
    # Create BookmarksBar if missing
    bar = _make_folder_node("BookmarksBar", str(uuid.uuid4()).upper(), [])
    bar["WebBookmarkIdentifier"] = ""
    plist_data.setdefault("Children", []).append(bar)
    return bar["Children"]


def _write_plist_atomic(plist_data: dict) -> None:
    """Write Safari Bookmarks.plist atomically."""
    with tempfile.NamedTemporaryFile(
        dir=SAFARI_BOOKMARKS.parent, delete=False, suffix=".plist"
    ) as tmp:
        plistlib.dump(plist_data, tmp, fmt=plistlib.FMT_BINARY)
        tmp_path = tmp.name
    os.replace(tmp_path, SAFARI_BOOKMARKS)


def write_tabs_to_safari(tabs: list[dict]) -> None:
    """Write Firefox open tabs as a Safari bookmarks folder (diff-based)."""
    with open(SAFARI_BOOKMARKS, "rb") as f:
        plist_data = plistlib.load(f)

    bar_children = _find_bookmarks_bar(plist_data)
    folder_uid = stable_uuid("firefox-tabs-folder")
    folder = _find_or_create_folder(bar_children, TABS_FOLDER_TITLE, folder_uid)

    # Build desired state
    desired = {}
    for tab in tabs:
        uid = stable_uuid(tab["url"])
        desired[uid] = _make_bookmark_node(tab["title"], tab["url"], uid)

    # Current state
    existing = {c["WebBookmarkUUID"]: c for c in folder.get("Children", [])
                if "WebBookmarkUUID" in c}

    # Diff
    changed = False

    # Add or update
    new_children = []
    for uid, node in desired.items():
        old = existing.get(uid)
        if old:
            # Check if content changed
            if (old.get("URIDictionary", {}).get("title") != node["URIDictionary"]["title"]
                    or old.get("URLString") != node["URLString"]):
                new_children.append(node)
                changed = True
            else:
                new_children.append(old)
        else:
            new_children.append(node)
            changed = True

    # Check removals
    if set(existing.keys()) != set(desired.keys()):
        changed = True

    if not changed:
        log.info("Tabs: no changes detected.")
        return

    folder["Children"] = new_children
    _write_plist_atomic(plist_data)
    log.info("Tabs: synced %d tabs to Safari.", len(tabs))


def write_bookmarks_to_safari(bookmarks: list[dict]) -> None:
    """Write Firefox bookmarks as a Safari bookmarks folder tree (diff-based)."""
    with open(SAFARI_BOOKMARKS, "rb") as f:
        plist_data = plistlib.load(f)

    bar_children = _find_bookmarks_bar(plist_data)
    folder_uid = stable_uuid("firefox-bookmarks-folder")
    folder = _find_or_create_folder(bar_children, BOOKMARKS_FOLDER_TITLE, folder_uid)

    # Build new children from Firefox bookmark tree
    new_children = []
    for root_folder in bookmarks:
        safari_node = _build_safari_tree(root_folder)
        if safari_node:
            new_children.append(safari_node)

    # Simple deep comparison: serialize to check for changes
    old_json = json.dumps(folder.get("Children", []), sort_keys=True, default=str)
    new_json = json.dumps(new_children, sort_keys=True, default=str)

    if old_json == new_json:
        log.info("Bookmarks: no changes detected.")
        return

    folder["Children"] = new_children
    _write_plist_atomic(plist_data)
    log.info("Bookmarks: synced to Safari.")


def _extract_domain(url: str) -> str | None:
    """Extract domain expansion from URL (e.g., 'example.com' from 'https://www.example.com/path')."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = parsed.hostname or ""
        # Remove www. prefix for domain_expansion
        if host.startswith("www."):
            host = host[4:]
        return host or None
    except Exception:
        return None


def write_history_to_safari(visits: list[dict]) -> None:
    """Write new history entries to Safari History.db."""
    if not visits:
        log.info("History: no new visits to sync.")
        return

    try:
        conn = sqlite3.connect(str(SAFARI_HISTORY), timeout=5)
    except sqlite3.OperationalError as e:
        raise RuntimeError(f"Cannot open History.db (Safari may hold lock): {e}") from e

    try:
        with conn:
            for visit in visits:
                url = visit["url"]
                title = visit["title"]
                visit_time_cocoa = visit["visit_time_unix"] - COCOA_OFFSET
                domain = _extract_domain(url)

                # Application-level upsert for history_items
                row = conn.execute(
                    "SELECT id FROM history_items WHERE url = ?", (url,)
                ).fetchone()

                if row:
                    item_id = row[0]
                    conn.execute(
                        """UPDATE history_items
                           SET visit_count = visit_count + 1,
                               should_recompute_derived_visit_counts = 1
                           WHERE id = ?""",
                        (item_id,),
                    )
                else:
                    cursor = conn.execute(
                        """INSERT INTO history_items
                           (url, domain_expansion, visit_count,
                            daily_visit_counts, weekly_visit_counts,
                            autocomplete_triggers,
                            should_recompute_derived_visit_counts,
                            visit_count_score)
                           VALUES (?, ?, 1, x'', NULL, NULL, 1, 0)""",
                        (url, domain),
                    )
                    item_id = cursor.lastrowid

                # Insert the visit
                conn.execute(
                    """INSERT INTO history_visits
                       (history_item, visit_time, title)
                       VALUES (?, ?, ?)""",
                    (item_id, visit_time_cocoa, title),
                )
    finally:
        conn.close()

    log.info("History: synced %d visits to Safari.", len(visits))

# ---------------------------------------------------------------------------
# FDA check
# ---------------------------------------------------------------------------

def check_full_disk_access() -> bool:
    """Verify we can read Safari files (requires Full Disk Access)."""
    try:
        with open(SAFARI_BOOKMARKS, "rb") as f:
            f.read(1)
        return True
    except PermissionError:
        log.error(
            "Full Disk Access not granted. Cannot read Safari files.\n"
            "Grant FDA to the Python interpreter:\n"
            "  1. Open System Settings → Privacy & Security → Full Disk Access\n"
            "  2. Click + and add: %s\n"
            "  3. Enable the toggle and restart the daemon.",
            sys.executable,
        )
        return False

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("Sync cycle starting.")
    state = load_state()

    if not check_full_disk_access():
        sys.exit(1)

    profile = detect_firefox_profile(state)
    log.info("Using Firefox profile: %s", profile)

    errors = []

    # Tabs → "Firefox Tabs" bookmark folder
    try:
        tabs = read_open_tabs(profile)
        if tabs:
            write_tabs_to_safari(tabs)
        else:
            log.info("No tabs to sync.")
    except Exception as e:
        errors.append(f"tabs: {e}")
        log.warning("Tabs sync failed: %s", e)

    # Bookmarks → "Firefox" bookmark folder tree
    try:
        bookmarks = read_firefox_bookmarks(profile)
        write_bookmarks_to_safari(bookmarks)
    except Exception as e:
        errors.append(f"bookmarks: {e}")
        log.warning("Bookmarks sync failed: %s", e)

    # History → Safari History.db (incremental)
    try:
        new_visits = read_new_history(profile, state.get("last_history_sync_unix"))
        if state.get("last_history_sync_unix") is None:
            # First run: set watermark, no historical seeding
            log.info("History: first run — setting watermark, no historical sync.")
            state["last_history_sync_unix"] = time.time()
        else:
            write_history_to_safari(new_visits)
            state["last_history_sync_unix"] = time.time()
    except Exception as e:
        errors.append(f"history: {e}")
        log.warning("History sync failed: %s", e)
        # Do NOT update watermark on failure

    save_state(state)

    if errors:
        log.error("Sync cycle completed with %d error(s): %s", len(errors), "; ".join(errors))
    else:
        log.info("Sync cycle completed successfully.")


if __name__ == "__main__":
    main()
