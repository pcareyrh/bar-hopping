"""Tests for catalogue PDF parsing — legacy, trial-1358, trial-1482 and Pawlympics formats."""
from app.scraper.catalogue import (
    _parse_pdf_pages,
    _split_dog_handler,
    _event_name_from_code,
)


def _w(text: str, x0: float) -> dict:
    return {"text": text, "x0": x0}


def _line(words: list[dict]) -> dict:
    return {"text": " ".join(w["text"] for w in words), "words": words}


def test_split_dog_handler_four_words():
    words = [_w("200", 51), _w("Pippi", 88), _w("Ally Elizabeth", 277), _w("Poodle", 392)]
    assert _split_dog_handler(words) == ("Pippi", "Ally Elizabeth")


def test_split_dog_handler_shifted_columns():
    # PM sessions in trial 1358 use different x-positions; word indices stay stable.
    words = [_w("411", 51), _w("Toby", 86), _w("Julie Alessi", 314), _w("Associate Register", 425)]
    assert _split_dog_handler(words) == ("Toby", "Julie Alessi")


def test_split_dog_handler_non_four_returns_none():
    # Wrapped/split dog-name rows yield !=4 words; treat as unparseable.
    words = [_w("AG.CH. DbAgCh. Lefay Lively Little", 88)]
    assert _split_dog_handler(words) == (None, None)


def test_format_a_header_day_prefix():
    # Legacy header: "Saturday - Class (CODE) Judge: ..." — day in header, no Ring line.
    pages = [[
        _line([_w("Saturday - Novice Jumping (JD) Judge: Jane", 50)]),
        _line([_w("201", 51), _w("Pippi", 88), _w("Ally Elizabeth", 277), _w("Poodle", 392)]),
    ]]
    entries = _parse_pdf_pages(pages)
    assert len(entries) == 1
    e = entries[0]
    assert e["event_name"] == "Novice Jumping"
    assert e["day"] == 1
    assert e["height_group"] == 200
    assert e["dog_name"] == "Pippi"
    assert e["handler_name"] == "Ally Elizabeth"


def test_format_a_day_rollover_on_event_repeat():
    # Same event reappearing implies a new day in legacy format.
    pages = [[
        _line([_w("Saturday - Novice Jumping (JD) Judge: Jane", 50)]),
        _line([_w("201", 51), _w("Pippi", 88), _w("Ally", 277), _w("Poodle", 392)]),
        _line([_w("Saturday - Novice Jumping (JD) Judge: Jane", 50)]),
        _line([_w("202", 51), _w("Rex", 88), _w("Bob", 277), _w("Poodle", 392)]),
    ]]
    entries = _parse_pdf_pages(pages)
    assert [e["day"] for e in entries] == [1, 2]


def test_format_b_height_inline_with_ring_day_markers():
    # Trial 1358 format: height in class header, day on separate "Ring N DAY" lines.
    pages = [
        [
            _line([_w("Ring 1 SATURDAY AM", 50)]),
            _line([_w("Novice Agility (AD) 200 Judge: Jo Comber", 50)]),
            _line([_w("200", 51), _w("Tdrop", 88), _w("Meg Stow", 277), _w("Poodle", 392)]),
            _line([_w("202", 51), _w("Cocobo", 88), _w("Cathy Reade", 277), _w("Poodle", 392)]),
        ],
        [
            _line([_w("Ring 2 SUNDAY", 50)]),
            _line([_w("Open Jumping (JDO) 400 Judge: Jo Comber", 50)]),
            _line([_w("408", 51), _w("Cosmo", 88), _w("Stephanie Weir", 277), _w("Border Collie", 392)]),
        ],
    ]
    entries = _parse_pdf_pages(pages)
    assert len(entries) == 3
    sat = [e for e in entries if e["day"] == 1]
    sun = [e for e in entries if e["day"] == 2]
    assert len(sat) == 2 and len(sun) == 1
    assert sat[0]["event_name"] == "Novice Agility"
    assert sat[0]["height_group"] == 200
    assert sat[0]["height_group_total"] == 2
    assert sun[0]["event_name"] == "Open Jumping"
    assert sun[0]["height_group"] == 400
    assert sun[0]["dog_name"] == "Cosmo"
    assert sun[0]["handler_name"] == "Stephanie Weir"


def test_format_b_skips_non_competition_heights():
    pages = [[
        _line([_w("Ring 1 SATURDAY", 50)]),
        _line([_w("Junior Agility (JR) 100 Judge: Jane", 50)]),
        _line([_w("100", 51), _w("Tiny", 88), _w("Sam", 277), _w("Toy", 392)]),
    ]]
    assert _parse_pdf_pages(pages) == []


def test_format_b_class_continuation_on_next_page():
    # Same class header repeating on a continuation page shouldn't duplicate or split.
    pages = [
        [
            _line([_w("Ring 1 SATURDAY", 50)]),
            _line([_w("Masters Agility (ADM) 300 Judge: Dean", 50)]),
            _line([_w("301", 51), _w("Dog1", 88), _w("H1", 277), _w("Br", 392)]),
        ],
        [
            _line([_w("Ring 1 SATURDAY", 50)]),
            _line([_w("Masters Agility (ADM) 300 Judge: Dean", 50)]),
            _line([_w("302", 51), _w("Dog2", 88), _w("H2", 277), _w("Br", 392)]),
        ],
    ]
    entries = _parse_pdf_pages(pages)
    assert len(entries) == 2
    assert all(e["event_name"] == "Masters Agility" and e["height_group"] == 300 for e in entries)
    assert [e["cat_number"] for e in entries] == ["301", "302"]
    assert all(e["height_group_total"] == 2 for e in entries)


def test_event_name_from_code_with_session_digit():
    # Saturday AM/PM sessions of the same class get distinct event names so
    # they don't collide on (event_name, day) when buckets are flushed.
    assert _event_name_from_code("AD1") == "Novice Agility (AD1)"
    assert _event_name_from_code("ADX2") == "Excellent Agility (ADX2)"
    assert _event_name_from_code("JDM1") == "Masters Jumping (JDM1)"


def test_event_name_from_code_without_session_digit():
    # Sunday Open / single-session classes use the bare canonical name.
    assert _event_name_from_code("AD") == "Novice Agility"
    assert _event_name_from_code("ADO") == "Open Agility"
    assert _event_name_from_code("JDO") == "Open Jumping"


def test_event_name_from_code_unknown_returns_none():
    assert _event_name_from_code("XYZ") is None
    assert _event_name_from_code("") is None


def test_format_c_saturday_ring_and_class_code():
    # Trial 1482 Saturday format: day + ring + AM/PM in header, class code trails.
    pages = [[
        _line([_w("Saturday Ring 1 AM -Cam List - Novice  Judge: Cam List - AD1", 50)]),
        _line([_w("500", 51), _w("Quickstep", 88), _w("Leire Ituarte Perez", 277), _w("Sporting Register", 392)]),
        _line([_w("501", 51), _w("Rivlin", 88), _w("Cristian hinojosa", 277), _w("Sporting Register", 392)]),
    ]]
    entries = _parse_pdf_pages(pages)
    assert len(entries) == 2
    e = entries[0]
    assert e["event_name"] == "Novice Agility (AD1)"
    assert e["day"] == 1
    assert e["ring_number"] == "1"
    assert e["height_group"] == 500
    assert e["dog_name"] == "Quickstep"
    assert e["handler_name"] == "Leire Ituarte Perez"


def test_format_c_corrupted_judge_text_still_matches():
    # pdfplumber word extraction can merge adjacent columns into garbage like
    # "ExcellenJu  dge: C assie Crew - ADX2" — the trailing class code is the
    # only reliable anchor for the regex.
    pages = [[
        _line([_w("Saturday Ring 1 PM - Cassie Crew - ExcellenJu  dge: C assie Crew - ADX2", 50)]),
        _line([_w("525", 51), _w("Indianoak", 88), _w("Samantha Oliver", 277), _w("Border Collie", 392)]),
    ]]
    entries = _parse_pdf_pages(pages)
    assert len(entries) == 1
    assert entries[0]["event_name"] == "Excellent Agility (ADX2)"
    assert entries[0]["ring_number"] == "1"


def test_format_c_ring_2_jumping_uses_ring_2():
    # Ring 2 Saturday hosts the Jumping trials; ring_number must reflect that.
    pages = [[
        _line([_w("Saturday Ring 2 AM - Cassie Crew - Masters Judge: Cassie Crew - JDM1", 50)]),
        _line([_w("500", 51), _w("Dog", 88), _w("Handler", 277), _w("Br", 392)]),
    ]]
    entries = _parse_pdf_pages(pages)
    assert entries[0]["event_name"] == "Masters Jumping (JDM1)"
    assert entries[0]["ring_number"] == "2"


def test_format_d_sunday_with_RIng_typo():
    # Trial 1482 Sunday format: source has "RIng" (capital I) typo and the
    # class code in parentheses at end of line. Day comes from the header.
    pages = [[
        _line([_w("Sunday RIng 1 - Robyn Jones/Cam List - Open Agility (ADO)", 50)]),
        _line([_w("500", 51), _w("Dog", 88), _w("Handler", 277), _w("Border Collie", 392)]),
    ]]
    entries = _parse_pdf_pages(pages)
    assert len(entries) == 1
    e = entries[0]
    assert e["event_name"] == "Open Agility"
    assert e["day"] == 2
    assert e["ring_number"] == "1"


def test_format_c_day_rollover_to_format_d():
    # Saturday Format C followed by Sunday Format D should flush AM/PM buckets
    # under day=1 and start a fresh day=2 bucket.
    pages = [[
        _line([_w("Saturday Ring 1 AM - Judge - Novice Judge: J - AD1", 50)]),
        _line([_w("500", 51), _w("DogA", 88), _w("HA", 277), _w("Br", 392)]),
        _line([_w("Sunday RIng 1 - J - Open Agility (ADO)", 50)]),
        _line([_w("500", 51), _w("DogB", 88), _w("HB", 277), _w("Br", 392)]),
    ]]
    entries = _parse_pdf_pages(pages)
    assert [e["day"] for e in entries] == [1, 2]
    assert entries[0]["event_name"] == "Novice Agility (AD1)"
    assert entries[1]["event_name"] == "Open Agility"
    assert entries[0]["ring_number"] == "1"
    assert entries[1]["ring_number"] == "1"


def test_format_a_emits_null_ring_number():
    # Legacy format never knew about ring assignment; field stays None.
    pages = [[
        _line([_w("Saturday - Novice Jumping (JD) Judge: Jane", 50)]),
        _line([_w("201", 51), _w("Pippi", 88), _w("Ally", 277), _w("Poodle", 392)]),
    ]]
    entries = _parse_pdf_pages(pages)
    assert entries[0]["ring_number"] is None


def test_format_e_ring_day_header_then_class_code():
    # Pawlympics format: ring/day header on one line, class code on next line.
    pages = [[
        _line([_w("SATURDAY - RING 1 - AM Judge Cam List (NZ)", 50)]),
        _line([_w("AD1", 268)]),
        _line([_w("Cat#", 54), _w("Dog Name", 147), _w("Handler", 310), _w("Breed", 443)]),
        _line([_w("500", 57), _w("Quickstep Meant To Be", 91), _w("Leire Ituarte Perez", 261), _w("Sporting Register", 405)]),
        _line([_w("501", 57), _w("Rivlin", 91), _w("Cristian hinojosa", 261), _w("Sporting Register", 405)]),
    ]]
    entries = _parse_pdf_pages(pages)
    assert len(entries) == 2
    e = entries[0]
    assert e["event_name"] == "Novice Agility (AD1)"
    assert e["day"] == 1
    assert e["ring_number"] == "1"
    assert e["height_group"] == 500
    assert e["dog_name"] == "Quickstep Meant To Be"
    assert e["handler_name"] == "Leire Ituarte Perez"
    assert e["height_group_total"] == 2


def test_format_e_multiple_classes_under_same_ring():
    # Multiple class codes under one ring/day header (same ring, different events).
    pages = [[
        _line([_w("SATURDAY - RING 2 - AM Judge Cassie Crew (VIC)", 50)]),
        _line([_w("JDM1", 268)]),
        _line([_w("Cat#", 54), _w("Dog Name", 147), _w("Handler", 310), _w("Breed", 443)]),
        _line([_w("400", 57), _w("Dog1", 91), _w("Handler1", 261), _w("Breed1", 405)]),
        _line([_w("JD1", 268)]),
        _line([_w("Cat#", 54), _w("Dog Name", 147), _w("Handler", 310), _w("Breed", 443)]),
        _line([_w("300", 57), _w("Dog2", 91), _w("Handler2", 261), _w("Breed2", 405)]),
    ]]
    entries = _parse_pdf_pages(pages)
    assert len(entries) == 2
    assert entries[0]["event_name"] == "Masters Jumping (JDM1)"
    assert entries[0]["ring_number"] == "2"
    assert entries[0]["height_group"] == 400
    assert entries[1]["event_name"] == "Novice Jumping (JD1)"
    assert entries[1]["ring_number"] == "2"
    assert entries[1]["height_group"] == 300


def test_format_e_day_rollover_saturday_to_sunday():
    # Saturday ring header followed by Sunday ring header flushes day=1 entries.
    pages = [[
        _line([_w("SATURDAY - RING 1 - AM Judge Cam List (NZ)", 50)]),
        _line([_w("AD1", 268)]),
        _line([_w("500", 57), _w("DogA", 91), _w("HandlerA", 261), _w("Breed", 405)]),
        _line([_w("SUNDAY - RING 1 - Judges Robyn Jones / Cam List", 50)]),
        _line([_w("ADO", 268)]),
        _line([_w("500", 57), _w("DogB", 91), _w("HandlerB", 261), _w("Breed", 405)]),
    ]]
    entries = _parse_pdf_pages(pages)
    assert [e["day"] for e in entries] == [1, 2]
    assert entries[0]["event_name"] == "Novice Agility (AD1)"
    assert entries[1]["event_name"] == "Open Agility"

