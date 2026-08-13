"""
sf_whoson.py - Live "who's online" poller for Spitfire BBS

Reads the shared SFWHOSON.DAT node-status table and cross-references
SFUSERS.PTR for caller names, refreshing on a timer - no RustDesk,
no logging on, no per-node checking.

SETUP:
    See install.txt for full setup instructions.
    On first run, this script creates sf_whoson.ini next to itself.
    Edit that file with your paths and node count, then run again.

File formats (reverse-engineered from a live Spitfire install - see
install.txt for notes on verifying these against your own version):

SFWHOSON.DAT - shared across all nodes, one 6-byte record per node
    (only the first NODE_COUNT records are used; rest are unused slots)

    struct NodesDat:
        UserNo : SmallInt   (2 bytes, little-endian, -1 = node idle)
        Mode   : Char       (1 byte, activity code - not fully mapped yet)
        DBytes : Byte[3]    (reserved/unused)

SFUSERS.PTR - lightweight name index, one 31-byte record per user
    (0-based index, same order as SFUSERS.DAT)

    struct NamePtr:
        NameLen : Byte      (1 byte, Pascal string length prefix)
        Name    : Char[30]  (30 bytes, zero/garbage padded past NameLen)
"""

import configparser
import os
import struct
import sys
import time

CONFIG_FILENAME = "sf_whoson.ini"
NODE_REC_SIZE = 6   # fixed by Spitfire's NodesDat struct - do not change
NAME_REC_SIZE = 31  # fixed by Spitfire's String[30] format - do not change

DEFAULT_CONFIG = """\
[paths]
; Path to SFWHOSON.DAT, wherever your nodes' shared \\work directory lives.
; Can be a mapped drive letter (T:\\sf\\work\\SFWHOSON.DAT) or a UNC path
; (\\\\servername\\sf\\work\\SFWHOSON.DAT).
whoson_path =

; Path to SFUSERS.PTR, normally in the same folder as SFWHOSON.DAT.
userptr_path =

[settings]
; How many nodes your BBS runs. Must match your real node count -
; this is how many records get read from the front of SFWHOSON.DAT.
node_count =

; How often to refresh, in seconds.
refresh_seconds = 10
"""


def load_config(config_path):
    if not os.path.exists(config_path):
        with open(config_path, "w") as f:
            f.write(DEFAULT_CONFIG)
        print(f"No config found, so I created one: {config_path}")
        print("Edit it with your paths and node count, then run this again.")
        sys.exit(0)

    parser = configparser.ConfigParser()
    parser.read(config_path)

    try:
        whoson_path = parser.get("paths", "whoson_path")
        userptr_path = parser.get("paths", "userptr_path")
        node_count_raw = parser.get("settings", "node_count")
        refresh_seconds_raw = parser.get("settings", "refresh_seconds")
    except configparser.Error as e:
        print(f"Problem reading {config_path}: {e}")
        print("Delete the file and re-run this script to regenerate a fresh default.")
        sys.exit(1)

    blank_fields = [
        name for name, value in [
            ("whoson_path", whoson_path),
            ("userptr_path", userptr_path),
            ("node_count", node_count_raw),
        ]
        if not value.strip()
    ]
    if blank_fields:
        print(f"{config_path} still has blank value(s) for: {', '.join(blank_fields)}")
        print("Edit the file with your actual paths and node count, then run this again.")
        sys.exit(1)

    try:
        node_count = int(node_count_raw)
        refresh_seconds = int(refresh_seconds_raw) if refresh_seconds_raw.strip() else 10
    except ValueError as e:
        print(f"Problem reading {config_path}: {e}")
        print("node_count and refresh_seconds need to be plain numbers.")
        sys.exit(1)

    return whoson_path, userptr_path, node_count, refresh_seconds


def check_paths(whoson_path, userptr_path, node_count):
    """Fail early with a clear message rather than a confusing traceback."""
    problems = []

    if not os.path.exists(whoson_path):
        problems.append(f"Can't find SFWHOSON.DAT at: {whoson_path}")
    else:
        size = os.path.getsize(whoson_path)
        min_size = node_count * NODE_REC_SIZE
        if size < min_size:
            problems.append(
                f"SFWHOSON.DAT is only {size} bytes, but node_count={node_count} "
                f"needs at least {min_size} bytes. Check your node_count setting."
            )
        elif size % NODE_REC_SIZE != 0:
            problems.append(
                f"SFWHOSON.DAT is {size} bytes, which isn't a clean multiple of "
                f"{NODE_REC_SIZE}. This may be a different Spitfire version than "
                "this script was built against - see install.txt before trusting results."
            )

    if not os.path.exists(userptr_path):
        problems.append(f"Can't find SFUSERS.PTR at: {userptr_path}")
    else:
        size = os.path.getsize(userptr_path)
        if size % NAME_REC_SIZE != 0:
            problems.append(
                f"SFUSERS.PTR is {size} bytes, which isn't a clean multiple of "
                f"{NAME_REC_SIZE}. This may be a different Spitfire version than "
                "this script was built against - see install.txt before trusting results."
            )

    if problems:
        print("Setup problem(s) found:\n")
        for p in problems:
            print(f"  - {p}")
        print(f"\nCheck the paths and settings in {CONFIG_FILENAME} and try again.")
        sys.exit(1)


def read_node_status(whoson_path, node_count):
    """Return a list of (node_num, user_no, mode) for each node."""
    with open(whoson_path, "rb") as f:
        data = f.read(NODE_REC_SIZE * node_count)

    nodes = []
    for i in range(node_count):
        base = i * NODE_REC_SIZE
        user_no, mode_byte = struct.unpack_from("<hB", data, base)
        mode = chr(mode_byte) if 32 <= mode_byte < 127 else None
        nodes.append((i + 1, user_no, mode))
    return nodes


def get_username(userptr_path, user_no):
    """Look up a caller name in SFUSERS.PTR by 0-based user index."""
    if user_no < 0:
        return None

    with open(userptr_path, "rb") as f:
        f.seek(user_no * NAME_REC_SIZE)
        record = f.read(NAME_REC_SIZE)

    if not record:
        return f"<unknown user #{user_no}>"

    name_len = record[0]
    name_bytes = record[1:1 + name_len]
    try:
        return name_bytes.decode("cp437")
    except UnicodeDecodeError:
        return name_bytes.decode("latin-1", errors="replace")


def print_status(whoson_path, userptr_path, node_count):
    os.system("cls" if os.name == "nt" else "clear")
    print("[Spitfire BBS - Who's Online]")
    print(f"Last checked: {time.strftime('%m-%d-%y %H:%M:%S')}")
    print("-" * 40)

    try:
        nodes = read_node_status(whoson_path, node_count)
    except OSError as e:
        print(f"Could not read {whoson_path}: {e}")
        return

    for node_num, user_no, mode in nodes:
        if user_no < 0:
            print(f"Node {node_num} ... idle")
            continue

        try:
            name = get_username(userptr_path, user_no)
        except OSError as e:
            name = f"<lookup failed: {e}>"

        mode_str = f" ({mode})" if mode else ""
        print(f"Node {node_num} ... {name}{mode_str}")


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, CONFIG_FILENAME)

    whoson_path, userptr_path, node_count, refresh_seconds = load_config(config_path)
    check_paths(whoson_path, userptr_path, node_count)

    try:
        while True:
            print_status(whoson_path, userptr_path, node_count)
            time.sleep(refresh_seconds)
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
