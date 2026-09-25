# -*- coding: utf-8 -*-
"""
Background job handling for the AI Book Summarizer plugin.
Handles book text extraction, API calls, and saving results.
Providers (base URLs, keys, model lists, response shapes) live in providers.py.
"""

import os
import traceback
import subprocess
import time
import re
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from calibre_plugins.ai_summarizer import providers as P
except ImportError:  # running outside Calibre
    import providers as P


# ─────────────────────────────────────────────
# Worker thread
# ─────────────────────────────────────────────

def _state_dir():
    """Where the shared service keeps its small usage ledgers: Calibre's config folder."""
    try:
        from calibre.utils.config import config_dir
        return os.path.join(config_dir, 'plugins')
    except ImportError:  # outside Calibre (offline checks)
        import tempfile
        return tempfile.gettempdir()


class RetryableAPIError(RuntimeError):
    def __init__(self, message, retry_after_seconds=None, provider=None, rate_limited=True):
        RuntimeError.__init__(self, message)
        self.retry_after_seconds = retry_after_seconds
        self.provider = provider or "unknown"
        # False for an empty reply: no quota to wait out, so skip the 61s floor.
        self.rate_limited = rate_limited


class SummarizerWorker:
    """Summarizes each book; run() blocks, so call it from a Calibre job thread."""
    MAX_RETRIES = 4  # 5 attempts, as book writer's AIService
    EMPTY_RETRY_DELAYS = (3, 8, 20, 45)  # AIService's backoff for empty replies
    # A reasoning model can spend the whole cap thinking and answer nothing.
    TRUNCATED_FINISH_REASONS = {'length', 'max_tokens', 'MAX_TOKENS'}
    MIN_RETRY_DELAY_SECONDS = 61.0
    DEFAULT_RETRY_DELAY_SECONDS = 5.0
    REQUEST_TIMEOUT_SECONDS = 180
    DEFAULT_MAX_BOOK_WORDS = 500_000
    EXTRACTION_CHAR_BUDGET = 2_000_000
    RETRYABLE_HTTP_CODES = {408, 429, 500, 502, 503, 504}
    CONTEXT_THRESHOLD_RATIO = 0.8  # Use 80% of context window
    PROMPT_OVERHEAD_TOKENS = 500  # Rough estimate for system+user prompt overhead

    def __init__(self, db, book_ids, api_key, provider, model, prompt_template, max_words, max_input_words,
                 abort, progress, book_done, book_error, batch_size=1, base_url='', model_context=0):
        self.abort           = abort       # threading.Event, set when the job is stopped
        self.progress        = progress    # (current_index, message)
        self.book_done       = book_done   # (book_id, summary_text)
        self.book_error      = book_error  # (book_id, error_message)
        self.db              = db
        self.book_ids        = book_ids
        self.api_key         = api_key
        self.provider        = str(provider)
        self.provider_label  = P.label(self.provider)
        self.base_url        = base_url or ''
        self.model_context   = int(model_context or 0)
        self.model           = model
        self.prompt_template = prompt_template
        self.max_words       = max_words
        self.max_input_words = int(max_input_words or self.DEFAULT_MAX_BOOK_WORDS)
        self.batch_size      = max(1, min(batch_size, 20))

    def run(self):
        try:
            total = len(self.book_ids)
            batch_size = self.batch_size

            self.progress(0, f'Pre-extracting text for {total} books...')

            extracted_books = []
            for idx, book_id in enumerate(self.book_ids):
                if self.abort.is_set():
                    break
                try:
                    mi = self.db.get_metadata(book_id)
                    title   = mi.title or 'Unknown Title'
                    authors = ', '.join(mi.authors) if mi.authors else 'Unknown Author'

                    content, details = self._extract_book_text(
                        book_id,
                        title,
                        max_words=self.max_input_words,
                        char_budget=self.EXTRACTION_CHAR_BUDGET,
                    )
                    extracted_books.append({
                        'idx': idx,
                        'book_id': book_id,
                        'title': title,
                        'authors': authors,
                        'content': content,
                        'details': details,
                    })
                except Exception as e:
                    self.book_error(book_id, traceback.format_exc())

            if self.abort.is_set():
                return

            completed = 0
            total_books = len(extracted_books)

            if batch_size > 1 and total_books > 1:
                self.progress(0, f'Processing {total_books} books with {batch_size} concurrent workers...')

                def process_book(book_data):
                    result = self._summarize_book(book_data)
                    return book_data['book_id'], result

                with ThreadPoolExecutor(max_workers=batch_size) as executor:
                    futures = {executor.submit(process_book, b): b for b in extracted_books}
                    for future in as_completed(futures):
                        if self.abort.is_set():
                            executor.shutdown(wait=False, cancel_futures=True)
                            break
                        try:
                            book_id, result = future.result()
                            completed += 1
                            if result['success']:
                                self.book_done(book_id, result['summary'])
                            else:
                                self.book_error(book_id, result['error'])
                        except Exception as e:
                            book_id = futures[future]['book_id']
                            self.book_error(book_id, traceback.format_exc())
            else:
                for book_data in extracted_books:
                    if self.abort.is_set():
                        break
                    completed += 1
                    result = self._summarize_book(book_data)
                    book_id = result['book_id']
                    if result['success']:
                        self.book_done(book_id, result['summary'])
                    else:
                        self.book_error(book_id, result['error'])

        except Exception as e:
            self.book_error(-1, f'Fatal error: {traceback.format_exc()}')

    def _summarize_book(self, book_data):
        idx = book_data['idx']
        book_id = book_data['book_id']
        title = book_data['title']
        authors = book_data['authors']
        content = book_data['content']
        details = book_data['details']
        total = len(self.book_ids)

        self.progress(idx, f'[{idx+1}/{total}] {title}')
        self.progress(idx, '  Stage: Extracting text')
        available_formats = details.get('formats') or []
        self.progress(idx, f'    - Available formats: {", ".join(available_formats) if available_formats else "none"}')
        self.progress(idx, f'    - Chosen format: {details.get("chosen_fmt") or "unknown"}')
        if details.get('path'):
            self.progress(idx, f'    - Source path: {details["path"]}')
        if details.get('extractor'):
            self.progress(idx, f'    - Extractor: {details["extractor"]}')

        if not content:
            if details.get('error'):
                self.progress(idx, f'    - Extraction detail: {details["error"]}')
            return {'success': False, 'error': 'Could not extract text from book (no supported format found).', 'book_id': book_id}

        self.progress(idx, f'    - Extracted text: {details.get("word_count", 0)} words, {len(content)} chars')
        if details.get('truncated'):
            self.progress(idx, f'    - Extraction was truncated at {details.get("max_words", self.max_input_words)} words')

        self.progress(idx, f'  Stage: Calling {self.provider_label} API')

        try:
            split_info = self._check_context_split_needed(content)
            if split_info:
                self.progress(idx, f'    - Large text detected ({split_info["total_chunks"]} chunks), using two-phase summarization')
                chunk_summaries = []
                for i, (chunk_text, chunk_idx, chunk_total) in enumerate(split_info['chunks']):
                    self.progress(idx, f'      Chunk {chunk_idx}/{chunk_total}: {len(chunk_text.split())} words')
                    chunk_prompt = self.prompt_template.format(
                        title=title,
                        authors=authors,
                        text=chunk_text,
                        max_words=self.max_words
                    )
                    chunk_summary, api_meta = self._call_api_with_retries(chunk_prompt, idx)
                    chunk_summaries.append(chunk_summary)
                    self.progress(idx, f'      Chunk {chunk_idx} summary: {len(chunk_summary)} chars')

                self.progress(idx, f'    - Synthesizing {len(chunk_summaries)} chunk summaries into final summary')
                combined_chunks = '\n\n'.join(chunk_summaries)
                synthesis_prompt = (
                    f"You have summaries of a book in parts. Combine them into a single coherent summary.\n\n"
                    f"Title: {title}\n"
                    f"Author: {authors}\n\n"
                    f"Part summaries:\n{combined_chunks}\n\n"
                    f"Provide a unified summary in approximately {self.max_words} words:"
                )
                summary, api_meta = self._call_api_with_retries(synthesis_prompt, idx)
                self.progress(idx, f'    - Final synthesized summary: {len(summary)} chars')
            else:
                prompt = self.prompt_template.format(
                    title=title,
                    authors=authors,
                    text=content,
                    max_words=self.max_words
                )
                self.progress(idx, f'    - Model: {self.model}')
                self.progress(idx, f'    - Prompt size: {len(prompt.split())} words, {len(prompt)} chars')
                summary, api_meta = self._call_api_with_retries(prompt, idx)

            self.progress(idx, f'    - API response received')
            if api_meta.get('finish_reason'):
                self.progress(idx, f'    - Finish reason: {api_meta["finish_reason"]}')
            self.progress(idx, f'    - Summary characters: {len(summary)}')
            if not summary:
                return {'success': False, 'error': f'{self.provider_label} returned an empty response.', 'book_id': book_id}
            return {'success': True, 'summary': summary, 'book_id': book_id}
        except Exception as e:
            return {'success': False, 'error': traceback.format_exc(), 'book_id': book_id}

    # ─── helpers ─────────────────────────────

    def _check_context_split_needed(self, text):
        """Check if text needs to be split due to context window limits.

        Returns None if no splitting needed, or a dict with 'chunks' list if splitting needed.
        """
        max_context = P.context_window(self.model, self.model_context, provider=self.provider)
        effective_limit = int(max_context * self.CONTEXT_THRESHOLD_RATIO) - self.PROMPT_OVERHEAD_TOKENS

        # Convert text to approximate tokens (rough estimate: 1 word ~= 1.5 tokens)
        text_tokens = len(text.split()) * 1.5

        if text_tokens <= effective_limit:
            return None

        # Need to split - calculate number of chunks
        words = text.split()
        words_per_chunk = int(effective_limit / 1.5)  # Reverse the token estimate

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

        return {'chunks': chunks, 'total_chunks': total_chunks, 'max_context': max_context, 'effective_limit': effective_limit}

    def _call_api_with_retries(self, prompt, idx):
        total_attempts = self.MAX_RETRIES + 1
        # Anthropic needs an explicit cap; 2 tokens per requested word, floor 4096.
        max_tokens = max(4096, int(self.max_words) * 2)
        attempt = 1
        while True:
            try:
                if attempt > 1:
                    self.progress(idx, f'    - Retry attempt: {attempt}/{total_attempts}')
                summary, api_meta = self._call_api(prompt, max_tokens)
                if summary:
                    return summary, api_meta
                finish_reason = api_meta.get('finish_reason')
                if finish_reason in self.TRUNCATED_FINISH_REASONS:
                    max_tokens *= 2
                    self.progress(idx, f'    - Empty reply hit the token cap; raising it to {max_tokens}')
                raise RetryableAPIError(
                    f'{self.provider_label} returned an empty response (finish_reason={finish_reason})',
                    retry_after_seconds=self.EMPTY_RETRY_DELAYS[min(attempt, len(self.EMPTY_RETRY_DELAYS)) - 1],
                    provider=self.provider,
                    rate_limited=False,
                )
            except RetryableAPIError as e:
                if attempt > self.MAX_RETRIES:
                    raise RuntimeError(
                        f'{self.provider_label} request still failing after {self.MAX_RETRIES} retries: {e}'
                    )

                wait_seconds = e.retry_after_seconds
                if wait_seconds is None:
                    wait_seconds = self.DEFAULT_RETRY_DELAY_SECONDS * attempt
                if e.rate_limited:
                    wait_seconds = max(self.MIN_RETRY_DELAY_SECONDS, float(wait_seconds))
                self.progress(
                    idx,
                    f'    - Retryable error: {e}. Waiting {wait_seconds:.1f}s before retry {attempt + 1}/{total_attempts}.'
                )
                if not self._sleep_with_cancel(wait_seconds):
                    raise RuntimeError('Cancelled while waiting to retry API request.')

                attempt += 1

    def _sleep_with_cancel(self, seconds):
        end = time.time() + max(0.0, float(seconds))
        while time.time() < end:
            if self.abort.is_set():
                return False
            remaining = end - time.time()
            time.sleep(min(0.5, max(0.0, remaining)))
        return not self.abort.is_set()

    def _call_api(self, prompt, max_tokens):
        """One completion through book writer's shared AIService, as (text, meta).

        A retry decision stays here, where cancellation and the progress log live:
        the service makes a single fail-fast attempt and its errors are translated
        into RetryableAPIError (rate limits, overload, timeouts) or a final error.
        """
        module = P.ai_service_module()
        cli = P.spec(self.provider)['style'] == 'cli'
        if P.spec(self.provider).get('proxy_start'):
            P.ensure_openai_oauth_proxy()  # the plugin's GUI-safe starter; the service then finds it up
        name = self.provider_label
        try:
            service = P.shared_service(self.provider, self.model, self.api_key, self.base_url, _state_dir())
            text = service.generate_content(
                prompt, model=self.model, max_completion_tokens=max_tokens, max_retries=1,
                wait_for_limits=False, system=P.CLI_SYSTEM if cli else None)
        except module.EmptyGenerationError:
            return '', {'finish_reason': None}
        except module.IncompleteGenerationError:
            return '', {'finish_reason': 'length'}  # the retry loop doubles the cap
        except Exception as e:  # noqa: BLE001 - translated below, never swallowed
            status = getattr(e, 'status_code', None) or getattr(getattr(e, 'response', None), 'status_code', None)
            if status in self.RETRYABLE_HTTP_CODES:
                raise RetryableAPIError(f'{name} HTTP {status}', provider=self.provider)
            timed_out = isinstance(e, (TimeoutError, socket.timeout)) or (
                status is None and 'timed out' in str(e).lower())
            if timed_out:
                raise RetryableAPIError(f'{name} request timed out: {e}', provider=self.provider)
            # A subscription quota notice (status None) will not reset in minutes of retrying.
            raise RuntimeError(f'{name} request failed: {e}')
        return P.clean_text(text or ''), {'finish_reason': 'stop'}

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
# Calibre job
# ─────────────────────────────────────────────

def summarize_books(db, book_ids, column, worker_kwargs, notifications=None, abort=None, log=None):
    """ThreadedJob body. The full log lives in the job's details (Jobs → Show details);
    any problem is collected and raised at the end, so Calibre flags the job once,
    the way it flags a failed conversion, instead of interrupting per book."""
    total = len(book_ids)
    issues = []
    handled = [0]

    def title_of(book_id):
        try:
            return db.field_for('title', book_id)
        except Exception:
            return f'book_id={book_id}'

    def step(msg):
        handled[0] += 1
        notifications.put((min(1.0, handled[0] / total), msg))

    def on_done(book_id, summary):
        title = title_of(book_id)
        try:
            db.set_field(column, {book_id: summary})
            log(f'✓ Summary saved for: {title}')
        except Exception as e:
            # Fallback: append to comments, and flag it -- the column was not written.
            try:
                comments = db.field_for('comments', book_id) or ''
                db.set_field('comments', {book_id: comments + f'\n\n--- AI Summary ---\n{summary}'})
                issues.append(f'{title}: saved to comments instead ({column} error: {e})')
            except Exception as e2:
                issues.append(f'{title}: summary not saved ({e2})')
            log.error(f'✗ {issues[-1]}')
        step(title)

    def on_error(book_id, error):
        title = 'Fatal error' if book_id == -1 else title_of(book_id)
        log.error(f'✗ Error for "{title}":\n{error}')
        lines = (error or '').strip().splitlines()
        issues.append(f'{title}: {lines[-1] if lines else "unknown error"}')  # a traceback's last line
        step(title)

    SummarizerWorker(db, book_ids, abort=abort, progress=lambda idx, msg: log(msg),
                     book_done=on_done, book_error=on_error, **worker_kwargs).run()
    if abort.is_set():
        log('Cancelled.')
    if issues:
        raise RuntimeError(f'{len(issues)} of {total} book(s) had problems:\n\n' + '\n'.join(issues))


def start_job(gui, book_ids):
    """Queue the summaries as a Calibre job (bottom-right Jobs spinner), not a window."""
    from calibre.gui2 import Dispatcher, error_dialog
    from calibre.gui2.threaded_jobs import ThreadedJob
    from calibre_plugins.ai_summarizer.config import prefs

    provider = prefs['provider']
    worker_kwargs = dict(
        api_key         = P.resolve_key(provider, prefs.get('api_keys', {}) or {}),
        provider        = provider,
        model           = prefs['model'],
        prompt_template = prefs['prompt'],
        max_words       = prefs['max_words'],
        max_input_words = prefs['max_input_words'],
        batch_size      = prefs['batch_size'],
        base_url        = (prefs.get('base_urls', {}) or {}).get(provider, ''),
        model_context   = prefs.get('model_context', 0),
    )

    def done(job):
        # Refresh Calibre's book list
        try:
            gui.iactions['Edit Metadata'].refresh_books_after_metadata_edit(set(book_ids))
        except Exception:
            try:
                gui.current_view().model().refresh()
            except Exception:
                pass
        if job.failed:
            return error_dialog(gui, 'AI Book Summarizer', str(job.exception),
                                det_msg=job.details, show=True)
        gui.status_bar.show_message(f'AI summaries saved for {len(book_ids)} book(s)', 5000)

    job = ThreadedJob(
        'ai_summarizer', f'AI summarize {len(book_ids)} book(s) with {P.label(provider)}',
        summarize_books, (gui.current_db.new_api, book_ids, prefs['custom_column'], worker_kwargs), {},
        Dispatcher(done))
    gui.job_manager.run_threaded_job(job)
    gui.status_bar.show_message('AI summarize job started', 3000)
