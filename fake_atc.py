#!/usr/bin/env python3

import os
import pty
import json
import argparse
import platform
import signal
import sys
import time
import subprocess
from datetime import datetime

active_link = None
master_fd = None
slave_fd = None
VERBOSE = False

EMULATOR_NAME = "AT Simulator"
EMULATOR_VERSION = "1.1.0"
STARTUP_DELAY_SEC = 1

RESET = "\033[0m"
COLOR_INFO = "\033[32m"
COLOR_ERROR = "\033[31m"
COLOR_RX = "\033[34m"
COLOR_TX = "\033[33m"
COLOR_HIGHLIGHT = "\033[92m"


# ------------------------------------------------------------
# Logging System
# ------------------------------------------------------------

def use_color() -> bool:
    """Return True if stderr supports ANSI colors (TTY output)."""
    return sys.stderr.isatty()


def ts():
    """Return a formatted timestamp (HH:MM:SS.mmm) for log entries."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def clean_for_log(s: str):
    """
    Convert a multi-line modem response into a clean single-line form.
    This is necessary because the real PTY output uses CRLF blocks,
    but logs should remain readable and compact.
    """
    lines = [line.strip() for line in s.strip().splitlines() if line.strip()]
    return " | ".join(lines)


ARROWS = {"RX": "←", "TX": "→", "INF": "•", "ERR": "!"}


def _log(direction: str, msg: str, color: str):
    """
    Core formatted logger for RX/TX/INFO/ERR.
    Produces aligned, timestamped, bracketed log lines such as:

    [12:30:10.123]  [→ TX]  OK

    This gives a clean trace similar to real modem diagnostic tools.
    """
    if not VERBOSE:
        return

    timestamp = f"\033[36m{ts()}\033[0m" if use_color() else ts()
    arrow = ARROWS.get(direction, "?")

    if use_color():
        block = f"{color}{arrow} {direction}{RESET}"
    else:
        block = f"{arrow} {direction}"

    print(f"[{timestamp}]  [{block:<6}]  {msg}", file=sys.stderr, flush=True)


def log_rx(msg): _log("RX", msg, COLOR_RX)


def log_tx(msg): _log("TX", msg, COLOR_TX)


def log_info(msg): _log("INF", msg, COLOR_INFO)


def log_error(msg): _log("ERR", msg, COLOR_ERROR)


def banner(msg: str, color: str = ""):
    """Print startup/runtime status messages (not part of verbose debug)."""
    if use_color() and color:
        print(f"{color}{msg}{RESET}", file=sys.stderr)
    else:
        print(msg, file=sys.stderr)


def important(msg: str):
    """Highlight key runtime info such as PTY paths."""
    if use_color():
        print(f"{COLOR_HIGHLIGHT}{msg}{RESET}", file=sys.stderr)
    else:
        print(msg, file=sys.stderr)


def get_kernel_info() -> str:
    """Retrieve kernel information for banner display."""
    try:
        return subprocess.check_output(["uname", "-a"]).decode().strip()
    except Exception:
        return f"{platform.system()} {platform.release()}"


# ------------------------------------------------------------
# Reboot Handling
# ------------------------------------------------------------

def reboot_modem(args):
    """
    Perform a full modem reboot operation:
      - Close existing PTY descriptors
      - Allocate a fresh PTY pair
      - Recreate the symlink so clients see the new PTY
      - Display reboot startup info

    This simulates hardware-level modem reboot behaviour triggered by AT+CFUN=1,1.
    """
    global master_fd, slave_fd, active_link

    log_info("Reboot initiated...")

    try:
        if master_fd: os.close(master_fd)
        if slave_fd: os.close(slave_fd)
    except Exception:
        pass

    master_fd_new, slave_fd_new = pty.openpty()
    slave_name_new = os.ttyname(slave_fd_new)
    master_fd, slave_fd = master_fd_new, slave_fd_new

    if args.target:
        try:
            if os.path.exists(args.target):
                os.remove(args.target)
            os.symlink(slave_name_new, args.target)
            active_link = args.target
        except Exception:
            pass

    banner("\n========================================")
    banner(f"{EMULATOR_NAME}  (Rebooted)")
    banner(f"Version: {EMULATOR_VERSION}")
    banner("----------------------------------------")
    important(f"New PTY Port: {slave_name_new}")
    if args.target:
        important(f"Symlink: {args.target} → {slave_name_new}")
    banner("========================================\n")

    log_info("Reboot complete. Modem ready.")


# ------------------------------------------------------------
# AT Command Processing
# ------------------------------------------------------------

def load_commands(path="commands.json"):
    """Load AT response definitions from the JSON file."""
    with open(path) as f:
        return json.load(f)


def parse_at(line: str):
    """
    Parse an incoming AT command into:
      - command name (uppercase)
      - list of arguments if present
    """
    line = line.strip()
    if not line:
        return "", []
    if "=" in line:
        name, arg_str = line.split("=", 1)
        return name.upper(), [x.strip() for x in arg_str.split(",")]
    return line.upper(), []


def build_response(name: str, args, commands: dict):
    """
    Resolve an AT command into:
      - delay in milliseconds
      - modem-style response string (CRLF wrapped)

    Handles:
      - AT+DELAY=n → artificial latency injection
      - AT+CFUN=1,1 → full reboot trigger
      - Per-command delays defined in commands.json
      - Placeholder substitution inside responses
    """

    if name == "AT+DELAY":
        if args and args[0].isdigit():
            d = int(args[0])
            log_info(f"Induced delay: {d} ms")
            return d, "\r\nOK\r\n"
        return 0, "\r\nERROR\r\n"

    if name == "AT+CFUN" and args == ["1", "1"]:
        log_info("AT+CFUN=1,1 → Reboot requested")
        return -1, "\r\nOK\r\n"

    if name == "AT+CFUN":
        return 0, "\r\nOK\r\n"

    entry = commands.get(name)
    if entry is None:
        log_error(f"No match for command: {name}")
        return 0, "\r\nERROR\r\n"

    delay_ms = entry.get("delay", 0) if isinstance(entry, dict) else 0
    resp = entry["resp"] if isinstance(entry, dict) else entry

    if "{arg}" in resp and args:
        resp = resp.replace("{arg}", args[0])

    for key, val in commands.items():
        placeholder = "{" + key.lower().replace("+", "") + "}"
        if placeholder in resp:
            resp = resp.replace(placeholder, val)

    return delay_ms, f"\r\n{resp}\r\n"


# ------------------------------------------------------------
# Cleanup
# ------------------------------------------------------------

def cleanup(signum=None, frame=None):
    """
    Ensure the PTY and symlink are removed correctly before exit.
    This prevents stale symlinks and locked file descriptors.
    """
    global active_link, master_fd, slave_fd

    log_info("Shutting down emulator...")

    if active_link and os.path.islink(active_link):
        try:
            os.unlink(active_link)
        except Exception:
            pass

    try:
        if master_fd: os.close(master_fd)
        if slave_fd: os.close(slave_fd)
    except Exception:
        pass

    sys.exit(0)


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

def main():
    """
    Initialize emulator, create the PTY device, display startup info,
    then enter the main read/process/write loop for handling AT commands.
    """
    global active_link, master_fd, slave_fd, VERBOSE

    parser = argparse.ArgumentParser(
        prog="AT Simulator",
        description="Modem-like AT command emulator using PTY.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument("-t", "--target", help="Symlink to expose PTY under a stable path")
    parser.add_argument("-c", "--commands", help="JSON file with AT responses", default="commands.json")
    parser.add_argument("-v", "--verbose", help="Enable debug logs", action="store_true")

    args = parser.parse_args()
    VERBOSE = args.verbose

    commands = load_commands(args.commands)

    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)

    if args.target:
        try:
            if os.path.exists(args.target):
                os.remove(args.target)
            os.symlink(slave_name, args.target)
            active_link = args.target
        except Exception:
            pass

    banner("========================================")
    banner(f"{EMULATOR_NAME}  (v{EMULATOR_VERSION})")
    banner("----------------------------------------")
    banner(f"Platform: {platform.system()} {platform.release()}")
    banner(f"Python:   {platform.python_version()}")
    banner(f"Kernel:   {get_kernel_info()}")
    banner("----------------------------------------")
    important(f"PTY Port: {slave_name}")
    if args.target:
        important(f"Symlink: {args.target} → {slave_name}")
    banner("----------------------------------------")
    banner(f"Verbose Logging: {'ENABLED' if VERBOSE else 'DISABLED'}")
    banner("Booting modem...")
    time.sleep(STARTUP_DELAY_SEC)
    banner("Modem Ready. Connect your AT client.")
    banner("========================================\n")

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    buffer = b""

    while True:
        try:
            data = os.read(master_fd, 1024)
            if not data:
                log_info("Master closed.")
                break

            buffer += data

            # Process complete AT lines
            while b"\r" in buffer or b"\n" in buffer:
                idxs = [x for x in (buffer.find(b"\r"), buffer.find(b"\n")) if x != -1]
                sep = min(idxs)
                line_bytes = buffer[:sep]
                buffer = buffer[sep + 1:]

                try:
                    line = line_bytes.decode(errors="ignore").strip()
                except Exception:
                    line = ""
                if not line:
                    continue

                log_rx(line)
                name, args_list = parse_at(line)
                delay_ms, resp = build_response(name, args_list, commands)

                if delay_ms == -1:
                    log_tx(clean_for_log(resp))
                    os.write(master_fd, resp.encode())
                    time.sleep(1)
                    reboot_modem(args)
                    continue

                if delay_ms:
                    log_info(f"Delay {delay_ms} ms")
                    time.sleep(delay_ms / 1000.0)

                log_tx(clean_for_log(resp))
                os.write(master_fd, resp.encode())

        except KeyboardInterrupt:
            break
        except OSError as e:
            log_error(f"OSError: {e}")
            break

    cleanup()


if __name__ == "__main__":
    main()