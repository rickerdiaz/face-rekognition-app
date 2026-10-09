"""Server-side movement check. This is not certified anti-spoofing."""
import os
import threading
import numpy as np

_lock = threading.Lock()
_detector = None


def count_blinks(scores):
    opened = False
    closed = 0
    count = 0
    for score in scores:
        if score < 0.25:
            if opened and closed >= 1:
                count += 1
            opened = True
            closed = 0
        elif score > 0.55 and opened:
            closed += 1
    return count


def verify_blinks(images):
    global _detector
    model = os.getenv('FACE_MODEL_PATH', 'data/face_landmarker.task')
    if not os.path.isfile(model):
        raise RuntimeError('Face landmark model missing. Run python scripts/download_model.py.')
    import mediapipe as mp
    with _lock:
        if _detector is None:
            options = mp.tasks.vision.FaceLandmarkerOptions(
                base_options=mp.tasks.BaseOptions(model_asset_path=model),
                num_faces=2, output_face_blendshapes=True)
            _detector = mp.tasks.vision.FaceLandmarker.create_from_options(options)
        scores = []
        reference = None
        for img in images:
            result = _detector.detect(mp.Image(image_format=mp.ImageFormat.SRGB,
                                               data=np.asarray(img.convert('RGB'))))
            if len(result.face_landmarks) != 1 or len(result.face_blendshapes) != 1:
                raise ValueError('Exactly one face must remain visible throughout the capture.')
            shapes = {v.category_name: v.score for v in result.face_blendshapes[0]}
            score = min(shapes.get('eyeBlinkLeft', 0), shapes.get('eyeBlinkRight', 0))
            scores.append(score)
            if score < 0.25:
                reference = img
        if count_blinks(scores) < 1:
            raise ValueError('No complete blink detected. Open your eyes, blink once with both eyes, then open them again during capture.')
        return reference
