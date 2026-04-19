# -*- coding: utf-8 -*-
"""InterfaceAction entrypoint for the AI Book Summarizer plugin."""

import traceback

try:
    from qt.core import QIcon, QPixmap, QMenu, QToolButton
except ImportError:
    from PyQt5.Qt import QIcon, QPixmap, QMenu, QToolButton

from calibre.gui2 import error_dialog, question_dialog
from calibre.gui2.actions import InterfaceAction
try:
    from calibre.gui2 import get_icons as calibre_get_icons
except Exception:
    calibre_get_icons = None

from calibre_plugins.ai_summarizer.config import prefs


class AISummarizerAction(InterfaceAction):

    name = 'AI Book Summarizer'
    action_spec = (
        'AI Summarize',
        'icon.png',
        'Summarize selected books with AI (Gemini, OpenAI, Anthropic, MiniMax) and store in a custom column',
        None
    )
    action_type = 'current'
    popup_type = (
        QToolButton.ToolButtonPopupMode.MenuButtonPopup
        if hasattr(QToolButton, 'ToolButtonPopupMode')
        else QToolButton.MenuButtonPopup
    )

    def genesis(self):
        self._active_jobs = []
        icon = self._load_plugin_icon()
        if icon and not icon.isNull():
            self.qaction.setIcon(icon)

        self.menu = QMenu(self.gui)
        self.qaction.setMenu(self.menu)
        self.create_menu_action(
            self.menu,
            'gemini_summarize_selected',
            'Summarize Selected Books',
            triggered=self.summarize_selected
        )
        self.create_menu_action(
            self.menu,
            'gemini_configure_plugin',
            'Configure Plugin…',
            triggered=self.open_configuration
        )
        self.qaction.triggered.connect(self.summarize_selected)

    def apply_settings(self):
        pass  # Settings are read from prefs on each run

    # ------------------------------------------------------------------
    def summarize_selected(self, checked=False):
        try:
            try:
                book_ids = list(self.gui.library_view.get_selected_ids())
            except Exception:
                book_ids = list(self.gui.current_view().get_selected_ids())
            if not book_ids:
                return error_dialog(
                    self.gui, 'No books selected',
                    'Please select one or more books to summarize.', show=True
                )

            if not prefs['api_key']:
                return error_dialog(
                    self.gui, 'No API Key',
                    'Please set your API key in\n'
                    'Preferences → Plugins → AI Book Summarizer → Customize plugin.',
                    show=True
                )

            custom_col = (prefs['custom_column'] or '').strip()
            if not custom_col.startswith('#'):
                custom_col = '#%s' % custom_col.lstrip('#')
            db = self.gui.current_db.new_api
            if not self._custom_column_exists(db, custom_col):
                return error_dialog(
                    self.gui, 'Custom column not found',
                    f'Column "{custom_col}" does not exist in your library.\n\n'
                    'Create it via: Preferences → Add your own columns\n'
                    '(type must be "Long text / HTML")',
                    show=True
                )

            count = len(book_ids)
            if not question_dialog(
                self.gui, 'Confirm',
                f'Summarize {count} book(s) using AI?\n'
                f'Provider: {prefs["provider"]}\nModel: {prefs["model"]}\nColumn: {custom_col}',
            ):
                return

            from calibre_plugins.ai_summarizer.jobs import SummarizeJob
            job = SummarizeJob(self.gui, book_ids)
            self._active_jobs.append(job)
            job.finished.connect(lambda _=0, j=job: self._drop_job_ref(j))
            job.start()
        except Exception:
            return error_dialog(
                self.gui,
                'AI Summarizer error',
                traceback.format_exc(),
                det_msg=traceback.format_exc(),
                show=True
            )

    def open_configuration(self, checked=False):
        self.interface_action_base_plugin.do_user_config(self.gui)

    def _load_plugin_icon(self):
        icon_candidates = ('icon.png', 'images/icon.png')

        # Try calibre's icon loader first (works across most plugin contexts).
        if calibre_get_icons is not None:
            for candidate in icon_candidates:
                try:
                    icon = calibre_get_icons(candidate, self.name)
                    if icon and not icon.isNull():
                        return icon
                except Exception:
                    pass

        # Fallback to InterfaceAction helper.
        for candidate in icon_candidates:
            try:
                icon = self.get_icons(candidate)
                if icon and not icon.isNull():
                    return icon
            except Exception:
                pass

        # Last resort: load bytes directly from plugin resources.
        try:
            data = self.interface_action_base_plugin.load_resources(list(icon_candidates))
            for candidate in icon_candidates:
                raw = data.get(candidate)
                if raw:
                    pix = QPixmap()
                    if pix.loadFromData(raw):
                        return QIcon(pix)
        except Exception:
            pass
        return None

    def _custom_column_exists(self, db, custom_col):
        metadata = db.field_metadata.custom_field_metadata()
        if custom_col in metadata:
            return True

        lookup = custom_col.lstrip('#')
        for key, val in metadata.items():
            if key.lstrip('#') == lookup:
                return True
            if val.get('label') == lookup:
                return True
        return False

    def _drop_job_ref(self, job):
        try:
            self._active_jobs.remove(job)
        except ValueError:
            pass
