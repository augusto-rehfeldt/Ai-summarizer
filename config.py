# -*- coding: utf-8 -*-
"""
Configuration widget for AI Book Summarizer plugin.
"""

try:
    from qt.core import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                         QLineEdit, QPushButton, QComboBox, QGroupBox,
                         QTextEdit, QSpinBox)
except ImportError:
    from PyQt5.Qt import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                          QLineEdit, QPushButton, QComboBox, QGroupBox,
                          QTextEdit, QSpinBox)

from calibre.utils.config import JSONConfig

# Plugin prefs stored in: calibre/plugins/ai_summarizer.json
prefs = JSONConfig('plugins/ai_summarizer')

prefs.defaults['api_keys'] = {'gemini': '', 'openai': '', 'anthropic': '', 'minimax': ''}
prefs.defaults['provider'] = 'gemini'
prefs.defaults['model']         = 'gemini-3.1-pro'
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

PROVIDER_MODELS = {
    'gemini': [
        'gemini-3-flash-preview',
        'gemini-3.1-pro',
    ],
    'openai': [
        'gpt-5.4',
        'gpt-5.4-mini',
    ],
    'anthropic': [
        'claude-opus-4-7',
        'claude-sonnet-4-6',
        'claude-haiku-4-5',
    ],
    'minimax': [
        'MiniMax-M2.7',
    ],
}

PROVIDER_DISPLAY_NAMES = {
    'gemini': 'Google Gemini',
    'openai': 'OpenAI',
    'anthropic': 'Anthropic Claude',
    'minimax': 'MiniMax',
}


class ConfigWidget(QWidget):

    def __init__(self):
        QWidget.__init__(self)
        self.l = QVBoxLayout()
        self.setLayout(self.l)
        self.setWindowTitle('AI Book Summarizer Configuration')

        # --- API Settings ---
        api_group = QGroupBox('AI API Settings')
        api_layout = QVBoxLayout()
        api_group.setLayout(api_layout)

        # Provider dropdown
        provider_layout = QHBoxLayout()
        provider_layout.addWidget(QLabel('Provider:'))
        self.provider_combo = QComboBox(self)
        for prov_id, prov_name in PROVIDER_DISPLAY_NAMES.items():
            self.provider_combo.addItem(prov_name, prov_id)
        idx = self.provider_combo.findData(prefs['provider'])
        if idx >= 0:
            self.provider_combo.setCurrentIndex(idx)
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        provider_layout.addWidget(self.provider_combo)
        provider_layout.addStretch()
        api_layout.addLayout(provider_layout)

        self._current_provider = prefs['provider']

        # API Key (per-provider)
        key_layout = QHBoxLayout()
        key_layout.addWidget(QLabel('API Key:'))
        self.api_key_edit = QLineEdit(self)
        try:
            self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        except AttributeError:
            self.api_key_edit.setEchoMode(QLineEdit.Password)
        self._api_keys = dict(prefs.get('api_keys', {}) or {})
        current_provider = prefs['provider']
        self.api_key_edit.setText(self._api_keys.get(current_provider, ''))
        self._update_api_key_placeholder()
        key_layout.addWidget(self.api_key_edit)
        self.show_key_btn = QPushButton('Show')
        self.show_key_btn.setFixedWidth(50)
        self.show_key_btn.clicked.connect(self.toggle_key_visibility)
        key_layout.addWidget(self.show_key_btn)
        api_layout.addLayout(key_layout)

        # Model dropdown
        model_layout = QHBoxLayout()
        model_layout.addWidget(QLabel('Model:'))
        self.model_combo = QComboBox(self)
        self._populate_models(prefs['provider'])
        idx = self.model_combo.findText(prefs['model'])
        if idx >= 0:
            self.model_combo.setCurrentIndex(idx)
        model_layout.addWidget(self.model_combo)
        model_layout.addStretch()
        api_layout.addLayout(model_layout)

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
        self.prompt_edit.setMinimumHeight(120)
        prompt_layout.addWidget(self.prompt_edit)

        reset_btn = QPushButton('Reset to Default Prompt')
        reset_btn.clicked.connect(self.reset_prompt)
        prompt_layout.addWidget(reset_btn)

        self.l.addWidget(prompt_group)
        self.l.addStretch()

    def _populate_models(self, provider):
        """Populate the model dropdown based on selected provider."""
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        models = PROVIDER_MODELS.get(provider, PROVIDER_MODELS['gemini'])
        for m in models:
            self.model_combo.addItem(m)
        self.model_combo.blockSignals(False)

    def _on_provider_changed(self, index):
        """Handle provider change - update model dropdown, save current key, load new provider's key."""
        self._api_keys[self._current_provider] = self.api_key_edit.text().strip()
        new_provider = self.provider_combo.itemData(index)
        self._current_provider = new_provider
        self._populate_models(new_provider)
        self._update_api_key_placeholder()
        self.api_key_edit.setText(self._api_keys.get(new_provider, ''))

    def _update_api_key_placeholder(self):
        """Update API key placeholder text based on selected provider."""
        provider = self.provider_combo.itemData(self.provider_combo.currentIndex())
        placeholders = {
            'gemini': 'Enter your Gemini API key...',
            'openai': 'Enter your OpenAI API key...',
            'anthropic': 'Enter your Anthropic API key...',
            'minimax': 'Enter your MiniMax API key...',
        }
        self.api_key_edit.setPlaceholderText(placeholders.get(provider, 'Enter your API key...'))

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
        prefs['api_keys'] = self._api_keys
        prefs['provider'] = current_provider
        prefs['model'] = self.model_combo.currentText()
        prefs['custom_column'] = custom_column
        prefs['max_words'] = self.max_words_spin.value()
        prefs['max_input_words'] = self.max_input_words_spin.value()
        prefs['batch_size'] = self.batch_size_spin.value()
        prefs['prompt'] = self.prompt_edit.toPlainText()
