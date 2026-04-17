"""
remote_control_client.py — Laptop-side controller for Go2 over WiFi.

Connects to the TCP server running on the Go2 Jetson and sends
motion commands via keyboard.

Controls:
    1   →   StandDown (lie/crawl)
    2   →   StandUp
    Q   →   Quit

Usage:
    python laptop_side_code/remote_control_client.py
    python laptop_side_code/remote_control_client.py --host 192.168.123.18 --port 9876
"""

import sys
import socket
import argparse
import threading
import tty
import termios

JETSON_HOST = "192.168.123.18"
PORT = 9876


def recv_loop(sock: socket.socket, stop: threading.Event):
    """Background thread: prints server responses."""
    try:
        buf = ""
        while not stop.is_set():
            data = sock.recv(256)
            if not data:
                print("\r[!] Server closed connection.")
                stop.set()
                break
            buf += data.decode("utf-8", errors="ignore")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                if line.strip():
                    print(f"\r[Go2] {line.strip():<40}")
    except OSError:
        pass


def send_command(sock: socket.socket, cmd: str):
    sock.sendall(f"{cmd}\n".encode())


def main():
    parser = argparse.ArgumentParser(description="Go2 remote control client")
    parser.add_argument("--host", default=JETSON_HOST,
                        help=f"Jetson IP address (default: {JETSON_HOST})")
    parser.add_argument("--port", type=int, default=PORT,
                        help=f"Server port (default: {PORT})")
    args = parser.parse_args()

    print(f"[*] Connecting to {args.host}:{args.port} ...")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect((args.host, args.port))
        sock.settimeout(None)
    except (ConnectionRefusedError, TimeoutError, OSError) as e:
        print(f"[!] Connection failed: {e}")
        sys.exit(1)

    print("[*] Connected!\n")
    print("=" * 40)
    print("  Go2 Wireless Remote Control")
    print("=" * 40)
    print("  1   →   StandDown (crawl/lie)")
    print("  2   →   StandUp")
    print("  Q   →   Quit")
    print("=" * 40 + "\n")

    stop = threading.Event()
    t_recv = threading.Thread(target=recv_loop, args=(sock, stop), daemon=True)
    t_recv.start()

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    tty.setraw(fd)

    try:
        while not stop.is_set():
            ch = sys.stdin.read(1)

            if ch in ("q", "Q", "\x03"):   # Q or Ctrl-C
                print("\r[*] Quitting...                        ")
                stop.set()
                break
            elif ch == "1":
                print("\r[>>] StandDown sent                    ", end="", flush=True)
                send_command(sock, "1")
            elif ch == "2":
                print("\r[>>] StandUp sent                      ", end="", flush=True)
                send_command(sock, "2")
            # ignore anything else

    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        sock.close()
        print()


if __name__ == "__main__":
    main()
