import cv2
import odrpc
import time
import logging
from fresh_frame import FreshestFrame

class Streamer:
    def __init__(self, doods):
        self.doods = doods
        self.logger = logging.getLogger("doods.streamer")

    def start_stream(self, detect_request: odrpc.DetectRequest):
        url = detect_request.data
        last_frame_time = 0
        vcap = self.create_vcap(url)
        try:
            while True:
                if time.time() - last_frame_time < detect_request.throttle:
                    time.sleep(detect_request.throttle - (time.time() - last_frame_time))
                last_frame_time = time.time()
                _, detect_request.data = vcap.read()
                if detect_request.data is None:
                    self.logger.error("No stream frame; reconnecting")
                    vcap.release()
                    vcap = None
                    vcap = self.create_vcap(url)
                    continue
                detect_response = self.doods.detect(detect_request)
                if (yield detect_response):
                    return
        finally:
            if vcap is not None:
                vcap.release()

    def create_vcap(self, url):
        vcap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        vcap = FreshestFrame(vcap)
        return vcap

    @staticmethod
    def mjpeg_streamer(gen):
        # StreamingResponse iterates synchronous iterators in its thread pool.
        try:
            for detect_response in gen:
                yield (b'--frame\r\n' b'Content-Type: image/jpeg\r\n\r\n'
                       + bytearray(detect_response.image) + b'\r\n')
        finally:
            gen.close()
