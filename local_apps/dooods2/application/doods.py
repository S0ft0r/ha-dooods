import base64
import logging
import numpy as np
import cv2
import odrpc
import time

from detectors.tensorflow import Tensorflow
from detectors.tflite import TensorflowLite

try:
    from .annotations import AnnotationCanvas, AnnotationTranslations
    from .box_merging import build_display_boxes, pad_display_box
    from .zones import filter_detections_by_zones
except ImportError:  # запуск приложения как набора модулей из каталога application
    from annotations import AnnotationCanvas, AnnotationTranslations
    from box_merging import build_display_boxes, pad_display_box
    from zones import filter_detections_by_zones

logger = logging.getLogger('doods.doods')

# dict from detector type to class
detectors = {
    "tflite": TensorflowLite,
    "tensorflow": Tensorflow,
}

try:
    from detectors.pytorch import PyTorch
    detectors['pytorch'] = PyTorch
except ModuleNotFoundError:
    logger.info('PyTorch not installed...')

try:
    from detectors.deepstack import DeepStack
    detectors['deepstack'] = DeepStack
except ModuleNotFoundError:
    logger.info('DeepStack not installed...')

try:
    from detectors.tensorflow2 import Tensorflow2
    detectors['tensorflow2'] = Tensorflow2
except ModuleNotFoundError:
    logger.info("Tensorflow2 Object Detection API not installed...")

try:
    from detectors.yolo import YOLO
    detectors['yolo'] = YOLO
except ModuleNotFoundError:
    logger.info('YOLO not installed...')

font                   = cv2.FONT_HERSHEY_PLAIN
fontScale              = 1.2
thickness              = 1
lineType               = 4

# These are the valid types and the conversion to what cv2 needs.
detect_request_image_conversion = {
    'true'      : '.jpg',
    '.jpg'      : '.jpg',
    'jpg'       : '.jpg',
    'jpeg'      : '.jpg',
    'image/jpeg': '.jpg',
    '.png'      : '.png',
    'png'       : '.png',
    'image/png' : '.png',
}

detectors_load_precedence = [
    "tflite",
    "tensorflow",
    "tensorflow2",
    "deepstack",
    "pytorch",
    "yolo",
]

class MissingDetector:
    def __init__(self, dconfig):
        raise Exception('Unknown detector type %s.' % dconfig.type)

class Doods:
    def __init__(self, config, profile_store=None):
        self.config = config
        self.profile_store = profile_store
        self.annotation_translations = AnnotationTranslations(
            getattr(config, "annotation_labels", None)
        )

        self.config.detectors = sorted(self.config.detectors, key=lambda d: detectors_load_precedence.index(d.type) if d.type in detectors_load_precedence else 99)

        # Initialize the detectors
        self._detectors = {}
        for detector_config in self.config.detectors:
            detector_class = detectors.get(detector_config.type, MissingDetector)
            try:
                detector = detector_class(detector_config)
            except Exception as e:
                logger.error('Could not create detector %s/%s: %s' % (detector_config.type, detector_config.name, e))
                continue
            logger.info('Registered detector type:%s name:%s', detector.config.type, detector.config.name)
            self._detectors[detector_config.name] = detector

    # Get the detectors configs
    def detectors(self):
        detectors = []
        for name in self._detectors:
            detectors.append(self._detectors[name].config)
        return detectors

    # Detect image
    def detect(self, detect):
        profile = None
        profile_id = getattr(detect, "profile_id", None)
        if profile_id and getattr(detect, "regions", None):
            return odrpc.DetectResponse(
                id=detect.id,
                error="profile_id cannot be combined with legacy regions",
            )
        if profile_id:
            if self.profile_store is None:
                return odrpc.DetectResponse(
                    id=detect.id,
                    error="profile support is not configured",
                )
            profile = self.profile_store.get(profile_id)
            if profile is None:
                return odrpc.DetectResponse(
                    id=detect.id,
                    error=f"unknown profile id: {profile_id}",
                )

        # Coerce the image output type into something we like
        if detect.image:
            detect.image = detect_request_image_conversion.get(detect.image, '')

        # Get the detector
        if not detect.detector_name:
            detect.detector_name = 'default'
        if not detect.detector_name in self._detectors:
            return odrpc.DetectResponse(error="unknown detector name: %s" % detect.detector_name)
        detector = self._detectors[detect.detector_name]
        if not detector:
            ret = odrpc.DetectResponse
            ret.error = "could not determine detector"
            return ret

        # Already an image
        if type(detect.data) is np.ndarray:
            image = detect.data

        # If it's a url, use cv2 to read an image or frame.
        elif detect.data.startswith("http") or detect.data.startswith("rtsp") or detect.data.startswith("ftp"):
            cap = cv2.VideoCapture(detect.data)
            if cap.isOpened():
                _, image = cap.read()
                cap.release()
            else:
                raise 'No Image'
        
        # Should be base64 encoded image
        else:
            # Decode the image
            image_data = base64.b64decode(detect.data)
            image_bytes = np.frombuffer(image_data, dtype=np.uint8)
            image = cv2.imdecode(image_bytes, cv2.IMREAD_COLOR)
        
        # Handle preprocessing
        for process in detect.preprocess:
            if process == 'grayscale':
                image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            else:
                raise ValueError('unknown preprocessing request: %s' % process)

        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)

        # Run detection
        start = time.perf_counter()
        ret = detector.detect(image)
        if ret.error:
            return ret
        # Detection duration in milliseconds.
        ret.duration = (time.perf_counter() - start) * 1000

        # Set the id        
        ret.id = detect.id
        # Sort the detections by confidence
        ret.detections = sorted(ret.detections, key=lambda d: d.confidence, reverse=True)

        if self.config.log == 'all':
            logger.info(ret)

        if profile is None:
            # Без profile_id сохраняется исходный фильтр прямоугольных regions.
            ret.detections = Doods.filter_detections(
                ret.detections,
                detect.detect,
                detect.regions,
            )
        else:
            # Профиль заменяет request.regions; порядок зон задаёт приоритет.
            detection_dicts = [detection.asdict() for detection in ret.detections]
            filtered_detections = filter_detections_by_zones(
                detection_dicts,
                detect.detect,
                profile.polygon_zones,
            )
            ret.detections = [
                odrpc.Detection(**detection)
                for detection in filtered_detections
            ]

        if self.config.log == 'detections':
            logger.info(ret)

        # If no image was requested, return the detection object
        if not detect.image:
            return ret

        # Convert the image back to BGR for drawing boxes and saving.
        image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        height, width, _channels = image.shape
        annotation_locale = getattr(detect, "annotation_locale", None)
        text_annotations = []

        def display_label(label):
            """Перевести подпись кадра, не изменяя машинное имя метки."""
            return self.annotation_translations.display_label(label, annotation_locale)

        # Список глобальных порогов — диагностическая подпись только для debug.
        debug_annotations_enabled = logger.isEnabledFor(logging.DEBUG)
        if self.config.globals.enabled and debug_annotations_enabled:
            global_labels = [
                "%s:%d" % (display_label(label), detect.detect[label])
                for label in detect.detect
            ]
            if global_labels and self.config.globals.fontThickness:
                text_annotations.append(
                    (
                        ",".join(global_labels),
                        (5, 15),
                        tuple(self.config.globals.fontColor),
                        self.config.globals.fontScale,
                        self.config.globals.fontThickness,
                    )
                )

        if self.config.regions.enabled:
            region_color = tuple(reversed(self.config.regions.boxColor))
            if profile is not None:
                for zone in profile.zones:
                    polygon_points = [
                        (
                            int(round(left * (width - 1))),
                            int(round(top * (height - 1))),
                        )
                        for left, top in zone.polygon.points
                    ]
                    if self.config.regions.boxThickness:
                        polygon = np.array(polygon_points, dtype=np.int32).reshape(
                            (-1, 1, 2)
                        )
                        cv2.polylines(
                            image,
                            [polygon],
                            True,
                            region_color,
                            thickness=self.config.regions.boxThickness,
                        )
                    if self.config.regions.fontThickness:
                        zone_left = min(point[0] for point in polygon_points)
                        zone_top = min(point[1] for point in polygon_points)
                        text_annotations.append(
                            (
                                zone.name,
                                (zone_left, max(0, zone_top - 2)),
                                tuple(reversed(self.config.regions.fontColor)),
                                self.config.regions.fontScale,
                                self.config.regions.fontThickness,
                            )
                        )
            else:
                # Legacy regions остаются прямоугольными; список их порогов
                # тоже относится к диагностической разметке и виден только в debug.
                for region in detect.regions:
                    region_labels = [
                        "%s:%d" % (display_label(label), region.detect[label])
                        for label in region.detect
                    ]
                    if (
                        debug_annotations_enabled
                        and region_labels
                        and self.config.regions.fontThickness
                    ):
                        text_annotations.append(
                            (
                                ",".join(region_labels),
                                (
                                    int(region.left * width),
                                    int(region.top * height) - 2,
                                ),
                                tuple(reversed(self.config.regions.fontColor)),
                                self.config.regions.fontScale,
                                self.config.regions.fontThickness,
                            )
                        )
                    if self.config.regions.boxThickness:
                        cv2.rectangle(
                            image,
                            (int(region.left * width), int(region.top * height)),
                            (int(region.right * width), int(region.bottom * height)),
                            color=region_color,
                            thickness=self.config.regions.boxThickness,
                        )

        if self.config.boxes.enabled:
            display_boxes = [
                pad_display_box(
                    box,
                    getattr(self.config.boxes, "boxPaddingPercent", 0.0),
                )
                for box in build_display_boxes(
                    ret.detections,
                    merge_overlapping=getattr(
                        self.config.boxes,
                        "mergeOverlapping",
                        True,
                    ),
                )
            ]
            for box in display_boxes:
                if (
                    getattr(self.config.boxes, "showLabels", True)
                    and self.config.boxes.fontThickness
                ):
                    label_text = display_label(box.label)
                    if box.count > 1:
                        label_text += f" ×{box.count}"
                    else:
                        label_text += ":%d" % box.confidence
                    text_annotations.append(
                        (
                            label_text,
                            (
                                int(box.left * width),
                                int(box.top * height) - 2,
                            ),
                            tuple(reversed(self.config.boxes.fontColor)),
                            self.config.boxes.fontScale,
                            self.config.boxes.fontThickness,
                        )
                    )
                if self.config.boxes.boxThickness:
                    cv2.rectangle(
                        image,
                        (int(box.left * width), int(box.top * height)),
                        (int(box.right * width), int(box.bottom * height)),
                        color=tuple(reversed(self.config.boxes.boxColor)),
                        thickness=self.config.boxes.boxThickness,
                    )

        if text_annotations:
            canvas = AnnotationCanvas(image)
            for text, origin, color, font_scale, font_thickness in text_annotations:
                canvas.draw_text(text, origin, color, font_scale, font_thickness)
            image = canvas.to_bgr()

        ret.image = cv2.imencode(detect.image, image)[1].tostring()
        return ret

    # Filter the detections to the matches
    @staticmethod
    def filter_detections(detections, detect, regions):
        ret = {}
        for i, d in enumerate(detections):
            if d.label in detect:
                if d.confidence >= detect[d.label]:
                    ret[i] = d
                    continue
            elif '*' in detect and d.confidence >= detect['*']:
                ret[i] = d
                continue
            for r in regions:
                if (
                    ( r.covers and r.top <= d.top and r.left <= d.left and r.bottom >= d.bottom and r.right >= d.right ) or
                    ( not r.covers and d.top <= r.bottom and d.left <= r.right and d.bottom >= r.top and d.right >= r.left )
                ):
                    if d.label in r.detect:
                        if d.confidence >= r.detect[d.label]:
                            ret[i] = d
                            ret[i].region_id = r.id # Add ID of region for which this passed filters.
                            break
                    elif '*' in r.detect and d.confidence >= r.detect['*']:
                        ret[i] = d
                        ret[i].region_id = r.id # Add ID of region for which this passed filters.
                        break
        return list(ret.values())
