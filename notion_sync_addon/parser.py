"""Parser to extract Anki note data from HTML."""
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote

from bs4 import BeautifulSoup

from .helpers import get_logger


@dataclass
class AnkiImage:
    """An image from HTML document."""

    #: `src` attribute as is in HTML document
    src: str
    #: Filename to be stored as
    filename: str
    #: Absolute path to the image
    abs_path: Path
    #: Image data
    data: bytes


@dataclass
class AnkiNote:
    """Anki note model."""

    #: Front side
    front: str
    #: Back side (can be empty for cloze note)
    back: Optional[str] = None
    #: Tags
    tags: Optional[List[str]] = None
    #: Link to Notion page
    source: Optional[str] = None
    #: Note images
    images: Optional[List[AnkiImage]] = None


class NoteDataExtractor(HTMLParser):
    """Parser to extract Anki note data from HTML.

    For each toggle block Notion generates HTML like this:
    <ul id=<guid> class="toggle">
        <li>
            <details open="">
                <summary>Front side of a note</summary>
                <p>Back side paragraph 1.</p>
                <p>Back side paragraph 2.</p>
            </details>
        </li>
    </ul>

    LaTeX blocks follow MathML notation with a lot of generated HTML tags,
    but parser only cares about <annotation> tag which contains LaTeX code
    and <div class="equation-container"> wrapper tag to differ inline and
    block LaTeX.
    """

    #: Allowed tags
    ALLOWED_TAGS = {
        'h1',
        'h2',
        'h3',
        'p',
        'strong',
        'summary',
        'em',
        'del',
        'pre',
        'code',
        'mark',
        'ul',
        'ol',
        'li',
        'div',
        'span',
        'blockquote',
        'hr',
        'figure',
        'a',
        'img',
    }
    #: Do not save these attributes
    SKIP_ATTRIBUTES = {'id'}
    #: Notion inline LaTeX class
    INLINE_LATEX_CLASS = 'notion-text-equation-token'
    #: Notion block LaTeX class
    BLOCK_LATEX_CLASS = 'equation'
    #: Anki block LaTeX tags
    ANKI_BLOCK_LATEX_TAGS = ('\\[', '\\]')
    #: Anki LaTeX tags
    ANKI_INLINE_LATEX_TAGS = ('\\(', '\\)')
    #: Empty paragraphs regex
    EMPTY_P_RE = re.compile(r'<p [^>]*></p>')

    def __init__(self, base_dir: Path, debug: bool = False) -> None:
        """Init extractor.

        :param base_dir: base dir of a file being parsed
        """
        super().__init__(convert_charrefs=True)
        self.logger = get_logger(self.__class__.__name__, debug)
        self.base_dir = base_dir
        self.note_data: Dict[str, Any] = {}
        self._buffer: List[str] = []
        # Collecting started (it starts from a <summary> tag)
        self._collecting_started: bool = False
        # Number of tags skipped before collection started
        self._skipped_before: int = 0
        # Should capture tag and its content
        self._capture_tag: bool = False
        # Parser is in a LaTeX block
        self._in_latex: bool = False
        # Number of tags skipped inside a LaTeX block
        self._skipped_in_latex: int = 0
        # Anki LaTeX tags - either inline or block
        self._latex_tags: Optional[Tuple[str, str]] = None
        self._clozes_count: int = 0

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

    def _check_if_latex(
        self, tag: str, attrs: Iterable[Tuple[str, Optional[str]]]
    ) -> Optional[Tuple[str, str]]:
        """Check if tag manifests start of either inline or block LaTeX.

        :param tag: a tag
        :param attrs: attributes
        :returns: inline or block Anki LaTeX tags or None if not a LaTeX block
        """
        class_ = self._get_attr_by_name('class', attrs)
        if tag in ('span', 'figure'):
            if class_ == self.BLOCK_LATEX_CLASS:
                return self.ANKI_BLOCK_LATEX_TAGS
            elif class_ == self.INLINE_LATEX_CLASS:
                return self.ANKI_INLINE_LATEX_TAGS
        return None

    def handle_starttag(
        self, tag: str, attrs: Iterable[Tuple[str, Optional[str]]]
    ) -> None:
        """Handle a start tag.

        :param tag: a tag
        :param attrs: tag attributes
        """
        # Start collecting tags and data from a <summary> tag
        if tag == 'summary':
            self._collecting_started = self._capture_tag = True
            return
        if not self._collecting_started:
            self._skipped_before += 1
            return
        if self._in_latex:
            # Count skipped start tags
            self._skipped_in_latex += 1
            # Capture content of an annotation tag but not the tag itself
            if tag == 'annotation':
                self._capture_tag = True
            return
        if tag not in self.ALLOWED_TAGS or self._in_latex:
            self._capture_tag = False
            return
        else:
            # If LaTeX wrapper encountered - add corresponding Anki tag
            if (latex_tags := self._check_if_latex(tag, attrs)) is not None:
                self._latex_tags = latex_tags
                self._buffer.append(latex_tags[0])
                self._capture_tag, self._in_latex = False, True
            # Else add tag as is
            else:
                self._capture_tag = True
                attrs_and_values = ' '.join(
                    f'{attr}="{value}"'
                    for attr, value in attrs
                    if attr not in self.SKIP_ATTRIBUTES
                )
                if attrs_and_values:
                    self._buffer.append(f'<{tag} {attrs_and_values}>')
                else:
                    self._buffer.append(f'<{tag}>')
            # In addition, track images to upload them to Anki deck
            if tag == 'img':
                src = self._get_attr_by_name('src', attrs)
                assert src  # mypy
                prefix = ''.join(c for c in src if c.isalnum())
                abs_path = self.base_dir / unquote(src)
                image = AnkiImage(
                    src=src,
                    filename=f'{prefix}_{abs_path.name}',
                    abs_path=abs_path,
                    data=abs_path.read_bytes(),
                )
                self.note_data.setdefault('images', []).append(image)

    def handle_data(self, data: str) -> None:
        """Handle data.

        :param data: data
        """
        if not self._capture_tag:
            return
        # Check if data string should be considered as tags.  Tags can be added
        # only once
        if (
            data.startswith('#')
            and 'tags' not in self.note_data
            and not self._in_latex
        ):
            self.note_data['tags'] = [
                tag.strip() for tag in data.split('#') if tag.strip()
            ]
            return
        # Add data as is
        else:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        """Handle end tag.

        :param tag: tag
        """
        # For LaTeX block decrement skipped tags count
        if self._in_latex:
            if not self._skipped_in_latex:
                self._in_latex, self._capture_tag = False, True
                return
            else:
                self._skipped_in_latex -= 1
        # Skip tags which are not allowed
        if not self._capture_tag:
            return
        # <summary> marks the end of a front side
        if tag == 'summary':
            # Check for clozes
            for i, tag_or_data in enumerate(self._buffer):
                if tag_or_data == '<code>':
                    self._clozes_count += 1
                    self._buffer[i] = f'{{{{c{self._clozes_count}::'
                elif tag_or_data == '</code>':
                    self._buffer[i] = '}}'
            self.note_data['front'] = ''.join(self._buffer)
            self._buffer.clear()
            return
        # <annotation> marks the end of a LaTeX section
        elif tag == 'annotation' and self._in_latex:
            assert self._latex_tags  # mypy
            self._buffer.append(self._latex_tags[1])
            # Skip following closing tags
            self._capture_tag = False
        # Add tag as is
        else:
            self._buffer.append(f'</{tag}>')

    def get_data(self) -> Optional[AnkiNote]:
        """Collect the rest of the data.

        :returns: Note model
        """
        # Cloze notes do not have a back side
        if self._clozes_count > 0:
            back = None
        # Compose a back side of a note
        else:
            # Pop accidental trailing new lines
            while self._buffer[-1] in '\n\r':
                self._buffer.pop()
            # Remove end tags that were skipped before collecting started
            buffer = self._buffer[: -self._skipped_before]
            back = ''.join(buffer)
            # Rewrite images src
            for image in self.note_data.get('images', []):
                prefix = ''.join(c for c in image.src if c.isalnum())
                image.filename = f'{prefix}_{image.abs_path.name}'
                back = back.replace(image.src, image.filename)
            # Remove empty paragraphs
            back = self.EMPTY_P_RE.sub('', back)
            # Remove empty classes
            back = back.replace('class=""', '').replace('<p >', '<p>')
        self.note_data['back'] = back
        try:
            note = AnkiNote(**self.note_data)
        except TypeError as exc:
            self.logger.error('Parsing error', exc_info=exc)
        else:
            return note
        return None

    @classmethod
    def extract_note(
        cls, html: str, base_dir: Path, debug: bool = False
    ) -> Optional[AnkiNote]:
        """Extract Note from HTML fragment.

        :param html: HTML
        :param base_dir: directory of HTML file (for construct absolute path
            of images)
        :param debug: debug mode
        :returns: note object
        """
        parser = cls(base_dir, debug)
        parser.feed(html)
        note = parser.get_data()
        return note


def extract_notes_data(
    source: Path, notion_namespace: str, debug: bool = False
) -> List[AnkiNote]:
    """Extract notes data from HTML source.

    :param source: HTML path
    :param notion_namespace: Notion namespace (to form `source` fields)
    :param debug: debug mode
    :return: notes
    """
    notes = []
    html_doc = source.read_text(encoding='utf8')
    soup = BeautifulSoup(html_doc, 'html.parser')
    article = soup.find_all('article')[0]
    article_id = article['id'].replace('-', '')
    for note_node in soup.find_all('ul', 'toggle'):
        note = NoteDataExtractor.extract_note(
            html=str(note_node), base_dir=source.parent, debug=debug
        )
        if note:
            notion_url = f'https://notion.so/{notion_namespace}/{article_id}'
            note.source = f'<a href="{notion_url}">{notion_url}</a>'
            notes.append(note)
    return notes


if __name__ == '__main__':
    import sys
    from pprint import pprint

    source = sys.argv[1]
    notes = extract_notes_data(Path(source), 'test')
    pprint(notes)
