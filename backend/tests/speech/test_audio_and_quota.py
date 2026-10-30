"""Recording formats are read from the bytes; the quota slides, refunds and isolates users."""

from __future__ import annotations

import asyncio

import pytest

from botai.speech.audio import EXTENSIONS, sniff_audio
from botai.speech.quota import DAY_S, HOUR_S, QuotaRefused, SpeechQuota

WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 24
OGG = b"OggS\x00\x02" + b"\x00" * 24
M4A = b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00" + b"\x00" * 16
SAFARI = b"\x00\x00\x00\x1cftypisom\x00\x00\x02\x00" + b"\x00" * 16
HEIC = b"\x00\x00\x00\x20ftypheic\x00\x00\x00\x00" + b"\x00" * 16
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 16
FLAC = b"fLaC\x00\x00\x00\x22" + b"\x00" * 16
MP3_TAG = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 24
MP3_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 28
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
UTF16 = "Привет".encode("utf-16")  # BOM FF FE looks like an MPEG sync


@pytest.mark.parametrize(
    ("data", "mime"),
    [
        (WEBM, "audio/webm"),
        (OGG, "audio/ogg"),
        (M4A, "audio/mp4"),
        (SAFARI, "audio/mp4"),
        (WAV, "audio/wav"),
        (FLAC, "audio/flac"),
        (MP3_TAG, "audio/mpeg"),
        (MP3_FRAME, "audio/mpeg"),
    ],
)
def test_a_container_is_recognised_by_its_first_bytes(data: bytes, mime: str) -> None:
    assert sniff_audio(data) == mime
    assert mime in EXTENSIONS  # the provider can name the file for its decoder


@pytest.mark.parametrize("data", [PNG, HEIC, UTF16, b"<html></html>", b"plain text", b"\x00" * 16])
def test_pictures_text_and_noise_are_not_recordings(data: bytes) -> None:
    assert sniff_audio(data) is None


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def quota(clock: Clock, *, per_hour: int = 3, per_day: int = 300, parallel: int = 2) -> SpeechQuota:
    return SpeechQuota(per_hour=per_hour, seconds_per_day=per_day, parallel=parallel, clock=clock)


def use(q: SpeechQuota, user: str, seconds: float) -> None:
    with q.slot(user, seconds):
        pass


def test_requests_per_hour_slide_and_say_when_the_oldest_leaves() -> None:
    clock = Clock()
    q = quota(clock)
    for _ in range(3):
        use(q, "u", 1)
        clock.now += 60
    with pytest.raises(QuotaRefused) as refused:
        use(q, "u", 1)
    assert refused.value.scope == "hour"
    assert refused.value.retry_after_s == pytest.approx(HOUR_S - 180)
    clock.now += HOUR_S - 180  # the first of the three leaves the window
    use(q, "u", 1)


def test_seconds_per_day_count_what_was_recorded_and_free_up_as_the_day_moves() -> None:
    clock = Clock()
    q = quota(clock, per_hour=100, per_day=300)
    use(q, "u", 150)
    clock.now += 4000
    use(q, "u", 150)
    clock.now += 10
    with pytest.raises(QuotaRefused) as refused:
        use(q, "u", 20)
    assert refused.value.scope == "day"
    assert refused.value.retry_after_s == pytest.approx(DAY_S - 4010)  # the first 150 s age out
    clock.now += DAY_S - 4010
    use(q, "u", 20)


def test_users_are_counted_separately() -> None:
    clock = Clock()
    q = quota(clock, per_hour=1)
    use(q, "a", 1)
    use(q, "b", 1)
    with pytest.raises(QuotaRefused):
        use(q, "a", 1)


def test_an_error_inside_the_slot_does_not_give_the_seconds_back() -> None:
    """A timeout, a dropped connection or a cancelled request may have been processed already."""
    clock = Clock()
    q = quota(clock, per_hour=10, per_day=100)
    for failure in (RuntimeError("provider down"), TimeoutError(), asyncio.CancelledError()):
        with pytest.raises(type(failure)), q.slot("u", 60):
            raise failure
        clock.now += 1
        with pytest.raises(QuotaRefused) as refused:  # the 60 s of the failure are still spent
            use(q, "u", 60)
        assert refused.value.scope == "day"
        clock.now += DAY_S  # the day passes and the account is clear again
    use(q, "u", 60)


def test_a_plain_refusal_is_refunded_by_the_caller_but_still_counts_as_a_request() -> None:
    clock = Clock()
    q = quota(clock, per_hour=2, per_day=100)
    with q.slot("u", 100) as slot:
        slot.refund()
    use(q, "u", 100)  # the 100 s of the refused one were returned
    with pytest.raises(QuotaRefused) as refused:  # but two requests were made in the hour
        use(q, "u", 1)
    assert refused.value.scope == "hour"


def test_settle_replaces_the_estimate_with_the_length_the_provider_measured() -> None:
    clock = Clock()
    q = quota(clock, per_hour=100, per_day=300)
    with q.slot("u", 1) as slot:  # the client claimed one second
        slot.settle(250)  # the provider heard four minutes
    with pytest.raises(QuotaRefused) as refused:
        use(q, "u", 100)
    assert refused.value.scope == "day"
    with q.slot("v", 200) as slot:  # an over-claim is charged by what was really said
        slot.settle(20)
    use(q, "v", 270)


def test_parallel_requests_of_one_user_are_limited() -> None:
    clock = Clock()
    q = quota(clock, per_hour=100, parallel=2)
    with q.slot("u", 1), q.slot("u", 1):
        with pytest.raises(QuotaRefused) as refused, q.slot("u", 1):
            pass
        assert refused.value.scope == "busy"
        with q.slot("other", 1):  # another user is not held up
            pass
    use(q, "u", 1)  # both slots were released
