import os
import yaml
import argparse
import logging
from api import API
from mqtt import MQTT
from config import Config
from doods import Doods
from profiles import ProfileStore
from face_gallery import FaceGalleryStore, FaceGalleryStoreError
from face_debug_store import FaceDebugStore, FaceDebugStoreError
from face_settings import FaceSettingsError, FaceSettingsStore
from face_analyzer import FaceAnalyzer
from supervisor_options import SupervisorOptionsError, ensure_api_key

# Allows you to pass a flattened dict into an unflattened one for things like
# Hass.IO that only allows config a single level deep for some reason.
def unflatten_dict(d):
    ret = {}
    def sub_unflatten_dict(subd, keys, value):
        if len(keys) > 0:
            key = keys[0]
            if not key in subd:
                subd[key] = {}
            subd[key] = sub_unflatten_dict(subd[key], keys[1:], value)
            return subd
        else:
            return value

    for path, val in d.items():
        keys = path.split(".")
        key = keys[0]
        if not key in ret:
            ret[key] = {}
        ret[key] = sub_unflatten_dict(ret[key], keys[1:], val)

    return ret

def hex_to_rgb(hex):
  return tuple(int(hex.strip('#')[i:i+2], 16) for i in (0, 2, 4)) 

def main():
    parser = argparse.ArgumentParser(description='DOODS2 - Dedicated Open Object Detection Service')
    parser.add_argument('--config', '-c', help='Configuration File', default='defaults.yaml')
    parser.add_argument('action', nargs='?', help='Action: api=Start REST api, mqtt=Start MQTT forwarder', default='api')
    args = parser.parse_args()

    # Use environment, then arguments, then the bundled defaults file.
    config_file = os.environ.get('CONFIG_FILE', args.config)

    # Load config file
    with open(config_file, 'r') as stream:
        try:
            config = Config(**unflatten_dict(yaml.safe_load(stream)))
        except yaml.YAMLError as exc:
            print(exc)

    # It's ugly and I'm sure there's a better way to do it, but if we specified colors as hex strings
    # convert them to color arrays.
    if isinstance(config.doods.boxes.boxColor, str):
        config.doods.boxes.boxColor = hex_to_rgb(config.doods.boxes.boxColor)
    if isinstance(config.doods.boxes.fontColor, str):
        config.doods.boxes.fontColor = hex_to_rgb(config.doods.boxes.fontColor)
    if isinstance(config.doods.regions.boxColor, str):
        config.doods.regions.boxColor = hex_to_rgb(config.doods.regions.boxColor)
    if isinstance(config.doods.regions.fontColor, str):
        config.doods.regions.fontColor = hex_to_rgb(config.doods.regions.fontColor)
    if isinstance(config.doods.globals.fontColor, str):
        config.doods.globals.fontColor = hex_to_rgb(config.doods.globals.fontColor)

    # Setup logging
    level = logging.getLevelName(config.logger.level.upper())
    logger = logging.getLogger("doods")
    logger.propagate = False
    logger.setLevel(level)
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    handler = logging.StreamHandler()
    handler.setLevel(level)
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    # Generate once on first install, then keep the key in Supervisor options.
    try:
        config.server.auth_key = ensure_api_key(
            config.server.auth_key,
            os.environ.get("SUPERVISOR_TOKEN"),
        )
    except SupervisorOptionsError as err:
        logger.error("Could not initialize server.auth_key: %s", err)
        raise SystemExit(1) from None

    # The App exposes only its protected API; the HA integration provides the UI.
    # Initialize the versioned profile store and detection service.
    profiles_file = config.server.profiles_file or "/data/profiles.json"
    profile_store = ProfileStore(profiles_file)
    doods = Doods(config.doods, profile_store=profile_store)

    if args.action == 'api':
        # Face analysis is alpha: model initialization and capture persistence run
        # off the API event loop and failures do not affect object detection.
        face_options = getattr(config, "face", None)
        yunet_score_threshold = getattr(
            face_options,
            "yunet_score_threshold",
            0.9,
        )
        min_face_width = getattr(face_options, "min_face_width", 100)
        min_face_height = getattr(face_options, "min_face_height", 100)
        # Пустой/отсутствующий список в config.yaml — фильтр не настроен.
        face_detection_cameras = getattr(
            face_options, "face_detection_cameras", None
        ) or None
        # Список, сохранённый интеграцией, важнее значения из config.yaml:
        # в Home Assistant файл конфигурации App только для чтения.
        face_settings_store = FaceSettingsStore("/data/face_settings.json")
        try:
            saved_face_cameras = face_settings_store.load_cameras()
        except FaceSettingsError as err:
            logger.warning(
                "Face settings could not be read, using config value: %s", err
            )
            saved_face_cameras = None
        if saved_face_cameras is not None:
            face_detection_cameras = saved_face_cameras
        if face_detection_cameras is None:
            face_cameras_label = "(all)"
        elif not face_detection_cameras:
            face_cameras_label = "(none)"
        else:
            face_cameras_label = ", ".join(face_detection_cameras)
        logger.info("Face detection cameras: %s", face_cameras_label)
        face_debug_store = None
        try:
            face_debug_store = FaceDebugStore(
                "/data/face_debug",
                min_face_width=min_face_width,
                min_face_height=min_face_height,
                max_matches=30,
            )
        except FaceDebugStoreError as err:
            logger.error("Face-debug capture storage is unavailable: %s", err)

        # Галерея лиц хранится только в постоянном каталоге данных App.
        # Повреждённый файл отключает новую функцию, но не основной DOOODS API.
        face_gallery_store = None
        try:
            face_gallery_store = FaceGalleryStore(
                "/data/face_gallery.json",
                face_debug_store=face_debug_store,
            )
        except FaceGalleryStoreError as err:
            logger.error("Face gallery is disabled because storage is unavailable: %s", err)

        face_match_threshold = getattr(
            face_options, "face_match_threshold", 0.4
        )
        face_match_min_gap = getattr(
            face_options, "face_match_min_gap", 0.1
        )
        logger.info(
            "Face matching settings: threshold=%.3f, min_gap=%.3f, yunet=%.2f",
            face_match_threshold if face_match_threshold is not None else 0.4,
            face_match_min_gap if face_match_min_gap is not None else 0.1,
            yunet_score_threshold if yunet_score_threshold is not None else 0.9,
        )
        face_analyzer = FaceAnalyzer(
            "/data/face_models",
            yunet_score_threshold=yunet_score_threshold,
            face_match_threshold=face_match_threshold,
            face_match_min_gap=face_match_min_gap,
            face_detection_cameras=face_detection_cameras,
            face_gallery_store=face_gallery_store,
            debug_store=face_debug_store,
        )

        # Start the server
        api = API(
            config.server,
            doods,
            face_gallery_store=face_gallery_store,
            face_analyzer=face_analyzer,
            face_debug_store=face_debug_store,
            face_settings_store=face_settings_store,
        )
        api.run()
    elif args.action == 'mqtt':
        # Start the server
        mqtt = MQTT(config.mqtt, doods, metrics_server_config=config.server)
        mqtt.run()
    else:
        print('Unknown action: '+args.action)

if __name__ == "__main__":
    main()
