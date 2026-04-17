"""Configuration constants for the Jetson record daemon.

Edit these values to change where recordings land, which DDS interface to use,
or which task names are accepted by the /start endpoint. Mirrors the task
list and DATA_ROOT used by camera_record_pipeline so rsync-merged sessions
end up in matching directory trees.
"""

# Task names accepted by POST /start. Must stay in sync with
# camera_record_pipeline/backend/config.py for session directory alignment.
TASKS = [
    "task1", "task2", "task3", "task4", "task5",
    "task6", "task7", "task8", "task9", "task10",
]

# Root directory where session folders are created on the Jetson.
# Mirrored on the laptop by camera_record_pipeline so that
# `rsync unitree@<jetson>:/home/GO2_DATA/ /home/GO2_DATA/` merges cleanly.
DATA_ROOT = "/home/GO2_DATA"

# Network interface used by the Unitree SDK for DDS traffic to the Go2 main
# board. Must be the Jetson's on-board ethernet (reaches 192.168.123.0/24).
INTERFACE = "eth0"

# FastAPI listen address and port. The port is distinct from
# camera_record_pipeline (8000) so both can share a host if ever needed.
HOST = "0.0.0.0"
PORT = 8010
