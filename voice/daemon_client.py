#!/usr/bin/env python3
"""Minimal, unambiguous client for voice-daemon.py's UNIX socket.

Replaces the previous `printf | socat` pipeline, which raced: piping into
socat's stdin and closing it could tear down the connection before the
daemon's reply was read back, silently dropping the response and forcing
voice.sh onto its slow cold-load fallback.
"""
import socket
import sys


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: daemon_client.py <wav_path> <sock_path>", file=sys.stderr)
        return 2
    wav_path, sock_path = sys.argv[1], sys.argv[2]
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(60)
    try:
        s.connect(sock_path)
        s.sendall((wav_path + "\n").encode())
        s.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
        sys.stdout.write(b"".join(chunks).decode(errors="replace"))
        return 0
    except OSError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
