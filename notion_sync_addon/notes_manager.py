"""Notes manager."""
from pathlib import Path
from typing import Set

from anki.notes import Note
from aqt import mw

from .helpers import get_logger
from .parser import AnkiNote


class NotesManager:
    """Notes manager.

    Creates deck and models, adds and removes notes.
    """

    #: Note model stylesheet
    MODEL_CSS: str = (Path(__file__).parent / 'model.css').read_text('utf-8')
    #: Note model name
    MODEL_NAME: str = 'notion-anki-sync'
    #: Cloze note model name
    CLOZE_MODEL_NAME: str = 'notion-anki-sync-cloze'
    #: Card template name
    CARD_TEMPLATE_NAME: str = 'Question-Answer'
    #: Note front side template
    FRONT_TMPL: str = '<div class="front">{{Front}}</div>'
    #: Note back side template
    BACK_TMPL: str = (
        '<div class="front-on-back">{{Front}}</div>'
        '<hr id="back">'
        '<div class="back">{{Back}}</div>'
        '<hr id="source">'
        '<div class="backlink">{{Source}}</div>'
    )
    #: Cloze note front side template
    CLOZE_FRONT_TMPL: str = '<div class="front">{{cloze:Front}}</div>'
    #: Cloze note back side template
    CLOZE_BACK_TMPL: str = (
        '<div class="front">{{cloze:Front}}</div>'
        '<hr id="source">'
        '<div class="backlink">{{Source}}</div>'
    )

    def __init__(self, collection, deck_name: str, debug: bool = False):
        """Init syncer.

        :param collection: Anki collection
        :param deck_name: name of the target deck
        """
        self.logger = get_logger(self.__class__.__name__, debug)
        self.collection = collection
        self.deck_name = deck_name
        self.deck = self.get_deck()
        self.create_models()

    def create_models(self) -> None:
        """Create Question-Answer and Cloze models."""
        model_manager = self.collection.models
        # Create new model if not exists
        model = model_manager.byName(self.MODEL_NAME)
        if not model:
            model = model_manager.new(self.MODEL_NAME)
            # Add fields
            for field_name in ('Front', 'Back', 'Source'):
                field = model_manager.new_field(field_name)
                model_manager.add_field(model, field)
            # Add template
            template = model_manager.new_template(self.CARD_TEMPLATE_NAME)
            model_manager.add_template(model, template)
            self.logger.info('Model created')
        # If exists, update template and CSS
        else:
            template = model['tmpls'][0]
        # Update template
        template['qfmt'] = self.FRONT_TMPL
        template['afmt'] = self.BACK_TMPL
        # Style model
        model['css'] = self.MODEL_CSS
        model_manager.save(model)
        self.logger.info('Model updated')
        # Copy cloze model if not exists
        cloze_model = model_manager.byName(self.CLOZE_MODEL_NAME)
        if not cloze_model:
            std_cloze_model = model_manager.byName('Cloze')
            cloze_model = model_manager.copy(std_cloze_model)
            cloze_model['name'] = self.CLOZE_MODEL_NAME
            # Ensure cloze model fields
            cloze_model['flds'] = [
                model_manager.new_field('Front'),
                model_manager.new_field('Source'),
            ]
            self.logger.info('Cloze model created')
        # Update cloze template
        cloze_template = cloze_model['tmpls'][0]
        cloze_template['qfmt'] = self.CLOZE_FRONT_TMPL
        cloze_template['afmt'] = self.CLOZE_BACK_TMPL
        # Style cloze model
        cloze_model['css'] = self.MODEL_CSS
        model_manager.save(cloze_model)
        self.logger.info('Cloze model updated')

    def _escape_for_query(self, query: str) -> str:
        """Escape special characters in string for use in a query.

        :param query: query
        :returns: escaped query
        """
        escaped_query = query.replace('\\', '\\\\').replace('"', '\\"')
        return escaped_query

    def upsert_note(self, note: AnkiNote) -> int:
        """Create or update note.

        :param note: a note
        :returns:
        """
        # Check if note is a duplicate
        front = self._escape_for_query(note.front)
        query = f'deck:"{self.deck_name}" front:"{front}"'
        self.logger.debug('Searching with a query: %s', query)
        note_ids = self.collection.find_notes(query=query)
        if not note.back:
            model = self.collection.models.byName(self.CLOZE_MODEL_NAME)
        else:
            model = self.collection.models.byName(self.MODEL_NAME)
        # Get an existing note
        if note_ids:
            note_id = note_ids[0]
            anki_note = self.collection.getNote(note_id)
            self.logger.info(
                'Note exists: note_id=%s, front=%s', note_id, note.front
            )
        # Create a new note
        else:
            anki_note = Note(self.collection, model)
            deck_id = self.get_deck()
            self.collection.add_note(anki_note, deck_id)
            note_id = anki_note.id
            self.logger.info(
                'Note created: id=%s, front=%s', note_id, note.front
            )
        anki_note.tags = note.tags
        for field in model['flds']:
            field_name = field['name']
            value = getattr(note, field_name.lower())
            anki_note[field_name] = value
            self.logger.debug('Setting %s="%s"', field_name, value)
        # Upload note media
        media_manager = self.collection.media
        if note.images:
            for image in note.images:
                new_filename = media_manager.write_data(
                    image.filename, image.data
                )
                self.logger.info('Image stored: %s', image.filename)
                if new_filename:
                    self.logger.debug(
                        'Renaming image %s -> %s', image.filename, new_filename
                    )
                    anki_note['Back'] = anki_note['Back'].replace(
                        image.filename, new_filename
                    )
        anki_note.flush()
        return note_id

    def get_deck(self) -> int:
        """Get or create target deck."""
        assert mw  # mypy
        deck_id = mw.col.decks.id(self.deck_name, create=True)
        assert deck_id  # mypy
        return deck_id

    def remove_all_notes_excluding(self, note_ids: Set[int]) -> None:
        """Remove all notes excluding provided.

        :param note_ids: note ids to skip
        """
        existing_note_ids = set(
            self.collection.find_notes(f'deck:"{self.deck_name}"')
        )
        note_ids_to_remove = existing_note_ids - note_ids
        self.logger.info('Removing notes: %s', note_ids_to_remove)
        self.collection.remove_notes(list(note_ids_to_remove))
