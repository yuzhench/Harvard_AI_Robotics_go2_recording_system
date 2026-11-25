"""
test_collectors.py — Quick sanity test for the IMU, ego camera and LiDAR collectors.

Runs the collectors for 5 seconds (synthetic data only with --mock), streaming
raw files to a new directory under data/test_session/, and prints a summary. Convert the result
with scripts/finalize_session.py.

Usage:
    # Mock mode (no robot needed):
    python scripts/test_collectors.py --mock

    # Real robot:
    python scripts/test_collectors.py --interface eth0
"""

import argparse
import time
import tempfile
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from utility.imu import IMUCollector, SDK_AVAILABLE
from utility.egocentric_camera import EgocentricCameraCollector
from utility.lidar import LidarCollector

if SDK_AVAILABLE:
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interface", default="eth0", help="Network interface connected to Go2")
    parser.add_argument("--duration", type=float, default=5.0, help="Test duration in seconds")
    parser.add_argument("--mock", action="store_true", help="Generate synthetic data; do not connect to hardware")
    args = parser.parse_args()
    if args.duration <= 0:
        parser.error("--duration must be positive")

    parent = Path("data/test_session")
    parent.mkdir(parents=True, exist_ok=True)
    save_dir = Path(tempfile.mkdtemp(prefix="mock-" if args.mock else "real-", dir=parent)) / "first_person"

    print(f"=== Go2 Collector Test ({args.duration}s) ===")
    print(f"Interface : {args.interface}")
    print(f"Save dir  : {save_dir}")
    print()

    # Initialize DDS once for the whole process
    if not args.mock and SDK_AVAILABLE:
        ChannelFactoryInitialize(0, args.interface)

    imu = IMUCollector(network_interface=args.interface, mock=args.mock)
    cam = EgocentricCameraCollector(network_interface=args.interface, mock=args.mock)
    lidar = LidarCollector(network_interface=args.interface, mock=args.mock)

    collectors = (imu, cam, lidar)
    info = []
    try:
        for collector in collectors:
            collector.start(save_dir)
        print(f"[+] Recording for {args.duration}s... (Ctrl+C to stop early)")
        time.sleep(args.duration)
    except KeyboardInterrupt:
        print("\n[!] Interrupted by user")
    finally:
        for collector in collectors:
            try:
                info.append(collector.stop())
            except Exception as exc:
                info.append({"error": str(exc)})
    imu_info, cam_info, lidar_info = info

    print("\n=== Results ===")
    print(f"IMU    : {imu_info}")
    print(f"Camera : {cam_info}")
    print(f"LiDAR  : {lidar_info}")
    print(f"\nRaw data in: {save_dir.resolve()}")
    print(f"Finalize with: python scripts/finalize_session.py {save_dir}")
    if any("error" in item for item in info):
        raise RuntimeError("One or more collectors failed to stop cleanly")


if __name__ == "__main__":
    main()
