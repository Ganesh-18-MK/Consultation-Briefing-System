from app import timezones


def test_morning_block_deadline_is_one_hour_before_block_start():
    # 9:30 AM CDT on 2026-09-15 (September = Daylight Time, UTC-5) falls
    # inside the 9:00-10:15 AM block -> deadline is 8:00 AM CDT the same
    # day, i.e. 13:00 UTC.
    deadline = timezones.compute_brief_deadline_utc("2026-09-15T14:30:00+00:00")
    assert deadline == "2026-09-15T13:00:00+00:00"


def test_afternoon_block_deadline_is_30_minutes_before_block_start():
    # 4:00 PM CDT on 2026-09-15 falls inside the 3:00-5:15 PM block ->
    # deadline is 2:30 PM CDT the same day, i.e. 19:30 UTC.
    deadline = timezones.compute_brief_deadline_utc("2026-09-15T21:00:00+00:00")
    assert deadline == "2026-09-15T19:30:00+00:00"


def test_booking_outside_both_blocks_has_no_deadline():
    # Noon CDT — outside both the morning and afternoon blocks.
    assert timezones.compute_brief_deadline_utc("2026-09-15T17:00:00+00:00") is None


def test_block_boundaries_are_inclusive():
    # Exactly 10:15 AM CDT — the stated end of the morning block —
    # should still count as inside it.
    deadline = timezones.compute_brief_deadline_utc("2026-09-15T15:15:00+00:00")
    assert deadline is not None

    # One minute later, 10:16 AM CDT, should not.
    assert timezones.compute_brief_deadline_utc("2026-09-15T15:16:00+00:00") is None


def test_deadline_respects_standard_time_in_winter():
    # 9:00 AM CST on 2026-01-15 (January = Standard Time, UTC-6) ->
    # deadline 8:00 AM CST the same day, i.e. 14:00 UTC — confirms the
    # conversion uses real DST-aware tzdata, not a fixed offset.
    deadline = timezones.compute_brief_deadline_utc("2026-01-15T15:00:00+00:00")
    assert deadline == "2026-01-15T14:00:00+00:00"


def test_missing_or_unparseable_start_time_has_no_deadline():
    assert timezones.compute_brief_deadline_utc(None) is None
    assert timezones.compute_brief_deadline_utc("") is None
    assert timezones.compute_brief_deadline_utc("not-a-date") is None
