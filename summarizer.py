# -*- coding: utf-8 -*-
"""
Thin wrapper around the google-genai SDK.
This module is imported at runtime so that missing dependencies produce
a clear error message rather than crashing Calibre on startup.
"""


class GeminiSummarizer:
    """Calls the Gemini API to generate a book summary."""

    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model   = model
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            from google import genai as google_genai
        except ImportError:
            raise ImportError(
                'The "google-genai" package is not installed.\n\n'
                'Install it by running this command in a terminal:\n\n'
                '    pip install -q -U google-genai\n\n'
                'Then restart Calibre.'
            )
        self._client = google_genai.Client(api_key=self.api_key)
        return self._client

    def summarize(self, prompt: str) -> str:
        """Send prompt to Gemini and return the response text."""
        client = self._get_client()
        response = client.models.generate_content(
            model=self.model,
            contents=prompt,
        )
        return response.text.strip()
