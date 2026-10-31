import json
from dataclasses import field, asdict
from pydantic.dataclasses import dataclass
from typing import Optional
from typing import Dict
from typing import List

@dataclass
class DetectRegion:
    top: float
    left: float
    bottom: float
    right: float
    detect: Dict[str, float]
    covers: bool = False
    id: Optional[str] = None

@dataclass
class DetectRequest:
    id: Optional[str] = None
    detector_name: Optional[str] = None
    image: Optional[str] = ""
    throttle: Optional[float] = 0.0
    data: str = ""
    preprocess: List[str] = field(default_factory=list)
    detect: Dict[str, float] = field(default_factory=dict)
    regions: List[DetectRegion] = field(default_factory=list)
    profile_id: Optional[str] = None
    annotation_locale: Optional[str] = None

@dataclass
class MqttDetectRequest(DetectRequest):
    separate_detections: Optional[bool] = False
    crop: Optional[bool] = False
    binary_images: Optional[bool] = False

@dataclass
class MotionStreamRequest:
    source_id: str
    stream_source: str
    sample_interval: float = 0.2
    candidate_interval: float = 0.5
    max_width: int = 320
    pixel_threshold: int = 22
    changed_fraction: float = 0.004
    confirm_frames: int = 1
    quiet_frames: int = 4
    roi_polygons: List[List[List[float]]] = field(default_factory=list)

@dataclass
class Detector:
    name: str
    type: str
    model: str
    labels: List[str] = field(default_factory=list)
    width: int = 0
    height: int = 0

    def asdict(self):
        return asdict(self)

@dataclass
class DetectorsResponse:
    detectors: List[Detector] = field(default_factory=list)

    def asdict(self, include_none=True):
        ret = asdict(self)
        return ret if include_none else clean_none(ret)

@dataclass
class Detection:
    region_id: Optional[str] = None
    top: float = 0.0
    left: float = 0.0
    bottom: float = 0.0
    right: float = 0.0
    label: str = ""
    confidence: float = 0.0
    image: Optional[str] = None

    def asdict(self, include_none=True):
        ret = asdict(self)
        return ret if include_none else clean_none(ret)

@dataclass
class DetectResponse:
    id: Optional[str] = None
    image: Optional[str] = None
    detections: List[Detection] = field(default_factory=list)
    error: Optional[str] = None
    duration: Optional[int] = None

    def asdict(self, include_none=True):
        ret = asdict(self)
        return ret if include_none else clean_none(ret)

def clean_none(d):
    for key, value in list(d.items()):
        if value is None:
            del d[key]
        elif isinstance(value, dict):
            clean_none(value)
    return d  # For convenience
