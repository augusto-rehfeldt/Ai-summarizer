# -*- coding: utf-8 -*-
"""Offline checks for providers.py — no network, no Calibre, no Qt.

Run: python test_providers.py
"""

import json
import os
import sys
import tempfile
import threading
from pathlib import Path

import providers as P


def test_list_models_survives_a_malformed_body():
    """A reachable-but-junk answer falls back to the static row, never raises —
    an uncaught exception here aborts Calibre from inside a Qt slot."""
    saved_urlopen = P.urlrequest.urlopen

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    Resp.read = lambda self: b'null'
    P.urlrequest.urlopen = lambda req, timeout: Resp()
    try:
        assert [m for m, _ in P.list_models('hyper', 'K')] == P.spec('hyper')['models']

        Resp.read = lambda self: (b'{"data": [{"id": "weird", "context_length": "200k"},'
                                  b' {"id": "good", "context_length": 123}]}')
        models = dict(P.list_models('hyper', 'K'))
        assert models.get('weird') == 0 and models.get('good') == 123, models
    finally:
        P.urlrequest.urlopen = saved_urlopen


def test_validate_key():
    """Check button plumbing: CLI rows need no key; HTTP auth failures say so."""
    ok, msg = P.validate_key('command-code', '')
    assert ok and 'no key needed' in msg, msg

    ok, msg = P.validate_key('hyper', '')
    assert not ok and 'no API key' in msg, msg

    saved_urlopen = P.urlrequest.urlopen

    def fake_urlopen_401(req, timeout):
        raise P.urlerror.HTTPError(req.full_url if hasattr(req, 'full_url') else 'http://x',
                                   401, 'bad key', hdrs=None, fp=None)

    P.urlrequest.urlopen = fake_urlopen_401
    try:
        ok, msg = P.validate_key('hyper', 'wrong')
        assert not ok and '401' in msg, msg
    finally:
        P.urlrequest.urlopen = saved_urlopen

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"data": [{"id": "m1"}, {"id": "m2"}]}'

    def fake_urlopen_ok(req, timeout):
        return Resp()

    P.urlrequest.urlopen = fake_urlopen_ok
    try:
        ok, msg = P.validate_key('hyper', 'good')
        assert ok and '2' in msg, msg
    finally:
        P.urlrequest.urlopen = saved_urlopen


def test_list_models_merges_static_and_filters_non_chat():
    """A live answer keeps its context data but static entries are not dropped."""
    saved_urlopen = P.urlrequest.urlopen

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return (b'{"data": [{"id": "brand-new-model", "context_length": 999000},'
                    b' {"id": "tts-1"}, {"id": "text-embed-3"}]}')

    P.urlrequest.urlopen = lambda req, timeout: Resp()
    try:
        models = dict(P.list_models('hyper', 'K'))
        assert models.get('brand-new-model') == 999000, models
        assert 'tts-1' not in models and 'text-embed-3' not in models, models
        # static catalogue survives the merge
        assert 'qwen3.8-flash' in models, models
    finally:
        P.urlrequest.urlopen = saved_urlopen


def test_row_contexts_beat_the_flat_table():
    assert P.context_window('claude-sonnet-5') == 200000
    assert P.context_window('claude-sonnet-5', provider='command-code') == 1000000
    assert P.context_window('who-knows', provider='command-code') == P.DEFAULT_CONTEXT_WINDOW
    # The config dialog's static fallback uses this, so its auto-filled
    # spinbox must agree with the row, not the flat table.
    assert dict(P._row_models(P.spec('command-code')))['claude-sonnet-5'] == 1000000


def test_validate_key_needs_key_rows_and_gemini_ua():
    ok, msg = P.validate_key('openai-oauth', '')
    assert ok and 'no key needed' in msg, msg
    # gemini requests keep the browser UA like every other gateway call
    _, headers = P._models_request('gemini', 'K')
    assert headers.get('User-Agent') == P.BROWSER_UA, headers


def test_openai_oauth_proxy_startup():
    """A live proxy is never touched; a dead one is started via npx --detach."""
    saved_which = P.shutil.which
    saved_run = P.subprocess.run
    saved_connect = P.socket.create_connection

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Proc:
        returncode = 0

    state = {'up': True, 'argv': None}

    def fake_connect(*a, **kw):
        if state['up']:
            return FakeConn()
        raise OSError('nothing listening')

    def fake_run(argv, **kwargs):
        state['argv'] = argv
        state['up'] = True
        return Proc()

    P.shutil.which = lambda name: '/fake/%s' % name
    P.socket.create_connection = fake_connect
    try:
        # already listening: no npx call at all
        P.ensure_openai_oauth_proxy()
        assert state['argv'] is None

        # dead port: spawn, then the port poll succeeds
        state['up'] = False
        P.subprocess.run = fake_run
        P.ensure_openai_oauth_proxy()
        assert state['argv'][:4] == ['/fake/npx.cmd', '--yes', 'openai-oauth@latest', '--detach'], state['argv']

        # no npx on PATH: a clear error, not a crash
        P.shutil.which = lambda name: None
        state['up'] = False
        try:
            P.ensure_openai_oauth_proxy()
        except RuntimeError as e:
            assert 'npx' in str(e), e
        else:
            raise AssertionError('missing npx must raise')
    finally:
        P.shutil.which = saved_which
        P.subprocess.run = saved_run
        P.socket.create_connection = saved_connect


def test_every_row_is_complete():
    for name, cfg in P.PROVIDERS.items():
        assert cfg['style'] in ('openai', 'anthropic', 'gemini', 'cli'), name
        assert cfg['default_model'] in cfg['models'], name
        assert cfg['base_url'] or cfg['style'] == 'cli', name


def test_parse_preserves_prose_and_strips_only_explicit_reasoning():
    cases = [
        ('This book describes a family rebuilding after a storm.',
         'This book describes a family rebuilding after a storm.'),
        ('Based on the text, the narrator is unreliable.',
         'Based on the text, the narrator is unreliable.'),
        ('The appendix includes SUMMARY: a record of the voyage.',
         'The appendix includes SUMMARY: a record of the voyage.'),
        (' \n<think>private\nreasoning</think><thinking>more reasoning</thinking>\n'
         ' SUMMARY: A book.', 'A book.'),
    ]
    for raw, expected in cases:
        assert P.clean_text(raw) == expected, (raw, P.clean_text(raw))


def test_key_resolution_order(tmp_home):
    keys = {'hyper': '  from-dialog  '}
    assert P.resolve_key('hyper', keys) == 'from-dialog'
    assert P.key_source('hyper', keys) == 'this field'

    os.environ['AW_API_KEY'] = 'from-env'
    assert P.resolve_key('hyper', {'hyper': ''}) == 'from-env'
    assert P.key_source('hyper', {}) == '$AW_API_KEY'
    del os.environ['AW_API_KEY']

    # crush.json under $LOCALAPPDATA is the last resort for hyper
    crush = tmp_home / 'crush'
    crush.mkdir(parents=True, exist_ok=True)
    (crush / 'crush.json').write_text(
        json.dumps({'providers': {'hyper': {'api_key': 'from-crush'}}}), encoding='utf-8')
    os.environ['LOCALAPPDATA'] = str(tmp_home)
    assert P.resolve_key('hyper', {}) == 'from-crush'
    assert P.key_source('hyper', {}) == "Crush's crush.json"

    # An OAuth access token wins over api_key, and an expired one says so.
    (crush / 'crush.json').write_text(json.dumps({'providers': {'hyper': {
        'api_key': 'stale', 'oauth': {'access_token': 'fresh', 'expires_at': 4102444800}}}}),
        encoding='utf-8')
    assert P.resolve_key('hyper', {}) == 'fresh'
    assert P.key_source('hyper', {}) == "Crush's crush.json"
    (crush / 'crush.json').write_text(json.dumps({'providers': {'hyper': {
        'api_key': 'stale', 'oauth': {'access_token': 'old', 'expires_at': 1}}}}),
        encoding='utf-8')
    assert P.resolve_key('hyper', {}) == 'old'
    assert 'expired' in P.key_source('hyper', {})

    # A provider with no fallback anywhere resolves to empty, never to another provider's key.
    assert P.resolve_key('openrouter', {}) in ('', P.resolve_key('openrouter', {}))


def test_context_window():
    """Windows track the model, not the provider row: openai-oauth serves the
    same gpt-5.6-terra at 1.05M that command-code does, never the 100k default."""
    assert P.context_window('gpt-5.4') == 1050000
    assert P.context_window('gpt-5.6-terra') == 1050000
    assert P.context_window('gpt-5.6-terra', provider='openai-oauth') == 1050000
    assert P.context_window('who-knows') == P.DEFAULT_CONTEXT_WINDOW
    assert P.context_window('gpt-5.4', override=8000) == 8000  # the calibration knob wins


def test_empty_reply_is_retried_with_a_bigger_cap():
    """A reasoning model that spends the cap thinking answers empty with finish_reason=length."""
    import types
    import jobs

    w = object.__new__(jobs.SummarizerWorker)
    w.max_words, w.provider, w.provider_label, w.abort = 500, 'hyper', 'Hyper', threading.Event()
    w.progress = lambda *a: None
    w._sleep_with_cancel = lambda s: True
    caps = []
    def call(prompt, max_tokens):
        caps.append(max_tokens)
        return ('done', {}) if len(caps) == 3 else ('', {'finish_reason': 'length'})
    w._call_api = call
    assert w._call_api_with_retries('p', 0) == ('done', {})
    assert caps == [4096, 8192, 16384], caps

    caps.clear()
    w._call_api = lambda p, m: (caps.append(m), ('', {}))[1]
    try:
        w._call_api_with_retries('p', 0)
    except RuntimeError as e:
        assert 'empty response' in str(e), e
    else:
        raise AssertionError('always-empty replies must fail')
    assert len(caps) == jobs.SummarizerWorker.MAX_RETRIES + 1 == 5, caps


def test_rows_map_onto_the_shared_service():
    """Every completion runs on book writer's AIService, built from the provider row."""
    state = Path(tempfile.gettempdir())
    o = P.service_overrides('hyper', 'qwen3.8-flash', 'K', '', state)
    assert o['provider'] == 'openrouter' and o['base_url'] == 'https://hyper.charm.land/v1', o
    assert o['api_key'] == 'K' and o['headers']['User-Agent'] == P.BROWSER_UA, o
    assert o['token_param'] == 'max_tokens' and o['writing_model'] == 'qwen3.8-flash', o
    # OpenAI's own API 400s on the older spelling.
    assert P.service_overrides('openai', 'gpt-5.4', 'K', '', state)['token_param'] == 'max_completion_tokens'
    # Anthropic and Gemini publish OpenAI-compatible endpoints; the shared client speaks those.
    assert P.service_overrides('anthropic', 'm', 'K', '', state)['base_url'] == 'https://api.anthropic.com/v1'
    gemini = P.service_overrides('gemini', 'gemini-3.5-flash', 'K', '', state)
    assert gemini['base_url'].endswith('/v1beta/openai'), gemini
    # ...and the real shared service calls exactly that (no /v1 appended: that 404s).
    built = P.shared_service('gemini', 'gemini-3.5-flash', 'K', '', state)
    assert built.base_url == 'https://generativelanguage.googleapis.com/v1beta/openai', built.base_url
    # The summary cap is sent as given: an uncapped/raised cap gets a 402 on OpenRouter.
    assert gemini['cap_is_ceiling'] is True, gemini
    # A custom base URL is what makes an unlisted gateway usable.
    assert P.service_overrides('openai', 'm', 'K', 'http://localhost:1234/v1/', state)['base_url'] == 'http://localhost:1234/v1'
    for row, shared in (('claude-cli', 'claude'), ('command-code', 'commandcode')):
        o = P.service_overrides(row, 'sonnet', '', '', state)
        assert o['provider'] == shared and 'api_key' not in o and 'base_url' not in o, o
        assert o['timeout'] == P.CLI_TIMEOUT_SECONDS
    oauth = P.service_overrides('openai-oauth', 'gpt-5.6-terra', '', '', state)
    assert oauth['provider'] == 'openai-oauth' and 'api_key' not in oauth, oauth
    for key in ('groq_rate_state_path', 'usage_state_path'):
        assert str(oauth[key]).startswith(str(state)), oauth  # never inside the plugin zip


def test_shared_module_is_book_writers_and_ships_in_the_zip():
    module = P.ai_service_module()
    assert hasattr(module, 'AIService') and hasattr(module, 'EmptyGenerationError')
    import build
    assert 'ai_service.py' in build.PLUGIN_FILES
    assert build.plugin_file_source('ai_service.py') == P.BOOK_WRITER_AI_SERVICE


def test_the_plugin_sends_no_completion_requests_itself():
    here = Path(__file__).resolve().parent
    jobs_source = (here / 'jobs.py').read_text(encoding='utf-8')
    providers_source = (here / 'providers.py').read_text(encoding='utf-8')
    assert 'urlopen' not in jobs_source and 'chat/completions' not in jobs_source
    for gone in ('def build_request', 'def parse_response', 'def run_cli', '/messages', ':generateContent'):
        assert gone not in providers_source, gone


class _FakeService:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def generate_content(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _worker(provider='hyper'):
    import types
    import jobs
    w = object.__new__(jobs.SummarizerWorker)
    w.provider, w.provider_label, w.model = provider, P.label(provider), 'm'
    w.api_key, w.base_url, w.abort = 'K', '', threading.Event()
    return jobs, w


def test_call_api_runs_on_the_shared_service_and_translates_errors():
    jobs, w = _worker()
    module = P.ai_service_module()
    saved = P.shared_service
    try:
        service = _FakeService('<think>plan</think>SUMMARY: A plain summary.')
        P.shared_service = lambda *a, **k: service
        text, meta = w._call_api('book', 4096)
        assert text == 'A plain summary.' and meta == {'finish_reason': 'stop'}, (text, meta)
        prompt, kwargs = service.calls[0]
        assert prompt == 'book' and kwargs['model'] == 'm' and kwargs['max_completion_tokens'] == 4096
        assert kwargs['max_retries'] == 1 and kwargs['wait_for_limits'] is False
        assert kwargs.get('system') is None  # HTTP rows send exactly the prompt, as before

        empty = module.EmptyGenerationError('nothing')
        cut = module.IncompleteGenerationError('cut')
        for error, reason in ((empty, None), (cut, 'length')):
            P.shared_service = lambda *a, **k: _FakeService(error)
            assert w._call_api('book', 4096) == ('', {'finish_reason': reason})

        retryable = [module.ProviderLimitReached('limit', 429), module.TransportError('HTTP 503', 503),
                     module.TransportError('timed out'), TimeoutError('slow')]
        for error in retryable:
            P.shared_service = lambda *a, **k: _FakeService(error)
            try:
                w._call_api('book', 4096)
            except jobs.RetryableAPIError:
                pass
            else:
                raise AssertionError('%r must be retried' % error)

        # A subscription's quota notice is no rate limit: minutes of retrying will not reset it.
        P.shared_service = lambda *a, **k: _FakeService(module.ProviderLimitReached("You've hit your session limit"))
        try:
            w._call_api('book', 4096)
        except jobs.RetryableAPIError:
            raise AssertionError('a quota notice must not be retried')
        except RuntimeError as e:
            assert 'session limit' in str(e), e

        P.shared_service = lambda *a, **k: _FakeService(module.TransportError('HTTP 401: bad key', 401))
        try:
            w._call_api('book', 4096)
        except jobs.RetryableAPIError:
            raise AssertionError('a rejected key must not be retried')
        except RuntimeError as e:
            assert '401' in str(e), e
        else:
            raise AssertionError('401 must raise')

        # CLI rows keep the catalogue register: the neutral summary instruction goes along.
        _, w = _worker('claude-cli')
        service = _FakeService('A summary.')
        P.shared_service = lambda *a, **k: service
        w._call_api('book', 4096)
        assert service.calls[0][1]['system'] == P.CLI_SYSTEM
    finally:
        P.shared_service = saved


def test_job_saves_as_it_goes_and_flags_problems_once_at_the_end():
    """The Calibre job writes each summary, then raises one error listing every problem."""
    import queue
    import jobs

    class DB:
        def __init__(self):
            self.fields = {}
        def field_for(self, name, book_id):
            return self.fields.get((name, book_id), f'Book {book_id}' if name == 'title' else None)
        def set_field(self, name, values):
            if name == '#broken':
                raise ValueError('no such column')
            for book_id, val in values.items():
                self.fields[(name, book_id)] = val

    class Worker:
        def __init__(self, db, book_ids, abort, progress, book_done, book_error, **kw):
            self.book_done, self.book_error = book_done, book_error
        def run(self):
            self.book_done(1, 'S1')
            self.book_error(2, 'Traceback...\nRuntimeError: HTTP 402')

    saved, jobs.SummarizerWorker = jobs.SummarizerWorker, Worker
    log = lambda *a: None
    log.error = log
    try:
        db, notes = DB(), queue.Queue()
        try:
            jobs.summarize_books(db, [1, 2], '#summary', {}, notes, threading.Event(), log)
        except RuntimeError as e:
            assert str(e).startswith('1 of 2 book(s)') and 'Book 2: RuntimeError: HTTP 402' in str(e), e
        else:
            raise AssertionError('a failed book must fail the job')
        assert db.fields[('#summary', 1)] == 'S1'
        assert [notes.get()[0] for _ in range(2)] == [0.5, 1.0]

        db = DB()  # a broken column falls back to comments, and is still flagged
        try:
            jobs.summarize_books(db, [1], '#broken', {}, queue.Queue(), threading.Event(), log)
        except RuntimeError as e:
            assert 'saved to comments instead' in str(e), e
        else:
            raise AssertionError('a comments fallback must be flagged')
        assert 'S1' in db.fields[('comments', 1)]
    finally:
        jobs.SummarizerWorker = saved


def main():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        saved_env = {k: os.environ.get(k) for k in ('AW_API_KEY', 'HYPER_API_KEY', 'LOCALAPPDATA')}
        saved_home = P.Path.home
        # Point HOME at an empty dir so a real opencode login cannot skew the test.
        P.Path.home = staticmethod(lambda: home / 'nohome')
        for key in ('AW_API_KEY', 'HYPER_API_KEY'):
            os.environ.pop(key, None)
        try:
            test_validate_key()
            test_list_models_merges_static_and_filters_non_chat()
            test_list_models_survives_a_malformed_body()
            test_row_contexts_beat_the_flat_table()
            test_validate_key_needs_key_rows_and_gemini_ua()
            test_openai_oauth_proxy_startup()
            test_every_row_is_complete()
            test_parse_preserves_prose_and_strips_only_explicit_reasoning()
            test_key_resolution_order(home)
            test_context_window()
            test_empty_reply_is_retried_with_a_bigger_cap()
            test_rows_map_onto_the_shared_service()
            test_shared_module_is_book_writers_and_ships_in_the_zip()
            test_the_plugin_sends_no_completion_requests_itself()
            test_call_api_runs_on_the_shared_service_and_translates_errors()
            test_job_saves_as_it_goes_and_flags_problems_once_at_the_end()
        finally:
            P.Path.home = saved_home
            for key, val in saved_env.items():
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val
    print('providers: all checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
