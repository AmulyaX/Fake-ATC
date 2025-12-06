#!/usr/bin/env python3

import os
import pty
import json
import argparse
import platform
import signal
import sys


active_link = None  # store the symlink we create


def load_commands(path="commands.json"):
    with open(path) as f:
        return json.load(f)


def parse_at(line):
    line = line.strip()
    if "=" in line:
        name, arg_str = line.split("=", 1)
        args = [a.strip() for a in arg_str.split(",")]
    else:
        name = line
        args = []
    return name.upper(), args


def build_response(name, args, commands):
    if name in commands:
        resp = commands[name]

        if "{arg}" in resp and args:
            resp = resp.replace("{arg}", args[0])

        for key, val in commands.items():
            placeholder = "{" + key.lower().replace("+", "") + "}"
            if placeholder in resp:
                resp = resp.replace(placeholder, val)

        return f"\r\n{resp}\r\nOK\r\n"

    return "\r\nERROR\r\n"


def cleanup_symlink():
    global active_link
    if active_link and os.path.islink(active_link):
        try:
            os.unlink(active_link)
            print(f"\nCleaned link: {active_link}")
        except PermissionError:
            print(f"\nCould not delete symlink {active_link} (permission denied)")
    else:
        print("\nNo active link to clean.")


def signal_handler(sig, frame):
    print("\nReceived interrupt. Shutting down Fake-ATC...")
    cleanup_symlink()
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def create_symlink(target_link, pty_path):
    global active_link
    os_name = platform.system()

    print("Detected OS:", os_name)
    print("Fake-ATC PTY:", pty_path)
    print("Link target:", target_link)
    print()

    # Delete previous symlink if exists
    if os.path.islink(target_link):
        try:
            os.unlink(target_link)
        except PermissionError:
            print("Permission denied removing previous link.")
            return

    # macOS
    if os_name == "Darwin":
        os.symlink(pty_path, target_link)
        print("Linked successfully. Use", target_link, "as your serial port.")
        active_link = target_link
        return

    # Linux
    if os_name == "Linux":
        try:
            os.symlink(pty_path, target_link)
        except PermissionError:
            print("Permission denied creating symlink. Try sudo.")
            return

        print("Linked successfully. Use", target_link, "as your serial port.")
        active_link = target_link
        return

    print("Unsupported OS:", os_name)


def start_fake_atc(commands, target_link):
    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)

    print("Fake-ATC PTY created at:", slave_name)

    create_symlink(target_link, slave_name)
    print("Press Ctrl+C to stop.")

    while True:
        data = os.read(master_fd, 1024)
        if not data:
            continue

        line = data.decode(errors="ignore").strip()
        if not line:
            continue

        name, args = parse_at(line)
        reply = build_response(name, args, commands)
        os.write(master_fd, reply.encode())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-t", "--target", type=str, help="Path to link Fake-ATC PTY to")
    parser.add_argument("-c", "--config", type=str, default="commands.json", help="Commands JSON file")
    args = parser.parse_args()

    os_name = platform.system()

    if args.target:
        target_link = args.target
    else:
        if os_name == "Linux":
            target_link = "/dev/ttyUSB0"
        else:
            target_link = "./ttyUSB0"

    commands = load_commands(args.config)
    start_fake_atc(commands, target_link)