"""Configuration constants for the Jetson record daemon.

Edit these values to change where recordings land, which DDS interface to use,
or which task names are accepted by the /start endpoint. Mirrors the task
list and DATA_ROOT used by camera_record_pipeline so rsync-merged sessions
end up in matching directory trees.
"""

# Bump whenever the HTTP response shape changes. Must match
# camera_record_pipeline/backend/config.py::PROTOCOL_VERSION and the
# frontend's EXPECTED_PROTOCOL_VERSION. The frontend rejects any daemon
# whose version differs.
PROTOCOL_VERSION = 4

# Task names accepted by POST /start. Must stay in sync with
# camera_record_pipeline/backend/config.py for session directory alignment.
TASKS = [
    "task1", "task2", "task3", "task4", "task5",
    "task6", "task7", "task8", "task9", "task10",
]

# Root directory where session folders are created on the Jetson.
# Lives under the unitree user's home so we don't need sudo/chown to
# create it. On the laptop the corresponding path is configurable per
# session via the frontend "Save Directory" input.
DATA_ROOT = "/home/unitree/GO2_DATA"

# Network interface used by the Unitree SDK for DDS traffic to the Go2 main
# board. Must be the Jetson's on-board ethernet (reaches 192.168.123.0/24).
INTERFACE = "eth0"

# FastAPI listen address and port. The port is distinct from
# camera_record_pipeline (8000) so both can share a host if ever needed.
HOST = "0.0.0.0"
PORT = 8010
