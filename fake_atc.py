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
COLOR_INFO = "\033[32m"
COLOR_ERROR = "\033[31m"
COLOR_RX = "\033[34m"
COLOR_TX = "\033[33m"
COLOR_HIGHLIGHT = "\033[92m"


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
    if use_color() and color:
        print(f"{color}{msg}{RESET}", file=sys.stderr, flush=True)
    else:
        print(msg, file=sys.stderr, flush=True)


def important(msg: str):
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
# Reboot Logic
# -----------------------

def reboot_modem(args):
    global master_fd, slave_fd, active_link

    log_info("Reboot initiated...")

    # Close old PTY
    try:
        if master_fd:
            os.close(master_fd)
        if slave_fd:
            os.close(slave_fd)
    except Exception:
        pass

    # Create new PTY
    master_fd_new, slave_fd_new = pty.openpty()
    slave_name_new = os.ttyname(slave_fd_new)

    master_fd = master_fd_new
    slave_fd = slave_fd_new

    # Recreate symlink
    if args.target:
        try:
            if os.path.exists(args.target):
                os.remove(args.target)
            os.symlink(slave_name_new, args.target)
            active_link = args.target
        except Exception:
            pass

    # Reboot banner
    banner("\n========================================")
    banner(f"{EMULATOR_NAME}  (Rebooted)")
    banner(f"Version: {EMULATOR_VERSION}")
    banner("----------------------------------------")
    important(f"New PTY Port: {slave_name_new}")
    if args.target:
        important(f"Symlink: {args.target} → {slave_name_new}")
    banner("========================================\n")

    log_info("Reboot complete. Modem ready.")


# -----------------------
# Command Processor
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
        args = [a.strip() for a in arg_str.split(",")]
    else:
        name = line
        args = []

    return name.upper(), args


def build_response(name: str, args, commands: dict):
    """
    Supports:
    - Standard commands
    - Per-command delay (via {"delay": X, "resp": "..."})
    - Special dynamic command: AT+DELAY=ms
    - Full reboot: AT+CFUN=1,1
    """

    # ---------- SPECIAL COMMAND: AT+DELAY=xxx ----------
    if name == "AT+DELAY":
        if args and args[0].isdigit():
            delay_ms = int(args[0])
            log_info(f"Induced delay: {delay_ms} ms")
            return delay_ms, "\r\nOK\r\n"
        return 0, "\r\nERROR\r\n"

    # ---------- SPECIAL COMMAND: AT+CFUN=1,1 (Reboot) ----------
    if name == "AT+CFUN" and len(args) == 2 and args[0] == "1" and args[1] == "1":
        log_info("AT+CFUN=1,1 → Reboot requested")
        return -1, "\r\nOK\r\n"   # -1 triggers reboot

    # Normal CFUN commands return OK
    if name == "AT+CFUN":
        return 0, "\r\nOK\r\n"

    # ---------- NORMAL COMMANDS ----------
    entry = commands.get(name)

    if entry is None:
        log_error(f"No match for command: {name}")
        return 0, "\r\nERROR\r\n"

    delay_ms = 0

    if isinstance(entry, dict):
        delay_ms = entry.get("delay", 0)
        resp = entry.get("resp", "")
    else:
        resp = entry

    if "{arg}" in resp and args:
        resp = resp.replace("{arg}", args[0])

    # Placeholder replacements
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
        except Exception:
            pass

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

    parser.add_argument("-t", "--target", help="Create symlink to PTY", default=None)
    parser.add_argument("-c", "--commands", help="Path to commands.json", default="commands.json")
    parser.add_argument("-v", "--verbose", help="Enable RX/TX logging", action="store_true")

    args = parser.parse_args()
    VERBOSE = args.verbose

    commands = load_commands(args.commands)

    # Create PTY
    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)

    # Create symlink
    if args.target:
        try:
            if os.path.exists(args.target):
                os.remove(args.target)
            os.symlink(slave_name, args.target)
            active_link = args.target
        except Exception:
            pass

    # Startup banner
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
    banner("Verbose Logging: " + ("ENABLED" if VERBOSE else "DISABLED"))
    banner("Booting modem...")
    time.sleep(STARTUP_DELAY_SEC)
    banner("Modem Ready. Connect your AT client.")
    banner("========================================\n")

    # Main loop
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

                # Handle reboot
                if delay_ms == -1:
                    log_tx(resp.replace("\r", "\\r"))
                    os.write(master_fd, resp.encode())
                    time.sleep(1)
                    reboot_modem(args)
                    continue

                # Normal delay
                if delay_ms > 0:
                    log_info(f"Delaying {delay_ms} ms for {name}")
                    time.sleep(delay_ms / 1000.0)

                log_tx(resp.replace("\r", "\\r"))
                os.write(master_fd, resp.encode())

        except KeyboardInterrupt:
            break
        except OSError as e:
            log_error(f"OSError: {e}")
            break

    cleanup()


if __name__ == "__main__":
    main()