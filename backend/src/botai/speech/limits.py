"""Size and length limits of one dictation, light enough for the upload middleware to import.

Three minutes of Opus or AAC is well under 3 MB even at 128 kbit/s, so 5 MiB leaves room for any
browser's recording and refuses the long, heavy file a client could use to spend the provider's
free tier. The page stops at three minutes itself; the server checks the claim, the size, and the
length the provider measured.
"""

from __future__ import annotations

ROUTE = "/api/v1/speech/transcribe"
MAX_AUDIO_BYTES = 5 * 1024 * 1024
MAX_AUDIO_SECONDS = 180
DURATION_SLACK_S = 10  # a recorder's last chunk and the clock of the page are not exact
# The file plus the multipart framing and the other fields.
BODY_LIMIT = MAX_AUDIO_BYTES + 1024 * 1024
