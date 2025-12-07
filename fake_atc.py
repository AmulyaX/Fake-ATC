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
EMULATOR_VERSION = "1.0.0"
STARTUP_DELAY_SEC = 1    # seconds

# ANSI colours
RESET = "\033[0m"
COLOR_INFO = "\033[32m"   # green
COLOR_ERROR = "\033[31m"  # red
COLOR_RX = "\033[34m"     # blue
COLOR_TX = "\033[33m"     # yellow
COLOR_HIGHLIGHT = "\033[92m"  # bright green


# -----------------------
# Utility / Logging
# -----------------------

def use_color() -> bool:
    return sys.stderr.isatty()


def _log(level: str, msg: str, color: str = ""):
    if not VERBOSE:
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    level_str = f"{color}[{level}]{RESET}" if (use_color() and color) else f"[{level}]"
    print(f"{ts}  {level_str}  {msg}", file=sys.stderr, flush=True)


def log_info(msg: str): _log("INFO", msg, COLOR_INFO)
def log_error(msg: str): _log("ERROR", msg, COLOR_ERROR)
def log_rx(msg: str): _log("RX", msg, COLOR_RX)
def log_tx(msg: str): _log("TX", msg, COLOR_TX)


def banner(msg: str, color: str = ""):
    """Always-on printing for startup info."""
    if use_color() and color:
        print(f"{color}{msg}{RESET}", file=sys.stderr, flush=True)
    else:
        print(msg, file=sys.stderr, flush=True)


def important(msg: str):
    """Highlighted important info like port output."""
    if use_color():
        print(f"{COLOR_HIGHLIGHT}{msg}{RESET}", file=sys.stderr, flush=True)
    else:
        print(msg, file=sys.stderr, flush=True)


def get_kernel_info() -> str:
    try:
        return subprocess.check_output(["uname", "-a"]).decode().strip()
    except Exception:
        return f"{platform.system()} {platform.release()}"


# -----------------------
# AT Processing
# -----------------------

def load_commands(path="commands.json"):
    with open(path) as f:
        return json.load(f)


def parse_at(line: str):
    line = line.strip()
    if not line:
        return "", []
    if "=" in line:
        name, arg_str = line.split("=", 1)
        return name.upper(), [a.strip() for a in arg_str.split(",")]
    return line.upper(), []


def build_response(name: str, args, commands: dict):
    """
    Supports:
    1. Standard commands from commands.json
    2. Per-command delay (via { "delay": X, "resp": "..." })
    3. Special dynamic command: AT+DELAY=ms  (induced delay)
    """

    # ---------- SPECIAL COMMAND: AT+DELAY=xxx ----------
    if name == "AT+DELAY":
        if args and args[0].isdigit():
            delay_ms = int(args[0])
            log_info(f"Induced delay: {delay_ms} ms")
            return delay_ms, "\r\nOK\r\n"
        return 0, "\r\nERROR\r\n"

    # ---------- NORMAL COMMANDS ----------
    entry = commands.get(name)

    if entry is None:
        log_error(f"No match for command: {name}")
        return 0, "\r\nERROR\r\n"

    delay_ms = 0

    # JSON object case
    if isinstance(entry, dict):
        delay_ms = entry.get("delay", 0)
        resp = entry.get("resp", "")
    else:
        # Simple string case
        resp = entry

    # Replace {arg}
    if "{arg}" in resp and args:
        resp = resp.replace("{arg}", args[0])

    # Replace placeholders
    for key, val in commands.items():
        placeholder = "{" + key.lower().replace("+", "") + "}"
        if placeholder in resp:
            resp = resp.replace(placeholder, val)

    final = f"\r\n{resp}\r\n"
    return delay_ms, final

# -----------------------
# Cleanup
# -----------------------

def cleanup(signum=None, frame=None):
    global active_link, master_fd, slave_fd
    log_info("Shutting down emulator...")

    if active_link and os.path.islink(active_link):
        try:
            os.unlink(active_link)
            log_info(f"Removed symlink {active_link}")
        except Exception as e:
            log_error(f"Could not remove symlink: {e}")

    try:
        if master_fd: os.close(master_fd)
        if slave_fd: os.close(slave_fd)
    except Exception:
        pass

    sys.exit(0)


# -----------------------
# Main
# -----------------------

def main():
    global active_link, master_fd, slave_fd, VERBOSE

    parser = argparse.ArgumentParser(
        prog="AT Simulator",
        description="Lightweight modem AT command simulator using a pseudo-tty.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "-t", "--target",
        help="Create a symlink pointing to the PTY (e.g. /tmp/fake_modem)",
        default=None,
    )
    parser.add_argument(
        "-c", "--commands",
        help="Path to commands.json containing AT command responses",
        default="commands.json",
    )
    parser.add_argument(
        "-v", "--verbose",
        help="Enable detailed RX/TX logging to STDERR",
        action="store_true",
    )

    args = parser.parse_args()
    VERBOSE = args.verbose

    commands = load_commands(args.commands)

    # Create PTY
    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)

    # Symlink
    if args.target:
        if os.path.exists(args.target):
            try: os.remove(args.target)
            except Exception: pass
        os.symlink(slave_name, args.target)
        active_link = args.target

    # ---------------------------
    # Startup Information
    # ---------------------------
    banner("========================================")
    banner(f"{EMULATOR_NAME}  (v{EMULATOR_VERSION})")
    banner("----------------------------------------")
    banner(f"Platform: {platform.system()} {platform.release()}")
    banner(f"Python:   {platform.python_version()}")
    banner(f"Kernel:   {get_kernel_info()}")
    banner("----------------------------------------")

    important(f"PTY Port: {slave_name}")
    if args.target:
        important(f"Symlink:  {args.target} → {slave_name}")

    banner("----------------------------------------")
    banner("Verbose Logging: " + ("ENABLED" if VERBOSE else "DISABLED"))
    banner("Booting modem...")
    time.sleep(STARTUP_DELAY_SEC)
    banner("Modem Ready. Connect your AT client.")
    banner("========================================\n")

    # ---------------------------
    # Main loop
    # ---------------------------
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    buffer = b""

    while True:
        try:
            data = os.read(master_fd, 1024)
            if not data:
                log_info("Master side closed.")
                break

            buffer += data

            while b"\r" in buffer or b"\n" in buffer:
                idx_r = buffer.find(b"\r")
                idx_n = buffer.find(b"\n")
                sep_idx = min(i for i in [idx_r, idx_n] if i != -1)

                line_bytes = buffer[:sep_idx]
                buffer = buffer[sep_idx + 1:]

                try:
                    line = line_bytes.decode(errors="ignore").strip()
                except Exception:
                    line = ""

                if not line:
                    continue

                log_rx(line)

                name, args_list = parse_at(line)
                delay_ms, resp = build_response(name, args_list, commands)

                if delay_ms > 0:
                    log_info(f"Delaying {delay_ms} ms for {name}")
                    time.sleep(delay_ms / 1000.0)

                log_tx(resp.replace("\r", "\\r").replace("\n", "\\n"))
                os.write(master_fd, resp.encode())

        except KeyboardInterrupt:
            break
        except OSError as e:
            log_error(f"OSError: {e}")
            break

    cleanup()


if __name__ == "__main__":
    main()