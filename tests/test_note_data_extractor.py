"""Tests for note data extractor."""
from pathlib import Path

from notion_sync_addon.parser import NoteDataExtractor

#: Base directory
BASE_DIR = Path(__file__).parent


def test_parses_html():
    """Extract note data from HTML."""
    html = (BASE_DIR / 'data/note.html').read_text(encoding='utf8')
    note = NoteDataExtractor.extract_note(html, BASE_DIR / 'data')
    assert note.front == (
        '<mark class="highlight-orange">Front </mark>with '
        '<strong>bold</strong> and \\(\\LaTeX\\) inline'
    )
    assert note.back == (
        '<p>Back with a picture</p>'
        '<figure class="image"><a href="testimagepng_image.png">'
        '<img style="width:317px" src="testimagepng_image.png"></img></a>'
        '</figure><p>and</p>\\[block\\ \\LaTeX\\]<p>block</p>'
    )
    assert note.tags == ['tag1', 'tag2']
    assert len(note.images) == 1
    image = note.images[0]
    assert image.src == 'test/image.png'
    assert image.abs_path == BASE_DIR / 'data/test/image.png'
    assert image.filename == 'testimagepng_image.png'
    assert image.data == b'1'


def test_parses_html_with_cloze():
    """Extract note data from HTML creating note with clozes."""
    html = (BASE_DIR / 'data/note_with_cloze.html').read_text(encoding='utf8')
    note = NoteDataExtractor.extract_note(html, Path('/test-path'))
    assert note.front == 'Front with {{c1::\\(cloze_1\\)}} and {{c2::cloze2}}'
    assert note.tags == ['tag1', 'tag2']
    assert not note.back
    assert not note.images


def test_parses_html_with_mark():
    """Extract note data from HTML with <mark> tags."""
    html = (BASE_DIR / 'data/note_with_mark.html').read_text(encoding='utf8')
    note = NoteDataExtractor.extract_note(html, BASE_DIR / 'data')
    assert note.front == (
        'What is the <em>order of central tendencies</em> for a skewed to '
        'the left distribution?'
    )
    assert note.back == (
        '<p><mark class="highlight-red">Mode</mark>, '
        '<mark class="highlight-teal">median</mark>, '
        '<mark class="highlight-blue">mean.</mark></p>'
        '<figure class="image"><a href="testimagepng_image.png">'
        '<img style="width:570px" src="testimagepng_image.png"></img></a>'
        '</figure>'
    )
    assert note.tags == ['statistics', 'theory']
