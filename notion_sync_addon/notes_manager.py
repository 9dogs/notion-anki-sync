"""Notes manager."""
from pathlib import Path
from typing import Dict, List, Optional, Set

from anki.models import ModelsDictProxy
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

    @property
    def existing_note_ids(self) -> Set[int]:
        """Existing note ids in the deck."""
        return set(self.collection.find_notes(f'deck:"{self.deck_name}"'))

    def _escape_query(self, query: str) -> str:
        """Escape special characters in string for use in a query.

        :param query: query
        :returns: escaped query
        """
        escaped_query = query.replace('\\', '\\\\').replace('"', '\\"')
        return escaped_query

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

    def get_deck(self) -> int:
        """Get or create target deck."""
        assert mw  # mypy
        deck_id = mw.col.decks.id(self.deck_name, create=True)
        assert deck_id  # mypy
        return deck_id

    def find_note(self, note: AnkiNote) -> Optional[int]:
        """Find note by its front side.

        :param note: note
        :returns: note id if found else None
        """
        front = self._escape_query(note.front)
        query = f'deck:"{self.deck_name}" front:"{front}"'
        self.logger.debug('Searching with a query: %s', query)
        note_ids = self.collection.find_notes(query=query)
        self.logger.debug('Result: %s', note_ids)
        return note_ids[0] if note_ids else None

    def _fill_fields(
        self, target: Note, source: AnkiNote, model: ModelsDictProxy
    ) -> List[Dict[str, str]]:
        """Fill note fields from Anki model instance.

        :param target: target Anki note
        :param source: source Anki note model
        :returns: updated data
        """
        updated_data = []
        for field in model['flds']:
            field_name = field['name']
            new_value = getattr(source, field_name.lower())
            existing_value = target[field_name]
            if existing_value != new_value:
                updated_data.append(
                    {
                        f'{field}_old': existing_value,
                        f'{field}_new': new_value,
                    }
                )
                target[field_name] = new_value
        return updated_data

    def create_note(self, note: AnkiNote) -> int:
        """Create new note.

        :param note: note
        :returns: id of a note created
        """
        # Pick right model
        if not note.back:
            model = self.collection.models.byName(self.CLOZE_MODEL_NAME)
        else:
            model = self.collection.models.byName(self.MODEL_NAME)
        # Create note and add it to the deck
        anki_note = Note(self.collection, model)
        deck_id = self.get_deck()
        self.collection.add_note(anki_note, deck_id)
        self._fill_fields(anki_note, note, model)
        note_id = anki_note.id
        anki_note.flush()
        self.logger.info('Note created: id=%s, front=%s', note_id, note.front)
        return note_id

    def update_note(self, note_id: int, note: AnkiNote) -> bool:
        """Update existing note.

        :param note_id: id of a note to update
        :param note: note
        :returns: if note has been updated
        """
        updated_data = []
        # Pick right model
        if not note.back:
            model = self.collection.models.byName(self.CLOZE_MODEL_NAME)
        else:
            model = self.collection.models.byName(self.MODEL_NAME)
        # Get an existing note
        existing_note = self.collection.getNote(note_id)
        # Ensure note is of right model
        if existing_note.mid != model['id']:
            self.logger.warning(
                'Note type changed: note_id=%s, old=%s, new=%s',
                note_id,
                existing_note.mid,
                model['id'],
            )
            # It's easier to remove the note and create a new one...
            self.collection.remove_notes([note_id])
            self.create_note(note)
            updated_data.append(
                {'model_old': existing_note.mid, 'model_new': model['id']}
            )
        else:
            # Update tags
            if set(existing_note.tags) != set(note.tags):
                updated_data.append(
                    {'tags_old': existing_note.tags, 'tags_new': note.tags}
                )
                existing_note.tags = note.tags
            # Update field values
            updated_data += self._fill_fields(existing_note, note, model)
            # Upload note media
            media_manager = self.collection.media
            if note.images:
                for image in note.images:
                    # Skip if file already exists
                    if media_manager.have(image.filename):
                        continue
                    maybe_new_filename = media_manager.write_data(
                        image.filename, image.data
                    )
                    self.logger.info('Image stored: %s', image.filename)
                    if maybe_new_filename:
                        self.logger.debug(
                            'Renaming image %s -> %s',
                            image.filename,
                            maybe_new_filename,
                        )
                        existing_note['Back'] = existing_note['Back'].replace(
                            image.filename, maybe_new_filename
                        )
                        updated_data.append(
                            {
                                'filename_old': image.filename,
                                'filename_new': maybe_new_filename,
                            }
                        )
            if updated_data:
                existing_note.flush()
                self.logger.info(
                    'Note updated: note_id=%s, data=%s', note_id, updated_data
                )
            else:
                self.logger.info('No changes in note: note_id=%s', note_id)
        return bool(updated_data)

    def remove_notes(self, note_ids: Set[int]) -> None:
        """Remove notes with given IDs.

        :param note_ids: note ids to remove
        """
        self.logger.info('Removing notes: %s', note_ids)
        self.collection.remove_notes(list(note_ids))
