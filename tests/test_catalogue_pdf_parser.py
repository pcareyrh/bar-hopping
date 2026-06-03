"""Tests for catalogue PDF parsing — both legacy and trial-1358 formats."""
from app.scraper.catalogue import _parse_pdf_pages, _split_dog_handler


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
