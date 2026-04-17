"""
remote_control_server.py — TCP command server running on the Go2 Jetson.

Listens for single-character commands from the laptop over WiFi and
executes the corresponding SportClient action.

Commands:
    1   →   StandDown (lie/crawl)
    2   →   StandUp

Usage (on Jetson):
    conda activate go2
    python scripts/remote_control_server.py --interface eth0

    # Custom port
    python scripts/remote_control_server.py --interface eth0 --port 9876
"""

import sys
import socket
import argparse
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.go2.sport.sport_client import SportClient

PORT = 9876


def handle_client(conn: socket.socket, addr, client: SportClient):
    print(f"[+] Connected: {addr[0]}:{addr[1]}")
    try:
        buf = ""
        while True:
            data = conn.recv(64)
            if not data:
                break

            buf += data.decode("utf-8", errors="ignore")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                cmd = line.strip()

                if cmd == "1":
                    print("[CMD] StandDown (crawl)")
                    client.StandDown()
                    conn.sendall(b"OK: StandDown\n")

                elif cmd == "2":
                    print("[CMD] StandUp")
                    client.StandUp()
                    conn.sendall(b"OK: StandUp\n")

                elif cmd == "":
                    pass  # heartbeat / empty line

                else:
                    print(f"[WARN] Unknown command: {cmd!r}")
                    conn.sendall(f"ERR: unknown command '{cmd}'\n".encode())

    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        conn.close()
        print(f"[-] Disconnected: {addr[0]}:{addr[1]}")


def main():
    parser = argparse.ArgumentParser(description="Go2 remote control TCP server")
    parser.add_argument("--interface", default="eth0",
                        help="Network interface facing the Go2 (default: eth0)")
    parser.add_argument("--port", type=int, default=PORT,
                        help=f"TCP port to listen on (default: {PORT})")
    args = parser.parse_args()

    print(f"[*] Initializing Go2 SDK on interface '{args.interface}' ...")
    ChannelFactoryInitialize(0, args.interface)

    client = SportClient()
    client.SetTimeout(10.0)
    client.Init()
    print("[*] SportClient ready.")

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", args.port))
    server.listen(1)
    print(f"[*] Listening on 0.0.0.0:{args.port}  (waiting for laptop...)\n")

    try:
        while True:
            conn, addr = server.accept()
            t = threading.Thread(
                target=handle_client, args=(conn, addr, client), daemon=True
            )
            t.start()
    except KeyboardInterrupt:
        print("\n[*] Server shutting down.")
    finally:
        server.close()


if __name__ == "__main__":
    main()
