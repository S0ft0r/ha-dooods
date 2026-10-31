"""Константы независимой интеграции DOOODS."""

DOMAIN = "dooods"
SERVICE_SCAN = "scan"

CONF_URL = "url"
CONF_AUTH_KEY = "auth_key"
CONF_SOURCE = "source"
CONF_MOTION_SENSORS = "motion_sensors"
CONF_CAMERA_PROFILES = "camera_profiles"
CONF_CAMERA_SCAN_INTERVALS = "camera_scan_intervals"
CONF_CAMERA_MOTION_RECHECK_INTERVALS = "camera_motion_recheck_intervals"
CONF_CAMERA_DETECTION_ENABLED = "camera_detection_enabled"
CONF_CAMERA_STREAM_ENABLED = "camera_stream_enabled"
CONF_PROFILE_ID = "profile_id"
CONF_DETECTOR = "detector"
CONF_TIMEOUT = "timeout"
# Legacy global option. It is ignored and is never migrated to camera values.
CONF_SCAN_INTERVAL = "scan_interval"
CONF_CONFIDENCE = "confidence"
CONF_LABELS = "labels"
CONF_AREA = "area"
CONF_FILE_OUT = "file_out"
CONF_ARCHIVE_MAX_COUNT = "archive_max_count"
CONF_ARCHIVE_MAX_AGE_DAYS = "archive_max_age_days"

# Явная настройка камеры без сканирования по событиям движения.
MOTION_SENSOR_NONE = "none"
MOTION_SOURCE_RTSP = "rtsp_stream"

DEFAULT_ARCHIVE_MAX_COUNT = 10
DEFAULT_ARCHIVE_MAX_AGE_DAYS = 0

CONF_TOP = "top"
CONF_BOTTOM = "bottom"
CONF_LEFT = "left"
CONF_RIGHT = "right"
CONF_COVERS = "covers"

DEFAULT_TIMEOUT = 90
DEFAULT_CAMERA_SCAN_INTERVAL = 30
MIN_CAMERA_SCAN_INTERVAL = 1
MAX_CAMERA_SCAN_INTERVAL = 86400
DEFAULT_CAMERA_MOTION_RECHECK_INTERVAL = 5
MIN_CAMERA_MOTION_RECHECK_INTERVAL = 0
MAX_CAMERA_MOTION_RECHECK_INTERVAL = 3600
DEFAULT_CONFIDENCE = 0.0
DEFAULT_AREA = {
    CONF_TOP: 0.0,
    CONF_LEFT: 0.0,
    CONF_BOTTOM: 1.0,
    CONF_RIGHT: 1.0,
    CONF_COVERS: True,
}
