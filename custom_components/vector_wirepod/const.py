"""Constants for the Vector integration (domain vector_wirepod)."""

DOMAIN = "vector_wirepod"
PLATFORMS = ["camera", "switch", "sensor", "binary_sensor", "button", "image", "light", "number",
             "select", "text"]

CONF_WIREPOD_URL = "wirepod_url"
CONF_FALLBACK_URL = "fallback_url"
CONF_SERIAL = "serial"
CONF_NAME = "name"
CONF_STILL_REFRESH_S = "still_refresh_s"
CONF_BATTERY_POLL_S = "battery_poll_s"

DEFAULT_WIREPOD_URL = "http://escapepod.local:8080"
FAILOVER_CONNECT_TIMEOUT_S = 5.0
PRIMARY_RETRY_S = 120.0
SIGNAL_RECOVERED = "vector_wirepod_recovered_{}"
DEFAULT_STILL_REFRESH_S = 300
MAX_STILL_REFRESH_S = 86400

BATTERY_POLL_S = 60
MIN_BATTERY_POLL_S = 15
MAX_BATTERY_POLL_S = 3600
BATTERY_TIMEOUT_S = 20.0
# HA gives async_camera_image 10 s; answer well inside it.
STILL_WAIT_S = 7.0
STATUS_KEEPALIVE_S = 5.0
MAX_STREAM_STATUS_FRAMES = 720

BATTERY_LEVELS = {0: "unknown", 1: "low", 2: "nominal", 3: "full"}

# Sleep cycle (sleepcycle.py) and photos (photos.py).
CONF_QUIET_HOLD_S = "quiet_hold_s"
DEFAULT_QUIET_HOLD_S = 600        # quiet time after a wake before sleep is asked for again
MIN_QUIET_HOLD_S = 30
MAX_QUIET_HOLD_S = 86400
SLEEP_SETTLE_S = 180.0            # a sleep request not taken within this is given up
CYCLE_POLL_S = 20                 # robot is asked this often while a sleep cycle is running
QUIET_SETTLE_S = 3.0              # after quiet mode is lifted, before the robot is asked anything
PHOTO_WAIT_S = 12.0
WAKE_PHOTO_TRIES = 4              # a robot that has just woken gives no frames at first
WAKE_PHOTO_PAUSE_S = 3.0
PHOTO_SETTLE_FRAMES = 5           # frames let pass so the exposure settles
PHOTO_KEEP = 50
SIGNAL_PHOTO = "vector_wirepod_photo_{}"
EVENT_PHOTO = "vector_wirepod_photo"

# Cryosleep mode (cryosleep.py).
CRYOSLEEP_SETTLE_S = 60.0         # seconds given to a sleep request before re-enforcing or locking
CRYOSLEEP_GUARD_S = 300.0         # periodic check that cryo_mute and backlight=0 stay enforced
CRYOSLEEP_MAX_SLEEP_RETRIES = 2   # engage RESERVE_CONTROL movement lock after failed sleep tries
MAX_CRYOSLEEP_LISTENERS = 32
DEFAULT_AWAKE_BACKLIGHT = 10
