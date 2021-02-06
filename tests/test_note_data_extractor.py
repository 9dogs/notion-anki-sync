"""Tests for note data extractor."""
from pathlib import Path

from notion_anki_sync.parser import NoteDataExtractor

#: Base directory
BASE_DIR = Path(__file__).parent
#: Note HTML
NOTE_HTML = (BASE_DIR / 'data/note.html').read_text(encoding='utf8')
#: Note with clozes HTML
NOTE_WITH_CLOZES_HTML = (BASE_DIR / 'data/note_with_cloze.html').read_text(
    encoding='utf8'
)


def test_parses_html():
    """Extract note data from HTML."""
    note = NoteDataExtractor.extract_note(NOTE_HTML, Path('/test-path'))
    assert note.front == (
        '<mark class="highlight-orange">Front </mark>with '
        '<strong>bold</strong> and \\(\\LaTeX\\) inline'
    )
    assert note.back == (
        '<p class="">Back with a picture</p>'
        '<figure class="image"><a href="testimagepng_image.png">'
        '<img style="width:317px" src="testimagepng_image.png"></img></a>'
        '</figure><p class="">and</p>'
        '\\[block\\ \\LaTeX\\]'
        '<p class="">block</p><p class=""></p>'
    )
    assert note.tags == ['tag1', 'tag2']
    assert len(note.images) == 1
    image = note.images[0]
    assert image.src == 'test/image.png'
    assert image.abs_path == Path('/test-path') / 'test/image.png'
    assert image.filename == 'testimagepng_image.png'


def test_parses_html_with_cloze():
    """Extract note data from HTML creating note with clozes."""
    note = NoteDataExtractor.extract_note(
        NOTE_WITH_CLOZES_HTML, Path('/test-path')
    )
    assert note.front == 'Front with {{c1::\\(cloze_1\\)}} and {{c2::cloze2}}'
    assert note.tags == ['tag1', 'tag2']
    assert not note.back
    assert not note.images
