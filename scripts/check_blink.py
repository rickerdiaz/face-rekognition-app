"""Initialize the real detector, including native dependencies, in the deployed image."""
from PIL import Image
from app.blink import verify_blinks

try:
    verify_blinks([Image.new('RGB', (640, 480))] * 24)
except ValueError as exc:
    assert 'Exactly one face' in str(exc), str(exc)
    print('Blink detector initialized; blank capture rejected correctly.')
else:
    raise AssertionError('A blank capture must never pass the blink check.')
