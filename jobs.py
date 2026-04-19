# -*- coding: utf-8 -*-
"""
Background job handling for the AI Book Summarizer plugin.
Handles book text extraction, API calls, and saving results.
Supports multiple AI providers: Gemini, OpenAI, Anthropic, MiniMax.
"""

import os
import traceback
import json
import subprocess
import time
import re
import socket
from enum import Enum
from urllib import request as urlrequest
from urllib import error as urlerror

try:
    from openai import OpenAI as _OpenAIClient
    _HAS_OPENAI_CLIENT = True
except ImportError:
    _OpenAIClient = None
    _HAS_OPENAI_CLIENT = False

try:
    from qt.core import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                          QPushButton, QProgressBar, QTextEdit,
                          QThread, pyqtSignal)
except ImportError:
    from PyQt5.Qt import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                           QPushButton, QProgressBar, QTextEdit,
                           QThread, pyqtSignal)


# ─────────────────────────────────────────────
# Provider and model definitions
# ─────────────────────────────────────────────

class Provider(Enum):
    GEMINI = "gemini"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    MINIMAX = "minimax"


# Context windows in tokens for known models
MODEL_CONTEXT_WINDOWS = {
    # OpenAI
    "gpt-5.4": 256000,
    "gpt-5.4-mini": 256000,
    # Anthropic
    "claude-opus-4.7": 200000,
    "claude-sonnet-4.6": 200000,
    "claude-haiku-4.5": 200000,
    # MiniMax
    "MiniMax-M2.7": 204800,
    # Gemini
    "gemini-3.1-flash": 1048576,
    "gemini-3.1-pro": 1048576,
}

PROVIDER_MODELS = {
    Provider.GEMINI: [
        "gemini-3.1-flash",
        "gemini-3.1-pro",
    ],
    Provider.OPENAI: [
        "gpt-5.4",
        "gpt-5.4-mini",
    ],
    Provider.ANTHROPIC: [
        "claude-opus-4.7",
        "claude-sonnet-4.6",
        "claude-haiku-4.5",
    ],
    Provider.MINIMAX: [
        "MiniMax-M2.7",
    ],
}


# ─────────────────────────────────────────────
# Worker thread
# ─────────────────────────────────────────────

class RetryableAPIError(RuntimeError):
    def __init__(self, message, retry_after_seconds=None, provider=None):
        RuntimeError.__init__(self, message)
        self.retry_after_seconds = retry_after_seconds
        self.provider = provider or "unknown"


class SummarizerWorker(QThread):
    """Worker thread that calls AI APIs for each book."""
    MAX_RETRIES = 3
    MIN_RETRY_DELAY_SECONDS = 61.0
    DEFAULT_RETRY_DELAY_SECONDS = 5.0
    REQUEST_TIMEOUT_SECONDS = 180
    DEFAULT_MAX_BOOK_WORDS = 500_000
    EXTRACTION_CHAR_BUDGET = 2_000_000
    RETRYABLE_HTTP_CODES = {408, 429, 500, 502, 503, 504}
    CONTEXT_THRESHOLD_RATIO = 0.8  # Use 80% of context window
    PROMPT_OVERHEAD_TOKENS = 500  # Rough estimate for system+user prompt overhead

    progress   = pyqtSignal(int, str)   # (current_index, message)
    book_done  = pyqtSignal(int, str)   # (book_id, summary_text)
    book_error = pyqtSignal(int, str)   # (book_id, error_message)
    finished   = pyqtSignal()

    def __init__(self, db, book_ids, api_key, provider, model, prompt_template, max_words, max_input_words):
        QThread.__init__(self)
        self.db              = db
        self.book_ids        = book_ids
        self.api_key         = api_key
        self.provider        = Provider(provider) if isinstance(provider, str) else provider
        self.model           = model
        self.prompt_template = prompt_template
        self.max_words       = max_words
        self.max_input_words = int(max_input_words or self.DEFAULT_MAX_BOOK_WORDS)
        self._cancelled      = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            total = len(self.book_ids)
            for idx, book_id in enumerate(self.book_ids):
                if self._cancelled:
                    break

                try:
                    mi = self.db.get_metadata(book_id)
                    title   = mi.title or 'Unknown Title'
                    authors = ', '.join(mi.authors) if mi.authors else 'Unknown Author'

                    self.progress.emit(idx, '')
                    self.progress.emit(idx, f'[{idx+1}/{total}] {title}')
                    self.progress.emit(idx, '  Stage: Extracting text')
                    content, details = self._extract_book_text(
                        book_id,
                        title,
                        max_words=self.max_input_words,
                        char_budget=self.EXTRACTION_CHAR_BUDGET,
                    )
                    available_formats = details.get('formats') or []
                    self.progress.emit(idx, f'    - Available formats: {", ".join(available_formats) if available_formats else "none"}')
                    self.progress.emit(idx, f'    - Chosen format: {details.get("chosen_fmt") or "unknown"}')
                    if details.get('path'):
                        self.progress.emit(idx, f'    - Source path: {details["path"]}')
                    if details.get('extractor'):
                        self.progress.emit(idx, f'    - Extractor: {details["extractor"]}')

                    if not content:
                        if details.get('error'):
                            self.progress.emit(idx, f'    - Extraction detail: {details["error"]}')
                        self.book_error.emit(book_id, 'Could not extract text from book (no supported format found).')
                        continue
                    self.progress.emit(
                        idx,
                        f'    - Extracted text: {details.get("word_count", 0)} words, {len(content)} chars'
                    )
                    if details.get('truncated'):
                        self.progress.emit(
                            idx,
                            f'    - Extraction was truncated at {details.get("max_words", self.max_input_words)} words'
                        )

                    self.progress.emit(idx, f'  Stage: Calling {self.provider.value.title()} API')

                    # Check if text needs splitting due to context window
                    split_info = self._check_context_split_needed(content)
                    if split_info:
                        self.progress.emit(idx, f'    - Large text detected ({split_info["total_chunks"]} chunks), using two-phase summarization')
                        chunk_summaries = []
                        for i, (chunk_text, chunk_idx, chunk_total) in enumerate(split_info['chunks']):
                            self.progress.emit(idx, f'      Chunk {chunk_idx}/{chunk_total}: {len(chunk_text.split())} words')
                            chunk_prompt = self.prompt_template.format(
                                title=title,
                                authors=authors,
                                text=chunk_text,
                                max_words=self.max_words
                            )
                            chunk_summary, api_meta = self._call_api_with_retries(chunk_prompt, idx)
                            chunk_summaries.append(chunk_summary)
                            self.progress.emit(idx, f'      Chunk {chunk_idx} summary: {len(chunk_summary)} chars')

                        # Phase 2: Synthesize all chunk summaries into a single final summary
                        self.progress.emit(idx, f'    - Synthesizing {len(chunk_summaries)} chunk summaries into final summary')
                        combined_chunks = '\n\n'.join(chunk_summaries)
                        synthesis_prompt = (
                            f"You have summaries of a book in parts. Combine them into a single coherent summary.\n\n"
                            f"Title: {title}\n"
                            f"Author: {authors}\n\n"
                            f"Part summaries:\n{combined_chunks}\n\n"
                            f"Provide a unified summary in approximately {self.max_words} words:"
                        )
                        summary, api_meta = self._call_api_with_retries(synthesis_prompt, idx)
                        self.progress.emit(idx, f'    - Final synthesized summary: {len(summary)} chars')
                    else:
                        prompt = self.prompt_template.format(
                            title=title,
                            authors=authors,
                            text=content,
                            max_words=self.max_words
                        )
                        self.progress.emit(idx, f'    - Model: {self.model}')
                        self.progress.emit(
                            idx,
                            f'    - Prompt size: {len(prompt.split())} words, {len(prompt)} chars'
                        )
                        summary, api_meta = self._call_api_with_retries(prompt, idx)

                    self.progress.emit(idx, f'    - API response received')
                    if api_meta.get('finish_reason'):
                        self.progress.emit(idx, f'    - Finish reason: {api_meta["finish_reason"]}')
                    self.progress.emit(idx, f'    - Summary characters: {len(summary)}')
                    if not summary:
                        raise ValueError(f'{self.provider.value.title()} returned an empty response.')
                    self.book_done.emit(book_id, summary)

                except Exception as e:
                    self.book_error.emit(book_id, traceback.format_exc())

        except Exception as e:
            self.book_error.emit(-1, f'Fatal error: {traceback.format_exc()}')
        finally:
            self.finished.emit()

    # ─── helpers ─────────────────────────────

    def _check_context_split_needed(self, text):
        """Check if text needs to be split due to context window limits.

        Returns None if no splitting needed, or a dict with 'chunks' list if splitting needed.
        """
        max_context = MODEL_CONTEXT_WINDOWS.get(self.model, 100000)
        effective_limit = int(max_context * self.CONTEXT_THRESHOLD_RATIO) - self.PROMPT_OVERHEAD_TOKENS

        # Convert text to approximate tokens (rough estimate: 1 word ~= 1.3 tokens)
        text_tokens = len(text.split()) * 1.3

        if text_tokens <= effective_limit:
            return None

        # Need to split - calculate number of chunks
        words = text.split()
        words_per_chunk = int(effective_limit / 1.3)  # Reverse the token estimate

        # Ensure we have a reasonable chunk size
        if words_per_chunk < 1000:
            words_per_chunk = 1000  # Minimum chunk size

        chunks = []
        chunk_idx = 0
        total_chunks = (len(words) + words_per_chunk - 1) // words_per_chunk

        for i in range(0, len(words), words_per_chunk):
            chunk_idx += 1
            chunk_words = words[i:i + words_per_chunk]
            chunk_text = ' '.join(chunk_words)
            chunks.append((chunk_text, chunk_idx, total_chunks))

        return {'chunks': chunks, 'max_context': max_context, 'effective_limit': effective_limit}

    def _call_api_with_retries(self, prompt, idx):
        total_attempts = self.MAX_RETRIES + 1
        attempt = 1
        while True:
            try:
                if attempt > 1:
                    self.progress.emit(idx, f'    - Retry attempt: {attempt}/{total_attempts}')
                return self._call_api(prompt)
            except RetryableAPIError as e:
                if attempt > self.MAX_RETRIES:
                    raise RuntimeError(
                        f'{self.provider.value.title()} request still failing after {self.MAX_RETRIES} retries: {e}'
                    )

                wait_seconds = e.retry_after_seconds
                if wait_seconds is None:
                    wait_seconds = self.DEFAULT_RETRY_DELAY_SECONDS * attempt
                wait_seconds = max(self.MIN_RETRY_DELAY_SECONDS, float(wait_seconds))
                self.progress.emit(
                    idx,
                    f'    - Retryable error: {e}. Waiting {wait_seconds:.1f}s before retry {attempt + 1}/{total_attempts}.'
                )
                if not self._sleep_with_cancel(wait_seconds):
                    raise RuntimeError('Cancelled while waiting to retry API request.')

                attempt += 1

    def _sleep_with_cancel(self, seconds):
        end = time.time() + max(0.0, float(seconds))
        while time.time() < end:
            if self._cancelled:
                return False
            remaining = end - time.time()
            time.sleep(min(0.5, max(0.0, remaining)))
        return not self._cancelled

    def _parse_retry_delay_seconds(self, error_payload):
        details = (error_payload or {}).get('details') or []
        for detail in details:
            retry_delay = (detail or {}).get('retryDelay')
            if not retry_delay:
                continue
            match = re.match(r'^\s*(\d+(?:\.\d+)?)s\s*$', str(retry_delay))
            if match:
                try:
                    return float(match.group(1))
                except Exception:
                    return None
        return None

    def _parse_retry_after_header_seconds(self, headers):
        if not headers:
            return None
        retry_after = headers.get('Retry-After')
        if not retry_after:
            return None
        retry_after = str(retry_after).strip()
        if retry_after.isdigit():
            try:
                return float(retry_after)
            except Exception:
                return None
        return None

    def _call_api(self, prompt):
        """Call AI provider REST API based on self.provider."""
        if self.provider == Provider.GEMINI:
            return self._call_gemini(prompt)
        elif self.provider == Provider.OPENAI:
            return self._call_openai(prompt)
        elif self.provider == Provider.ANTHROPIC:
            return self._call_anthropic(prompt)
        elif self.provider == Provider.MINIMAX:
            return self._call_minimax(prompt)
        else:
            raise RuntimeError(f'Unknown provider: {self.provider}')

    def _build_request(self, url, payload, headers, method='POST'):
        """Build and return a urllib Request object."""
        data = json.dumps(payload).encode('utf-8')
        return urlrequest.Request(
            url,
            data=data,
            headers=headers,
            method=method,
        )

    def _call_openai(self, prompt):
        """Call OpenAI chat completions API."""
        endpoint = 'https://api.openai.com/v1/chat/completions'
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.api_key}',
        }
        payload = {
            'model': self.model,
            'messages': [{'role': 'user', 'content': prompt}],
        }
        return self._do_api_request(endpoint, payload, headers, Provider.OPENAI)

    def _call_anthropic(self, prompt):
        """Call Anthropic messages API."""
        endpoint = 'https://api.anthropic.com/v1/messages'
        headers = {
            'Content-Type': 'application/json',
            'x-api-key': self.api_key,
            'anthropic-version': '2023-06-01',
        }
        payload = {
            'model': self.model,
            'messages': [{'role': 'user', 'content': prompt}],
            'max_tokens': 2048,
        }
        return self._do_api_request(endpoint, payload, headers, Provider.ANTHROPIC)

    def _call_minimax(self, prompt):
        """Call MiniMax API using OpenAI-compatible endpoint."""
        if _HAS_OPENAI_CLIENT:
            try:
                client = _OpenAIClient(
                    api_key=self.api_key,
                    base_url='https://api.minimax.io/v1'
                )
                resp = client.chat.completions.create(
                    model=self.model,
                    messages=[{'role': 'user', 'content': prompt}],
                    extra_body={'reasoning_split': True},
                    timeout=self.REQUEST_TIMEOUT_SECONDS,
                )
                # reasoning_split=True puts thinking in reasoning_details, clean text in content
                msg = resp.choices[0].message
                reasoning = getattr(msg, 'reasoning_details', None) or []
                thinking_text = ''.join(
                    r.get('text', '') for r in reasoning
                    if isinstance(r, dict)
                ) if isinstance(reasoning, list) else ''
                content = getattr(msg, 'content', '') or ''
                # Strip thinking blocks that may still appear in content
                content = re.sub(r'<thinking>.*?</thinking>', '', content, flags=re.DOTALL)
                content = re.sub(r'<think>.*?', '', content, flags=re.DOTALL)
                text = content.strip()
                meta = {'choices': len(resp.choices), 'finish_reason': resp.choices[0].finish_reason}
                if thinking_text:
                    meta['thinking_chars'] = len(thinking_text)
                return text, meta
            except Exception as e:
                # Fall through to HTTP fallback
                print(f'[MiniMax] OpenAI client failed: {e}, falling back to HTTP')

        # HTTP fallback
        endpoint = 'https://api.minimax.io/v1/chat/completions'
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.api_key}',
        }
        payload = {
            'model': self.model,
            'messages': [{'role': 'user', 'content': prompt}],
        }
        return self._do_api_request(endpoint, payload, headers, Provider.MINIMAX)

    def _call_gemini(self, prompt):
        """Call Gemini REST API without external SDK dependencies."""
        safe_endpoint = (
            'https://generativelanguage.googleapis.com/v1beta/models/'
            f'{self.model}:generateContent'
        )
        endpoint = (
            'https://generativelanguage.googleapis.com/v1beta/models/'
            f'{self.model}:generateContent?key={self.api_key}'
        )
        payload = {
            'contents': [{'parts': [{'text': prompt}]}]
        }
        headers = {'Content-Type': 'application/json'}
        return self._do_api_request(endpoint, payload, headers, Provider.GEMINI, safe_endpoint=safe_endpoint)

    def _do_api_request(self, endpoint, payload, headers, provider, safe_endpoint=None):
        """Execute API request and parse response. Provider-specific parsing happens in _parse_response."""
        safe_endpoint = safe_endpoint or endpoint
        req = self._build_request(endpoint, payload, headers)

        try:
            with urlrequest.urlopen(req, timeout=self.REQUEST_TIMEOUT_SECONDS) as resp:
                raw = resp.read().decode('utf-8', errors='replace')
        except urlerror.HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')
            if e.code in self.RETRYABLE_HTTP_CODES:
                retry_after = self._parse_retry_after_header_seconds(getattr(e, 'headers', None))
                parsed = None
                try:
                    parsed = json.loads(body)
                except Exception:
                    parsed = None
                retry_delay = None
                if retry_after is None and parsed:
                    retry_delay = self._parse_retry_delay_seconds(parsed.get('error') or {})
                raise RetryableAPIError(
                    f'{provider.value.title()} HTTP {e.code}',
                    retry_after_seconds=retry_after or retry_delay,
                    provider=provider.value,
                )
            raise RuntimeError(f'{provider.value.title()} HTTP {e.code} on {safe_endpoint}: {body}')
        except (TimeoutError, socket.timeout) as e:
            raise RetryableAPIError(
                f'{provider.value.title()} request timed out after {self.REQUEST_TIMEOUT_SECONDS}s: {e}',
                provider=provider.value,
            )
        except urlerror.URLError as e:
            reason = str(getattr(e, 'reason', e))
            timeout_like = 'timed out' in reason.lower() or isinstance(getattr(e, 'reason', None), socket.timeout)
            if timeout_like:
                raise RetryableAPIError(
                    f'{provider.value.title()} network timeout: {reason}',
                    provider=provider.value,
                )
            raise RuntimeError(f'{provider.value.title()} request failed on {safe_endpoint}: {reason}')
        except Exception as e:
            raise RuntimeError(f'{provider.value.title()} request failed on {safe_endpoint}: {e}')

        try:
            parsed = json.loads(raw)
        except Exception:
            raise RuntimeError(f'{provider.value.title()} returned non-JSON response.')

        return self._parse_response(parsed, provider)

    def _parse_response(self, parsed, provider):
        """Parse provider-specific response format and extract text."""
        if provider == Provider.OPENAI:
            candidates = parsed.get('choices') or []
            if not candidates:
                raise RuntimeError(f'{provider.value.title()} returned no choices: {parsed}')
            message = candidates[0].get('message') or {}
            text = (message.get('content') or '').strip()
            meta = {'choices': len(candidates), 'finish_reason': candidates[0].get('finish_reason')}
            return text, meta

        elif provider == Provider.MINIMAX:
            candidates = parsed.get('choices') or []
            if not candidates:
                raise RuntimeError(f'{provider.value.title()} returned no choices: {parsed}')
            first = candidates[0]
            msg = first.get('message') or {}
            content = msg.get('content') or ''
            # Handle content as a list of blocks (MiniMax may return [{type, text}])
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get('type') == 'thinking' or 'thinking' in block:
                            continue  # Skip extended thinking blocks
                        if block.get('type') == 'text' and 'text' in block:
                            text_parts.append(block['text'])
                        elif 'text' in block and isinstance(block['text'], str):
                            text_parts.append(block['text'])
                    elif isinstance(block, str):
                        text_parts.append(block)
                text = ''.join(text_parts).strip()
            elif isinstance(content, str):
                # Strip <thinking>...</thinking> and <think>... blocks
                text = re.sub(r'<thinking>.*?</thinking>', '', content, flags=re.DOTALL)
                text = re.sub(r'<think>.*?', '', text, flags=re.DOTALL).strip()
            else:
                text = str(content).strip()
            meta = {'choices': len(candidates), 'finish_reason': first.get('finish_reason')}
            return text, meta

        elif provider == Provider.ANTHROPIC:
            content = parsed.get('content') or []
            text = ''
            for block in content:
                if block.get('type') == 'text':
                    text = (block.get('text') or '').strip()
                    break
            if not text:
                # Try as a list of dicts (some MiniMax responses)
                if isinstance(content, list) and content:
                    text = str(content[0]) if isinstance(content[0], str) else ''
            meta = {'content_blocks': len(content)}
            return text, meta

        elif provider == Provider.GEMINI:
            candidates = parsed.get('candidates') or []
            if not candidates:
                msg = parsed.get('error') or parsed
                raise RuntimeError(f'{provider.value.title()} returned no candidates: {msg}')
            parts = ((candidates[0].get('content') or {}).get('parts')) or []
            text = ''.join((p.get('text') or '') for p in parts).strip()
            meta = {
                'candidates': len(candidates),
                'finish_reason': candidates[0].get('finishReason'),
            }
            return text, meta

        raise RuntimeError(f'Unknown provider for parsing: {provider}')
    def _extract_book_text(self, book_id, title, max_words=120_000, char_budget=2_000_000):
        """
        Try to extract plain text from the book.
        Priority: TXT → EPUB → MOBI/AZW → PDF (first N chars).
        Returns a truncated string or empty string.
        """
        db = self.db

        # Preferred format order
        details = {
            'formats': [],
            'chosen_fmt': None,
            'path': None,
            'extractor': None,
            'error': None,
            'max_words': max_words,
            'char_budget': char_budget,
            'truncated': False,
            'word_count': 0,
            'source_word_count': 0,
        }

        formats = db.formats(book_id)
        if not formats:
            details['error'] = 'No formats found in Calibre metadata.'
            return '', details

        if isinstance(formats, str):
            formats = [f.strip() for f in formats.split(',') if f.strip()]
        else:
            formats = [str(f).strip() for f in formats if str(f).strip()]
        if not formats:
            details['error'] = 'Formats list was empty after parsing.'
            return '', details
        details['formats'] = formats

        format_priority = ['TXT', 'EPUB', 'MOBI', 'AZW3', 'AZW', 'PDF', 'HTML', 'RTF', 'LIT']
        formats_upper   = [f.upper() for f in formats]

        chosen_fmt = None
        for pref in format_priority:
            if pref in formats_upper:
                chosen_fmt = formats[formats_upper.index(pref)]
                break

        if not chosen_fmt:
            chosen_fmt = formats[0]
        details['chosen_fmt'] = chosen_fmt

        path = db.format_abspath(book_id, chosen_fmt)
        details['path'] = path
        if not path or not os.path.exists(path):
            details['error'] = f'Format path missing or not found for {chosen_fmt}.'
            return '', details

        fmt_upper = chosen_fmt.upper()

        try:
            extracted = ''
            if fmt_upper == 'TXT':
                details['extractor'] = 'plain-text reader'
                with open(path, 'r', errors='replace') as f:
                    extracted = f.read(char_budget)

            elif fmt_upper in ('EPUB',):
                details['extractor'] = 'EPUB HTML parser'
                extracted = self._extract_epub(path, char_budget)

            elif fmt_upper == 'PDF':
                details['extractor'] = 'PDF extractor'
                extracted = self._extract_pdf(path, char_budget)

            elif fmt_upper in ('MOBI', 'AZW3', 'AZW', 'LIT'):
                details['extractor'] = 'ebook-convert fallback'
                extracted = self._extract_mobi(path, char_budget)

            elif fmt_upper == 'HTML':
                details['extractor'] = 'HTML parser'
                extracted = self._extract_html_file(path, char_budget)

            else:
                # Generic: try reading as text
                details['extractor'] = 'generic text reader'
                with open(path, 'r', errors='replace') as f:
                    extracted = f.read(char_budget)

            cleaned = self._clean_extracted_text(extracted)
            final_text, was_truncated, final_words, source_words = self._truncate_to_words(cleaned, max_words)
            details['word_count'] = final_words
            details['source_word_count'] = source_words
            details['truncated'] = was_truncated
            return final_text, details
        except Exception as e:
            details['error'] = str(e)
            return '', details

    def _clean_extracted_text(self, text):
        if not text:
            return ''
        # Normalize converter artifacts so character counts better match real content.
        text = text.replace('\x00', ' ')
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        text = re.sub(r'[ \t\f\v]+', ' ', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r'\u00ad', '', text)  # soft hyphen
        return text.strip()

    def _truncate_to_words(self, text, max_words):
        if not text:
            return '', False, 0, 0
        words = text.split()
        source_words = len(words)
        if source_words <= max_words:
            return text, False, source_words, source_words
        return ' '.join(words[:max_words]), True, max_words, source_words

    def _extract_epub(self, path, max_chars):
        import zipfile
        from html.parser import HTMLParser

        class TextExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self.text = []
                self._skip = False

            def handle_starttag(self, tag, attrs):
                if tag in ('script', 'style'):
                    self._skip = True

            def handle_endtag(self, tag):
                if tag in ('script', 'style'):
                    self._skip = False

            def handle_data(self, data):
                if not self._skip:
                    self.text.append(data)

        result = []
        total  = 0
        try:
            with zipfile.ZipFile(path) as zf:
                names = sorted([n for n in zf.namelist() 
                                 if n.endswith(('.html', '.xhtml', '.htm'))])
                for name in names:
                    if total >= max_chars:
                        break
                    try:
                        data = zf.read(name).decode('utf-8', errors='replace')
                        parser = TextExtractor()
                        parser.feed(data)
                        chunk = ' '.join(parser.text)
                        result.append(chunk)
                        total += len(chunk)
                    except Exception:
                        continue
        except Exception:
            pass
        return ' '.join(result)[:max_chars]

    def _extract_pdf(self, path, max_chars):
        try:
            import pdfminer.high_level as pdfminer
            from io import StringIO
            out = StringIO()
            with open(path, 'rb') as f:
                pdfminer.extract_text_to_fp(f, out, output_type='text')
            return out.getvalue()[:max_chars]
        except ImportError:
            pass
        # Fallback: try calibre's own PDF extraction
        try:
            from calibre.ebooks.pdf.pdftohtml import pdftotext
            return pdftotext(path)[:max_chars]
        except Exception:
            return ''

    def _extract_mobi(self, path, max_chars):
        try:
            import tempfile, os
            # Convert to txt via calibre-debug
            with tempfile.NamedTemporaryFile(suffix='.txt', delete=False) as tmp:
                tmp_path = tmp.name
            kwargs = {
                'capture_output': True,
                'timeout': 120,
            }
            if os.name == 'nt':
                # Avoid flashing a terminal window for each conversion on Windows.
                kwargs['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            subprocess.run(
                ['ebook-convert', path, tmp_path, '--output-profile=default'],
                **kwargs
            )
            if os.path.exists(tmp_path):
                with open(tmp_path, 'r', errors='replace') as f:
                    text = f.read(max_chars)
                os.unlink(tmp_path)
                return text
        except Exception:
            pass
        return ''

    def _extract_html_file(self, path, max_chars):
        from html.parser import HTMLParser

        class TP(HTMLParser):
            def __init__(self):
                super().__init__()
                self.parts = []
            def handle_data(self, data):
                self.parts.append(data)

        with open(path, 'r', errors='replace') as f:
            raw = f.read(max_chars * 3)
        p = TP()
        p.feed(raw)
        return ' '.join(p.parts)[:max_chars]


# ─────────────────────────────────────────────
# Progress dialog
# ─────────────────────────────────────────────

class SummarizeJob(QDialog):
    """Dialog that shows progress and runs the summarization job."""

    def __init__(self, gui, book_ids):
        QDialog.__init__(self, gui)
        self.gui      = gui
        self.book_ids = book_ids
        self.db       = gui.current_db.new_api
        self.worker   = None
        self.failed_books = []

        self.setWindowTitle('AI Book Summarizer')
        self.setMinimumWidth(520)
        self.setMinimumHeight(300)

        layout = QVBoxLayout(self)

        self.status_label = QLabel('Initializing…')
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximum(len(book_ids))
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(140)
        layout.addWidget(self.log)

        btn_row = QHBoxLayout()
        self.cancel_btn = QPushButton('Cancel')
        self.cancel_btn.clicked.connect(self._cancel)
        self.close_btn  = QPushButton('Close')
        self.close_btn.setEnabled(False)
        self.close_btn.clicked.connect(self.accept)
        btn_row.addStretch()
        btn_row.addWidget(self.cancel_btn)
        btn_row.addWidget(self.close_btn)
        layout.addLayout(btn_row)

    def start(self):
        from calibre_plugins.ai_summarizer.config import prefs

        self.show()

        self.worker = SummarizerWorker(
            db              = self.db,
            book_ids        = self.book_ids,
            api_key         = prefs['api_key'],
            provider        = prefs['provider'],
            model           = prefs['model'],
            prompt_template = prefs['prompt'],
            max_words       = prefs['max_words'],
            max_input_words = prefs['max_input_words'],
        )
        self.worker.progress.connect(self._on_progress)
        self.worker.book_done.connect(self._on_book_done)
        self.worker.book_error.connect(self._on_book_error)
        self.worker.finished.connect(self._on_finished)
        self.worker.start()

    def _on_progress(self, idx, msg):
        if msg and msg.strip():
            self.status_label.setText(msg)
        self.progress_bar.setValue(idx)
        self._log(msg)

    def _on_book_done(self, book_id, summary):
        from calibre_plugins.ai_summarizer.config import prefs
        col = prefs['custom_column']
        mi  = self.db.get_metadata(book_id)
        title = mi.title

        try:
            # Write to custom column
            self.db.set_field(col, {book_id: summary})
            msg = f'✓ Summary saved for: {title}'
        except Exception as e:
            msg = f'✗ Saved to comments instead for "{title}" (column error: {e})'
            # Fallback: append to comments
            try:
                old_comments = mi.comments or ''
                new_comments = old_comments + f'\n\n--- AI Summary ---\n{summary}'
                self.db.set_field('comments', {book_id: new_comments})
            except Exception:
                pass

        self._log(msg)
        self.progress_bar.setValue(self.progress_bar.value() + 1)

    def _on_book_error(self, book_id, error):
        if book_id == -1:
            self._log(f'FATAL ERROR:\n{error}')
        else:
            try:
                mi    = self.db.get_metadata(book_id)
                title = mi.title
            except Exception:
                title = f'book_id={book_id}'
            self.failed_books.append(title)
            self._log(f'✗ Error for "{title}":\n{error}')
        self.progress_bar.setValue(self.progress_bar.value() + 1)

    def _on_finished(self):
        if self.failed_books:
            self.status_label.setText(f'Done with errors ({len(self.failed_books)} failed).')
        else:
            self.status_label.setText('Done!')
        self.cancel_btn.setEnabled(False)
        self.close_btn.setEnabled(True)
        self._log('\n─── All done ───')
        if self.failed_books:
            self._log(f'⚠ Failed books: {len(self.failed_books)}')
            for title in self.failed_books:
                self._log(f'  - {title}')
        # Refresh Calibre's book list
        try:
            self.gui.iactions['Edit Metadata'].refresh_books_after_metadata_edit(
                set(self.book_ids)
            )
        except Exception:
            try:
                self.gui.current_view().model().refresh()
            except Exception:
                pass

    def _cancel(self):
        if self.worker:
            self.worker.cancel()
        self.cancel_btn.setEnabled(False)
        self._log('Cancellation requested…')

    def _log(self, msg):
        self.log.append(msg)
        sb = self.log.verticalScrollBar()
        sb.setValue(sb.maximum())
