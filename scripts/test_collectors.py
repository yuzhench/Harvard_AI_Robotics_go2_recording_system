"""
test_collectors.py — Quick sanity test for IMUCollector and EgocentricCameraCollector.

Runs both collectors for 5 seconds (mock mode if SDK unavailable),
then saves to data/test_session/ and prints a summary.

Usage:
    # Mock mode (no robot needed):
    python scripts/test_collectors.py

    # Real robot:
    python scripts/test_collectors.py --interface eth0
"""

import argparse
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from utility.imu import IMUCollector, SDK_AVAILABLE
from utility.egocentric_camera import EgocentricCameraCollector

if SDK_AVAILABLE:
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interface", default="eth0", help="Network interface connected to Go2")
    parser.add_argument("--duration", type=float, default=5.0, help="Test duration in seconds")
    args = parser.parse_args()

    save_dir = Path("data/test_session/robot")

    print(f"=== Go2 Collector Test ({args.duration}s) ===")
    print(f"Interface : {args.interface}")
    print(f"Save dir  : {save_dir}")
    print()

    # Initialize DDS once for the whole process
    if SDK_AVAILABLE:
        ChannelFactoryInitialize(0, args.interface)

    imu = IMUCollector(network_interface=args.interface)
    cam = EgocentricCameraCollector(network_interface=args.interface)

    print("[+] Starting collectors...")
    imu.start()
    cam.start()

    print(f"[+] Recording for {args.duration}s... (Ctrl+C to stop early)")
    try:
        for i in range(int(args.duration)):
            time.sleep(1.0)
            print(f"    {i+1}s — IMU samples: {imu.sample_count}  |  Camera frames: {cam.frame_count}")
    except KeyboardInterrupt:
        print("\n[!] Interrupted by user")

    print("\n[+] Stopping collectors...")
    imu.stop()
    cam.stop()

    print("\n[+] Saving data...")
    imu_info = imu.save(save_dir)
    cam_info = cam.save(save_dir)

    print("\n=== Results ===")
    print(f"IMU    : {imu_info}")
    print(f"Camera : {cam_info}")
    print(f"\nData saved to: {save_dir.resolve()}")


if __name__ == "__main__":
    main()
