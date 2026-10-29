"""Константы независимой интеграции DOOODS."""

DOMAIN = "dooods"
SERVICE_SCAN = "scan"

CONF_URL = "url"
CONF_AUTH_KEY = "auth_key"
CONF_SOURCE = "source"
CONF_MOTION_SENSORS = "motion_sensors"
CONF_CAMERA_PROFILES = "camera_profiles"
CONF_PROFILE_ID = "profile_id"
CONF_DETECTOR = "detector"
CONF_TIMEOUT = "timeout"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_CONFIDENCE = "confidence"
CONF_LABELS = "labels"
CONF_AREA = "area"
CONF_FILE_OUT = "file_out"
CONF_ARCHIVE_MAX_COUNT = "archive_max_count"
CONF_ARCHIVE_MAX_AGE_DAYS = "archive_max_age_days"

# Явная настройка камеры без сканирования по событиям движения.
MOTION_SENSOR_NONE = "none"

DEFAULT_ARCHIVE_MAX_COUNT = 10
DEFAULT_ARCHIVE_MAX_AGE_DAYS = 0

CONF_TOP = "top"
CONF_BOTTOM = "bottom"
CONF_LEFT = "left"
CONF_RIGHT = "right"
CONF_COVERS = "covers"

DEFAULT_TIMEOUT = 90
DEFAULT_SCAN_INTERVAL = 10
DEFAULT_CONFIDENCE = 0.0
DEFAULT_AREA = {
    CONF_TOP: 0.0,
    CONF_LEFT: 0.0,
    CONF_BOTTOM: 1.0,
    CONF_RIGHT: 1.0,
    CONF_COVERS: True,
}
