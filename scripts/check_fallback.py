"""Exercise the outage/review workflow in an isolated temporary database."""
import base64
import io
import os
import tempfile

from botocore.exceptions import EndpointConnectionError
from PIL import Image

with tempfile.TemporaryDirectory(prefix='dtr-fallback-check-') as folder:
    os.environ['DATA_DIR'] = folder
    from app import main

    class UnreachableAWS:
        def compare_faces(self, **kwargs):
            raise EndpointConnectionError(endpoint_url='https://rekognition.test')

    main.aws_client = lambda: UnreachableAWS()
    main.verify_blinks = lambda images: images[0]
    with main.connection() as con:
        con.execute('INSERT INTO employees(id,name,department,face) VALUES(?,?,?,?)',
                    ('TEST-ONLY', 'Isolated Test Employee', 'Test', b'photo'))
    challenge = main.challenge(main.ChallengeInput(action='time_in'))
    with main.connection() as con:
        con.execute('UPDATE challenges SET issued=issued-4 WHERE id=?', (challenge['id'],))
    out = io.BytesIO()
    Image.new('RGB', (64,64)).save(out, 'JPEG')
    frame = base64.b64encode(out.getvalue()).decode()
    result = main.punch(main.PunchInput(challenge_id=challenge['id'], frames=[frame]*24))
    assert result['status'] == 'pending'
    assert main.latest_punch() is None
    assert (main.PENDING_DIR / (result['pending_id'] + '.jpg')).is_file()
    main.review_pending(result['pending_id'], main.ReviewInput(
        decision='approve', employee_id='TEST-ONLY', note='Isolated runtime test'))
    assert main.latest_punch()['at'] == result['at']
    assert main.status('TEST-ONLY')['state'] == 'working'
    print('Isolated outage capture, persistent photo, pending exclusion, and timestamp-preserving approval passed.')
