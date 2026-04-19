# -*- coding: utf-8 -*-
from calibre.customize import InterfaceActionBase

class AISummarizerPlugin(InterfaceActionBase):
    name                    = 'AI Book Summarizer'
    description             = 'Summarizes selected books using AI APIs (Gemini, OpenAI, Anthropic, MiniMax) and saves to a custom column'
    supported_platforms     = ['windows', 'osx', 'linux']
    author                  = 'Calibre Plugin'
    version                 = (2, 0, 0)
    minimum_calibre_version = (5, 0, 0)
    actual_plugin           = 'calibre_plugins.ai_summarizer.action:AISummarizerAction'

    def is_customizable(self):
        return True

    def config_widget(self):
        from calibre_plugins.ai_summarizer.config import ConfigWidget
        return ConfigWidget()

    def save_settings(self, config_widget):
        config_widget.save_settings()
        ac = getattr(self, 'actual_plugin_', None)
        if ac is not None:
            ac.apply_settings()
