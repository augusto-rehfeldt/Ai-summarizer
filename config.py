# -*- coding: utf-8 -*-
"""
Configuration widget for Gemini Book Summarizer plugin.
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

# Plugin prefs stored in: calibre/plugins/gemini_summarizer.json
prefs = JSONConfig('plugins/gemini_summarizer')

prefs.defaults['api_key']       = ''
prefs.defaults['model']         = 'gemini-3-flash-preview'
prefs.defaults['custom_column'] = '#summary'
prefs.defaults['max_words']     = 2000
prefs.defaults['max_input_words'] = 500000
prefs.defaults['prompt']        = (
    "Please provide a comprehensive summary of the following book text. "
    "The summary should capture the main themes, key plot points or arguments, "
    "important characters or concepts, and the overall significance of the work. "
    "DO NOT ADD ANYTHING TO THE OUTPUT TEXT BUT THE SUMMARY. NO COMMENTS, NO INTRODUCTORY EXPLANATIONS, etc."
    "Keep the summary under {max_words} words.\n\n"
    "Book: {title} by {authors}\n\n"
    "Text sample:\n{text}"
)

AVAILABLE_MODELS = [
    'gemini-3-flash-preview',  # default
    'gemini-3.1-pro-preview',
    'gemini-3-pro',
    'gemini-2.5-flash',
    'gemini-2.5-pro',
    'gemini-2.0-flash',
    'gemini-2.0-flash-lite',
    'gemini-1.5-flash',
    'gemini-1.5-pro',
]


class ConfigWidget(QWidget):

    def __init__(self):
        QWidget.__init__(self)
        self.l = QVBoxLayout()
        self.setLayout(self.l)
        self.setWindowTitle('Gemini Book Summarizer Configuration')

        # --- API Key ---
        api_group = QGroupBox('Gemini API Settings')
        api_layout = QVBoxLayout()
        api_group.setLayout(api_layout)

        key_layout = QHBoxLayout()
        key_layout.addWidget(QLabel('API Key:'))
        self.api_key_edit = QLineEdit(self)
        try:
            self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        except AttributeError:
            self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setText(prefs['api_key'])
        self.api_key_edit.setPlaceholderText('Enter your Gemini API key...')
        key_layout.addWidget(self.api_key_edit)
        self.show_key_btn = QPushButton('Show')
        self.show_key_btn.setFixedWidth(50)
        self.show_key_btn.clicked.connect(self.toggle_key_visibility)
        key_layout.addWidget(self.show_key_btn)
        api_layout.addLayout(key_layout)

        model_layout = QHBoxLayout()
        model_layout.addWidget(QLabel('Model:'))
        self.model_combo = QComboBox(self)
        for m in AVAILABLE_MODELS:
            self.model_combo.addItem(m)
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
        prefs['api_key']       = self.api_key_edit.text().strip()
        prefs['model']         = self.model_combo.currentText()
        prefs['custom_column'] = custom_column
        prefs['max_words']     = self.max_words_spin.value()
        prefs['max_input_words'] = self.max_input_words_spin.value()
        prefs['prompt']        = self.prompt_edit.toPlainText()
