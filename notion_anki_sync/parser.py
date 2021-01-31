"""Parser to extract Anki note data from HTML."""
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote

import lxml.html

from notion_anki_sync.models.anki import Image, Note


class NoteDataExtractor(HTMLParser):
    """Parser to extract Anki note data from HTML."""

    def __init__(self, base_dir: Path):
        """Init extractor.

        :param base_dir: base dir of a file being parsed
        """
        super().__init__(convert_charrefs=True)
        self.base_dir = base_dir
        self.start_collecting = False
        self.buffer: List[str] = []
        self.note_data: Dict[str, Any] = {}

    def _get_attr_by_name(
        self, name: str, attrs: Iterable[Tuple[str, Optional[str]]]
    ) -> Optional[str]:
        """Get an attribute value by its name.

        :param name: name of an attribute
        :param attrs: attributes
        :returns: value of the attribute or None if no such attribute
        """
        for attr, value in attrs:
            if attr == name:
                return value
        return None

    def handle_starttag(
        self, tag: str, attrs: Iterable[Tuple[str, Optional[str]]]
    ) -> None:
        """Handle start tag.

        :param tag: a tag
        :param attrs: tag attributes
        """
        # If parser should collect data - add tags as is
        if self.start_collecting:
            attrs_and_values = ' '.join(
                f'{attr}="{value}"' for attr, value in attrs
            )
            if attrs_and_values:
                self.buffer.append(f'<{tag} {attrs_and_values}>')
            else:
                self.buffer.append(f'<{tag}>')
        # <ul> marks the start of a new note
        elif tag == 'ul':
            self.note_data['id'] = self._get_attr_by_name('id', attrs)
        # Start collecting data from <summary> tag
        elif tag == 'summary':
            self.start_collecting = True
        # Track images to upload them to Anki deck
        if tag == 'img':
            src = self._get_attr_by_name('src', attrs)
            assert src  # mypy
            image = Image(src=src, abs_path=self.base_dir / unquote(src))
            self.note_data.setdefault('images', []).append(image)

    def handle_data(self, data: str) -> None:
        """Handle data.

        :param data: data
        """
        if not self.start_collecting:
            return
        # Check if data string should be considered tags.  Tags can be added
        # only once
        if data.startswith('#') and 'tags' not in self.note_data:
            self.note_data['tags'] = [tag.lstrip('#') for tag in data.split()]
        # Add data as is
        else:
            self.buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        """Handle end tag.

        :param tag: tag
        """
        # <summary> marks an end of a front side
        if tag == 'summary':
            self.note_data['front'] = ''.join(self.buffer)
            self.buffer.clear()
        # Add tag as is
        else:
            self.buffer.append(f'</{tag}>')

    def get_data(self) -> Note:
        """Collect the rest of the data.

        :returns: Note model
        """
        self.note_data['back'] = ''.join(self.buffer)
        return Note(**self.note_data)

    @classmethod
    def extract_note(cls, html: str, base_dir: Path) -> Note:
        """Extract Note from HTML fragment.

        :param html: HTML
        :param base_dir: directory of HTML file (for construct absolute path
            of images)
        :returns: Note object
        """
        parser = cls(base_dir)
        parser.feed(html)
        note = parser.get_data()
        return note


def extract_notes_data(source: Path, notion_namespace: str) -> List[Note]:
    """Extract notes data from HTML source.

    :param source: HTML path
    :param notion_namespace: Notion namespace (to form `source` fields)
    :return: notes
    """
    notes = []
    doc = lxml.html.parse(str(source))
    article = doc.xpath('//article')[0]
    article_id = article.attrib['id'].replace('-', '')
    note_nodes = doc.xpath('//ul[contains(@class, "toggle")]')
    for note_node in note_nodes:
        html = lxml.html.tostring(note_node, encoding='utf8').decode()
        note = NoteDataExtractor.extract_note(html, base_dir=source.parent)
        note.source = f'https://notion.so/{notion_namespace}/{article_id}'
        notes.append(note)
    return notes
