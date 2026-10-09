from pathlib import Path
from urllib.request import urlopen

target = Path('data/face_landmarker.task')
target.parent.mkdir(parents=True, exist_ok=True)
url = 'https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task'
with urlopen(url, timeout=60) as response:
    target.write_bytes(response.read())
print(f'Downloaded {target}')
