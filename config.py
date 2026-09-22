# -*- coding: utf-8 -*-
"""
Configuration widget for AI Book Summarizer plugin.
"""

try:
    from qt.core import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                         QLineEdit, QPushButton, QComboBox, QGroupBox,
                         QTextEdit, QSpinBox, QSizePolicy, QApplication, Qt,
                         QTimer)
except ImportError:
    from PyQt5.Qt import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                          QLineEdit, QPushButton, QComboBox, QGroupBox,
                          QTextEdit, QSpinBox, QSizePolicy, QApplication, Qt,
                          QTimer)

from calibre.utils.config import JSONConfig

try:
    from calibre_plugins.ai_summarizer import providers as P
except ImportError:  # running outside Calibre
    import providers as P

# Plugin prefs stored in: calibre/plugins/ai_summarizer.json
prefs = JSONConfig('plugins/ai_summarizer')

prefs.defaults['api_keys'] = {name: '' for name in P.PROVIDERS}
prefs.defaults['base_urls'] = {}
prefs.defaults['provider'] = 'hyper'
prefs.defaults['model'] = P.PROVIDERS['hyper']['default_model']
prefs.defaults['model_context'] = 0
prefs.defaults['custom_column'] = '#summary'
prefs.defaults['max_words']     = 2000
prefs.defaults['max_input_words'] = 500000
prefs.defaults['batch_size']    = 1
prefs.defaults['prompt']        = (
    "You are a book summary generator. Write ONLY the summary — no preamble, "
    "no explanation of what you are doing, no meta-comments, no \"Here is the summary:\", "
    "no \"This text describes\", nothing but the summary itself.\n\n"
    "Title: {title}\n"
    "Author: {authors}\n\n"
    "Write a {max_words}-word summary of the following book text. "
    "Cover: main themes, key plot points or arguments, important characters or concepts, overall structure.\n\n"
    "TEXT:\n{text}\n\n"
    "SUMMARY:"
)


class ConfigWidget(QWidget):

    def __init__(self):
        QWidget.__init__(self)
        self.l = QVBoxLayout()
        self.setLayout(self.l)
        self.setWindowTitle('AI Book Summarizer Configuration')
        self.resize(1030, 700)
        self.setMinimumSize(680, 600)

        self._api_keys = dict(prefs.get('api_keys', {}) or {})
        self._base_urls = dict(prefs.get('base_urls', {}) or {})
        self._current_provider = prefs['provider']
        # model id -> context window, filled by Fetch models
        self._model_contexts = {}

        # --- API Settings ---
        api_group = QGroupBox('AI API Settings')
        api_layout = QVBoxLayout()
        api_group.setLayout(api_layout)

        # Provider dropdown
        provider_layout = QHBoxLayout()
        provider_layout.addWidget(QLabel('Provider:'))
        self.provider_combo = QComboBox(self)
        for prov_id, cfg in P.PROVIDERS.items():
            self.provider_combo.addItem(cfg['label'], prov_id)
        idx = self.provider_combo.findData(prefs['provider'])
        if idx >= 0:
            self.provider_combo.setCurrentIndex(idx)
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        provider_layout.addWidget(self.provider_combo)
        provider_layout.addStretch()
        api_layout.addLayout(provider_layout)

        # API Key (per-provider)
        key_layout = QHBoxLayout()
        key_layout.addWidget(QLabel('API Key:'))
        self.api_key_edit = QLineEdit(self)
        try:
            self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        except AttributeError:
            self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.api_key_edit.setMaximumHeight(28)
        self.api_key_edit.setText(self._api_keys.get(self._current_provider, ''))
        key_layout.addWidget(self.api_key_edit)
        self.show_key_btn = QPushButton('Show')
        self.show_key_btn.setFixedWidth(50)
        self.show_key_btn.clicked.connect(self.toggle_key_visibility)
        key_layout.addWidget(self.show_key_btn)
        self.check_key_btn = QPushButton('Check')
        self.check_key_btn.setFixedWidth(50)
        self.check_key_btn.clicked.connect(self.check_api_key)
        key_layout.addWidget(self.check_key_btn)
        api_layout.addLayout(key_layout)

        self.key_hint = QLabel('')
        self.key_hint.setWordWrap(True)
        api_layout.addWidget(self.key_hint)

        # Base URL (per-provider), so any OpenAI-compatible gateway works
        url_layout = QHBoxLayout()
        url_layout.addWidget(QLabel('Base URL:'))
        self.base_url_edit = QLineEdit(self)
        self.base_url_edit.setText(self._base_urls.get(self._current_provider, ''))
        url_layout.addWidget(self.base_url_edit)
        api_layout.addLayout(url_layout)

        # Model dropdown (editable: gateways add models faster than this list)
        model_layout = QHBoxLayout()
        model_layout.addWidget(QLabel('Model:'))
        self.model_combo = QComboBox(self)
        self.model_combo.setEditable(True)
        try:
            self.model_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        except Exception:
            pass
        self.model_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._populate_models(self._current_provider)
        self.model_combo.setEditText(prefs['model'])
        self.model_combo.currentTextChanged.connect(self._on_model_changed)
        model_layout.addWidget(self.model_combo)
        self.fetch_btn = QPushButton('Fetch models')
        self.fetch_btn.clicked.connect(self.fetch_models)
        model_layout.addWidget(self.fetch_btn)
        api_layout.addLayout(model_layout)

        ctx_layout = QHBoxLayout()
        ctx_layout.addWidget(QLabel('Model context (tokens, 0 = auto):'))
        self.model_context_spin = QSpinBox(self)
        self.model_context_spin.setRange(0, 10_000_000)
        self.model_context_spin.setSingleStep(1000)
        self.model_context_spin.setValue(int(prefs.get('model_context', 0) or 0))
        ctx_layout.addWidget(self.model_context_spin)
        ctx_layout.addWidget(QLabel('(drives when a long book is split into chunks)'))
        ctx_layout.addStretch()
        api_layout.addLayout(ctx_layout)

        self._update_provider_hints()
        self.l.addWidget(api_group)

        # --- Column Settings ---
        col_group = QGroupBox('Calibre Column Settings')
        col_layout = QVBoxLayout()
        col_group.setLayout(col_layout)

        col_row = QHBoxLayout()
        col_row.addWidget(QLabel('Custom Column (e.g. #summary):'))
        self.col_edit = QLineEdit(self)
        self.col_edit.setText(prefs['custom_column'])
        self.col_edit.setPlaceholderText('#summary')
        col_row.addWidget(self.col_edit)
        col_layout.addLayout(col_row)

        words_row = QHBoxLayout()
        words_row.addWidget(QLabel('Max summary words:'))
        self.max_words_spin = QSpinBox(self)
        self.max_words_spin.setRange(100, 5000)
        self.max_words_spin.setSingleStep(100)
        self.max_words_spin.setValue(prefs['max_words'])
        words_row.addWidget(self.max_words_spin)
        words_row.addStretch()
        col_layout.addLayout(words_row)

        input_words_row = QHBoxLayout()
        input_words_row.addWidget(QLabel('Max input words:'))
        self.max_input_words_spin = QSpinBox(self)
        self.max_input_words_spin.setRange(10000, 2000000)
        self.max_input_words_spin.setSingleStep(10000)
        self.max_input_words_spin.setValue(prefs['max_input_words'])
        input_words_row.addWidget(self.max_input_words_spin)
        input_words_row.addStretch()
        col_layout.addLayout(input_words_row)

        batch_row = QHBoxLayout()
        batch_row.addWidget(QLabel('Concurrent API requests:'))
        self.batch_size_spin = QSpinBox(self)
        self.batch_size_spin.setRange(1, 20)
        self.batch_size_spin.setValue(prefs['batch_size'])
        batch_row.addWidget(self.batch_size_spin)
        batch_row.addWidget(QLabel('(1=sequential, up to 20=parallel)'))
        batch_row.addStretch()
        col_layout.addLayout(batch_row)

        col_layout.addWidget(QLabel(
            '<small>Create the custom column in Calibre first:<br>'
            'Preferences → Add your own columns → Add column<br>'
            'Column id: <b>summary</b> → stored as <b>#summary</b> | Type: Long text / HTML</small>'
        ))

        self.l.addWidget(col_group)

        # --- Prompt ---
        prompt_group = QGroupBox('Summary Prompt Template')
        prompt_layout = QVBoxLayout()
        prompt_group.setLayout(prompt_layout)
        prompt_layout.addWidget(QLabel(
            'Available placeholders: {title}, {authors}, {max_words}, {text}'
        ))
        self.prompt_edit = QTextEdit(self)
        self.prompt_edit.setPlainText(prefs['prompt'])
        self.prompt_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.prompt_edit.setMinimumHeight(120)
        self.prompt_edit.setMaximumHeight(160)
        prompt_layout.addWidget(self.prompt_edit)

        reset_btn = QPushButton('Reset to Default Prompt')
        reset_btn.clicked.connect(self.reset_prompt)
        prompt_layout.addWidget(reset_btn)

        self.l.addWidget(prompt_group)
        self.l.addStretch()

        # The saved provider's live list arrives once the dialog is painted.
        QTimer.singleShot(0, lambda: self.fetch_models(auto=True))

    # ─── provider/model plumbing ─────────────

    def _populate_models(self, provider, models=None):
        """Fill the model dropdown, keeping whatever the user typed."""
        current = self.model_combo.currentText().strip()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        if models is None:
            models = P._row_models(P.spec(provider))
        self._model_contexts = {mid: ctx for mid, ctx in models if ctx}
        for model_id, _ctx in models:
            self.model_combo.addItem(model_id)
        self.model_combo.setEditText(current if current else P.spec(provider)['default_model'])
        self.model_combo.blockSignals(False)

    def _on_provider_changed(self, index):
        """Save the current provider's key/URL, then load the new one's."""
        self._api_keys[self._current_provider] = self.api_key_edit.text().strip()
        self._base_urls[self._current_provider] = self.base_url_edit.text().strip()
        new_provider = self.provider_combo.itemData(index)
        self._current_provider = new_provider
        self.api_key_edit.setText(self._api_keys.get(new_provider, ''))
        self.base_url_edit.setText(self._base_urls.get(new_provider, ''))
        self.model_combo.setEditText(P.spec(new_provider)['default_model'])
        self._populate_models(new_provider)
        self.model_context_spin.setValue(0)
        self._update_provider_hints()
        self.fetch_models(auto=True)

    def _on_model_changed(self, text):
        """A model fetched with a known context window fills the spinbox for you."""
        ctx = self._model_contexts.get(text.strip())
        if ctx:
            self.model_context_spin.setValue(int(ctx))

    def _update_provider_hints(self):
        provider = self._current_provider
        cfg = P.spec(provider)
        self.api_key_edit.setPlaceholderText('Enter your %s API key…' % cfg['label'])
        self.base_url_edit.setPlaceholderText(cfg['base_url'])
        keys = dict(self._api_keys)
        keys[provider] = self.api_key_edit.text().strip()
        source = P.key_source(provider, keys)
        envs = ' / '.join('$%s' % e for e in cfg.get('key_envs') or [])
        if not P.needs_key(provider):
            self.key_hint.setText(
                '<small>No API key needed — this provider runs on a local CLI or '
                'proxy you are already logged into.</small>'
            )
        elif source and source != 'this field':
            self.key_hint.setText(
                '<small>Leave empty to use the key found in <b>%s</b>. '
                'Otherwise set %s.</small>' % (source, envs)
            )
        elif source == 'this field':
            self.key_hint.setText('<small>Using the key typed above.</small>')
        else:
            self.key_hint.setText(
                '<small>No key found automatically — paste one above or set %s.</small>' % envs
            )

    def fetch_models(self, auto=False):
        """Ask the gateway which models it serves right now.

        auto=True is the quiet version that runs on dialog load and provider
        selection: it skips anything that would only hang the dialog or
        surprise the user (CLI rows have no endpoint, starting the
        openai-oauth proxy can open a browser sign-in, and without a key the
        gateway can only repeat the static list anyway).
        """
        provider = self._current_provider
        cfg = P.spec(provider)
        keys = dict(self._api_keys)
        keys[provider] = self.api_key_edit.text().strip()
        key = P.resolve_key(provider, keys)
        if auto:
            if cfg['style'] == 'cli':
                return
            if cfg.get('proxy_start') and not P.openai_oauth_proxy_running():
                self.key_hint.setText(
                    '<small>Press "Fetch models" to start the openai-oauth '
                    'proxy (sign-in may be needed) and list its models.</small>')
                return
            if not key and cfg.get('needs_key', True):
                return
        base_url = self.base_url_edit.text().strip()
        if not self._ensure_proxy():
            return
        self.fetch_btn.setEnabled(False)
        self.fetch_btn.setText('Fetching…')
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
        except Exception:
            pass
        try:
            models = P.list_models(provider, key, base_url)
            self._populate_models(provider, models)
            self.key_hint.setText('<small>%d models listed by %s.</small>'
                                  % (len(models), P.label(provider)))
        finally:
            try:
                QApplication.restoreOverrideCursor()
            except Exception:
                pass
            self.fetch_btn.setText('Fetch models')
            self.fetch_btn.setEnabled(True)

    def _ensure_proxy(self):
        """openai-oauth needs its local proxy up before any request works."""
        if not P.spec(self._current_provider).get('proxy_start'):
            return True
        self.key_hint.setText('<small>Starting the openai-oauth proxy '
                              '(browser sign-in if needed)…</small>')
        try:
            QApplication.processEvents()  # paint the hint before the blocking call
            P.ensure_openai_oauth_proxy()
        except (RuntimeError, OSError) as e:
            # OSError too: the npx shim can vanish between which() and run(),
            # and an uncaught exception in a Qt slot aborts Calibre.
            self.key_hint.setText('<small style="color:#b00020"><b>%s</b></small>' % e)
            return False
        return True

    def check_api_key(self):
        """Ask the gateway whether the key works, and say so in the hint."""
        provider = self._current_provider
        keys = dict(self._api_keys)
        keys[provider] = self.api_key_edit.text().strip()
        key = P.resolve_key(provider, keys)
        if not self._ensure_proxy():
            return
        ok, msg = P.validate_key(provider, key, self.base_url_edit.text().strip())
        if ok:
            self.key_hint.setText('<small><b>%s</b></small>' % msg)
        else:
            self.key_hint.setText('<small style="color:#b00020"><b>%s</b></small>' % msg)

    def toggle_key_visibility(self):
        try:
            is_password = self.api_key_edit.echoMode() in (QLineEdit.EchoMode.Password, QLineEdit.Password)
        except AttributeError:
            is_password = self.api_key_edit.echoMode() == QLineEdit.Password

        if is_password:
            self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Normal if hasattr(QLineEdit, 'EchoMode') else QLineEdit.Normal)
            self.show_key_btn.setText('Hide')
        else:
            try:
                self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
            except AttributeError:
                self.api_key_edit.setEchoMode(QLineEdit.Password)
            self.show_key_btn.setText('Show')

    def reset_prompt(self):
        self.prompt_edit.setPlainText(prefs.defaults['prompt'])

    def save_settings(self):
        custom_column = self.col_edit.text().strip()
        if custom_column and not custom_column.startswith('#'):
            custom_column = '#%s' % custom_column.lstrip('#')
        current_provider = self.provider_combo.itemData(self.provider_combo.currentIndex())
        self._api_keys[current_provider] = self.api_key_edit.text().strip()
        self._base_urls[current_provider] = self.base_url_edit.text().strip()
        prefs['api_keys'] = self._api_keys
        prefs['base_urls'] = self._base_urls
        prefs['provider'] = current_provider
        prefs['model'] = self.model_combo.currentText().strip()
        prefs['model_context'] = self.model_context_spin.value()
        prefs['custom_column'] = custom_column
        prefs['max_words'] = self.max_words_spin.value()
        prefs['max_input_words'] = self.max_input_words_spin.value()
        prefs['batch_size'] = self.batch_size_spin.value()
        prefs['prompt'] = self.prompt_edit.toPlainText()
