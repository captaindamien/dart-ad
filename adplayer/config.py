import os

MACHINE_TOKEN      = os.environ.get("MACHINE_TOKEN", "")
SERVER_URL         = os.environ.get("SERVER_URL", "http://localhost:3000").rstrip("/")
ADS_DIR            = os.environ.get("ADS_DIR", os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "public", "ads")))
SYNC_INTERVAL      = int(os.environ.get("SYNC_INTERVAL", "300"))
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "30"))

_PUBLIC = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "public"))
MARKER1_PATH = os.path.join(_PUBLIC, "marker.png")
MARKER2_PATH = os.path.join(_PUBLIC, "marker2.png")

THRESHOLD       = 0.75
DEBOUNCE_FRAMES = 3
DETECT_SCALE    = 0.25
DETECT_EVERY_N  = 3
