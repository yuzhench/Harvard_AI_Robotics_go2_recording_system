"""
keyboard_control.py — Control Go2 with keyboard arrow keys.

Controls:
    ↑ / ↓      Forward / Backward
    ← / →      Turn left / right
    A / D      Strafe left / right
    Space      Stop
    1          Stand up
    2          Stand down
    Q          Quit

Usage:
    python scripts/keyboard_control.py --interface eth0
"""

import sys
import time
import argparse
import threading
import tty
import termios
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.go2.sport.sport_client import SportClient

# Velocity settings
VX   = 0.4   # forward/backward (m/s)
VY   = 0.3   # strafe (m/s)
VYAW = 0.6   # turn (rad/s)
CMD_HZ = 20  # command send rate

# Shared state
vx, vy, vyaw = 0.0, 0.0, 0.0
stop_flag = threading.Event()
action = None  # 'standup' | 'standdown' | None


def read_keys():
    """Runs in a thread, updates global velocity from keypresses."""
    global vx, vy, vyaw, action

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd)

    try:
        while not stop_flag.is_set():
            ch = sys.stdin.read(1)

            if ch in ('q', 'Q'):
                stop_flag.set()
                break

            elif ch == '\x1b':
                ch2 = sys.stdin.read(1)
                ch3 = sys.stdin.read(1)
                arrow = ch2 + ch3

                if arrow == '[A':        # Up
                    vx, vy, vyaw = VX, 0.0, 0.0
                    print(f"\r[↑] Forward              ", end='', flush=True)
                elif arrow == '[B':      # Down
                    vx, vy, vyaw = -VX, 0.0, 0.0
                    print(f"\r[↓] Backward             ", end='', flush=True)
                elif arrow == '[D':      # Left
                    vx, vy, vyaw = 0.0, 0.0, VYAW
                    print(f"\r[←] Turn left            ", end='', flush=True)
                elif arrow == '[C':      # Right
                    vx, vy, vyaw = 0.0, 0.0, -VYAW
                    print(f"\r[→] Turn right           ", end='', flush=True)

            elif ch in ('a', 'A'):
                vx, vy, vyaw = 0.0, VY, 0.0
                print(f"\r[A] Strafe left           ", end='', flush=True)
            elif ch in ('d', 'D'):
                vx, vy, vyaw = 0.0, -VY, 0.0
                print(f"\r[D] Strafe right          ", end='', flush=True)
            elif ch == ' ':
                vx, vy, vyaw = 0.0, 0.0, 0.0
                print(f"\r[SPACE] Stop              ", end='', flush=True)
            elif ch == '1':
                action = 'standup'
                print(f"\r[1] Stand up              ", end='', flush=True)
            elif ch == '2':
                action = 'standdown'
                print(f"\r[2] Stand down            ", end='', flush=True)

    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def send_commands(client: SportClient):
    """Runs in a thread, sends Move() at fixed rate."""
    global action
    interval = 1.0 / CMD_HZ

    while not stop_flag.is_set():
        t0 = time.monotonic()

        if action == 'standup':
            client.StandUp()
            action = None
        elif action == 'standdown':
            client.StandDown()
            action = None
        else:
            client.Move(vx, vy, vyaw)

        elapsed = time.monotonic() - t0
        time.sleep(max(0.0, interval - elapsed))

    client.StopMove()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interface", default="eth0")
    args = parser.parse_args()

    ChannelFactoryInitialize(0, args.interface)
    client = SportClient()
    client.SetTimeout(10.0)
    client.Init()

    print("\n" + "="*40)
    print("  Go2 Keyboard Control")
    print("="*40)
    print("  ↑ / ↓       Forward / Backward")
    print("  ← / →       Turn left / right")
    print("  A / D       Strafe left / right")
    print("  SPACE       Stop")
    print("  1           Stand up")
    print("  2           Stand down")
    print("  Q           Quit")
    print("="*40 + "\n")

    t_cmd  = threading.Thread(target=send_commands, args=(client,), daemon=True)
    t_keys = threading.Thread(target=read_keys, daemon=True)

    t_cmd.start()
    t_keys.start()

    stop_flag.wait()
    print("\nQuitting...")


if __name__ == "__main__":
    main()
