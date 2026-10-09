"""Server-side movement check. This is not certified anti-spoofing."""
import os
import threading
import logging
import numpy as np

_lock = threading.Lock()
_detector = None
logger = logging.getLogger('uvicorn.error')


def count_blinks(scores):
    opened = False
    closed = 0
    count = 0
    for score in scores:
        if score is None:
            opened = False
            closed = 0
            continue
        if score < 0.25:
            if opened and closed >= 1:
                count += 1
            opened = True
            closed = 0
        elif score > 0.55 and opened:
            closed += 1
    return count


def validate_capture(scores):
    valid = sum(score is not None for score in scores)
    gap = longest_gap = 0
    for score in scores:
        gap = gap + 1 if score is None else 0
        longest_gap = max(longest_gap, gap)
    blinks = count_blinks(scores)
    logger.info('blink_check frames=%d tracked=%d longest_tracking_gap=%d blinks=%d',
                len(scores), valid, longest_gap, blinks)
    if valid < max(12, len(scores) * 0.8) or longest_gap > 4:
        raise ValueError(f'Exactly one face must be visible for most of the capture. Face tracked in {valid}/{len(scores)} frames. Keep your face centered and improve lighting.')
    if blinks < 1:
        raise ValueError('No complete blink detected. Open your eyes, blink once with both eyes, then open them again during capture.')


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
            if len(result.face_landmarks) > 1:
                logger.warning('blink_check rejected=multiple_faces')
                raise ValueError('More than one face detected. Only the employee recording attendance should be in view.')
            if not result.face_landmarks or not result.face_blendshapes:
                scores.append(None)
                continue
            shapes = {v.category_name: v.score for v in result.face_blendshapes[0]}
            score = min(shapes.get('eyeBlinkLeft', 0), shapes.get('eyeBlinkRight', 0))
            scores.append(score)
            if score < 0.25:
                reference = img
        validate_capture(scores)
        return reference
