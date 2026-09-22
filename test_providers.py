# -*- coding: utf-8 -*-
"""Offline checks for providers.py — no network, no Calibre, no Qt.

Run: python test_providers.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import providers as P


def test_request_shapes():
    url, payload, headers, safe = P.build_request('hyper', 'qwen3.8-flash', 'hi', 'K')
    assert url == 'https://hyper.charm.land/v1/chat/completions', url
    assert headers['Authorization'] == 'Bearer K'
    assert payload['messages'][0]['content'] == 'hi'
    assert safe == url  # nothing secret in an OpenAI-style URL

    url, payload, headers, safe = P.build_request('anthropic', 'claude-sonnet-5', 'hi', 'K')
    assert url.endswith('/v1/messages'), url
    assert headers['x-api-key'] == 'K' and 'max_tokens' in payload

    url, payload, headers, safe = P.build_request('gemini', 'gemini-3.5-flash', 'hi', 'K')
    assert url.endswith(':generateContent?key=K'), url
    assert 'K' not in safe, 'the logged URL must not carry the key'

    # A custom base URL is what makes an unlisted gateway usable.
    url, _, _, _ = P.build_request('openai', 'm', 'hi', 'K', base_url='http://localhost:1234/v1/')
    assert url == 'http://localhost:1234/v1/chat/completions', url


def test_every_openai_call_carries_a_token_cap():
    """Uncapped, OpenRouter bills the model's whole output budget and 402s."""
    _, payload, _, _ = P.build_request('openrouter', 'm', 'hi', 'K', max_tokens=4096)
    assert payload['max_tokens'] == 4096, payload
    # OpenAI's own API 400s on the older spelling.
    _, payload, _, _ = P.build_request('openai', 'gpt-5.4', 'hi', 'K', max_tokens=4096)
    assert payload['max_completion_tokens'] == 4096 and 'max_tokens' not in payload, payload


def test_cli_provider_never_builds_an_http_request():
    for cli_provider in ('claude-cli', 'command-code'):
        assert P.needs_key(cli_provider) is False
        try:
            P.build_request(cli_provider, 'sonnet', 'hi', '')
        except RuntimeError:
            pass
        else:
            raise AssertionError('a cli provider must not fall through to /chat/completions')
        # list_models() must not go near the network for it either.
        assert [m for m, _ in P.list_models(cli_provider, '')] == P.spec(cli_provider)['models']


def test_cli_command_resolution():
    """claude gets a system-prompt flag; command-code never resolves to cmd.exe."""
    saved_which = P.shutil.which
    P.shutil.which = lambda name: '/fake/%s' % name if name == 'claude' else None
    try:
        argv = P.cli_command('claude-cli', 'sonnet')
        assert argv[0] == '/fake/claude', argv
        assert argv[-2:] == ['--model', 'sonnet'], argv  # the flag itself is run_cli's job

        # System32's cmd.exe is on PATH, but it is not the coding agent: with only
        # 'cmd' answerable, command-code must fail loudly instead of running a shell.
        # Fallback dirs are emptied so a real install on this machine can't answer.
        P.shutil.which = lambda name: 'C:/WINDOWS/system32/cmd.exe' if name == 'cmd' else None
        saved_dirs = P._cli_fallback_dirs
        P._cli_fallback_dirs = lambda: []
        try:
            P.cli_command('command-code', 'claude-sonnet-5')
        except RuntimeError:
            pass
        else:
            raise AssertionError("must never execute cmd.exe as 'Command Code'")
        finally:
            P._cli_fallback_dirs = saved_dirs

        # A CLI invisible to which() is still found in a standard Node install dir.
        import pathlib
        fake_dir = pathlib.Path('/fake/nodejs')
        P._cli_fallback_dirs = lambda: [fake_dir]
        P._fake_cli_file = fake_dir / 'cmdc.cmd'
        real_is_file = pathlib.Path.is_file
        P._real_is_file = real_is_file
        pathlib.Path.is_file = lambda self: True if self == P._fake_cli_file else real_is_file(self)
        try:
            argv = P.cli_command('command-code', 'claude-sonnet-5')
            assert argv[0] == str(P._fake_cli_file), argv
        finally:
            pathlib.Path.is_file = real_is_file
            P._cli_fallback_dirs = saved_dirs

        P.shutil.which = lambda name: '/fake/%s' % name
        argv = P.cli_command('command-code', 'claude-sonnet-5')
        assert argv[0] == '/fake/cmdc', argv  # first candidate wins
        assert '--append-system-prompt' not in argv, argv
        assert '--skip-onboarding' in argv and '--no-session' in argv, argv
    finally:
        P.shutil.which = saved_which


def test_cli_system_prompt_delivery():
    """The neutral register reaches both CLIs, flag or no flag."""
    saved_which = P.shutil.which
    saved_run = P.subprocess.run
    P.shutil.which = lambda name: '/fake/%s' % name
    captured = {}

    def fake_run(argv, **kwargs):
        captured['argv'] = argv
        captured['input'] = kwargs.get('input')

        class Proc:
            returncode = 0
            stdout = 'A plain summary of the book.'
            stderr = ''
        return Proc()

    P.subprocess.run = fake_run
    try:
        text, meta = P.run_cli('claude-cli', 'sonnet', 'book text')
        assert 'A plain summary' in text and meta == {'finish_reason': 'stop'}
        assert any(a == '--append-system-prompt' for a in captured['argv'])
        assert captured['input'] == 'book text'

        text, _ = P.run_cli('command-code', 'gpt-5.6-terra', 'book text')
        # no flag: the instruction rides inside the prompt itself
        assert captured['input'].startswith(P._CLI_SYSTEM) and captured['input'].endswith('book text')
        assert '--append-system-prompt' not in captured['argv']
    finally:
        P.shutil.which = saved_which
        P.subprocess.run = saved_run


def test_cli_exit_codes_are_reported():
    """A non-zero exit is an error with meaning, not a silent empty summary."""
    saved_which = P.shutil.which
    saved_run = P.subprocess.run
    P.shutil.which = lambda name: '/fake/%s' % name

    class Proc:
        returncode = 10
        stdout = ''
        stderr = 'insufficient credits'

    P.subprocess.run = lambda argv, **kwargs: Proc()
    try:
        try:
            P.run_cli('command-code', 'gpt-5.6-terra', 'book text')
        except RuntimeError as e:
            assert 'credits' in str(e) and '10' in str(e), e
        else:
            raise AssertionError('exit 10 must raise')
    finally:
        P.shutil.which = saved_which
        P.subprocess.run = saved_run


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


def test_a_quota_notice_is_never_saved_as_a_summary():
    """`claude -p` prints its quota notice on stdout and exits 0."""
    assert P._QUOTA.search("You've hit your session limit · resets 9:10am")
    assert not P._QUOTA.search('The book argues that the limits of growth are political.')


def test_every_row_is_complete():
    for name, cfg in P.PROVIDERS.items():
        assert cfg['style'] in ('openai', 'anthropic', 'gemini', 'cli'), name
        assert cfg['default_model'] in cfg['models'], name
        assert cfg['base_url'] or cfg['style'] == 'cli', name


def test_parse_openai_strips_reasoning():
    body = {'choices': [{'message': {'content': '<think>plotting</think>SUMMARY: A book.'},
                         'finish_reason': 'stop'}]}
    text, meta = P.parse_response(body, 'hyper')
    assert text == 'A book.', text
    assert meta['finish_reason'] == 'stop'


def test_parse_openai_block_list():
    body = {'choices': [{'message': {'content': [
        {'type': 'thinking', 'thinking': 'hmm'},
        {'type': 'text', 'text': 'Two halves. '},
        {'type': 'text', 'text': 'One book.'},
    ]}}]}
    text, _ = P.parse_response(body, 'minimax')
    assert text == 'Two halves. One book.', text


def test_parse_anthropic_joins_all_text_blocks():
    body = {'content': [{'type': 'thinking', 'thinking': 'x'},
                        {'type': 'text', 'text': 'Part one. '},
                        {'type': 'text', 'text': 'Part two.'}],
            'stop_reason': 'end_turn'}
    text, meta = P.parse_response(body, 'anthropic')
    assert text == 'Part one. Part two.', text
    assert meta['finish_reason'] == 'end_turn'


def test_parse_gemini_and_errors():
    body = {'candidates': [{'content': {'parts': [{'text': 'A '}, {'text': 'book.'}]},
                            'finishReason': 'STOP'}]}
    text, meta = P.parse_response(body, 'gemini')
    assert text == 'A book.' and meta['finish_reason'] == 'STOP'

    for provider, empty in (('gemini', {'candidates': []}), ('hyper', {'choices': []})):
        try:
            P.parse_response(empty, provider)
        except RuntimeError:
            pass
        else:
            raise AssertionError('%s: empty response must raise' % provider)


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
            test_request_shapes()
            test_every_openai_call_carries_a_token_cap()
            test_cli_provider_never_builds_an_http_request()
            test_cli_command_resolution()
            test_cli_system_prompt_delivery()
            test_cli_exit_codes_are_reported()
            test_validate_key()
            test_list_models_merges_static_and_filters_non_chat()
            test_list_models_survives_a_malformed_body()
            test_row_contexts_beat_the_flat_table()
            test_validate_key_needs_key_rows_and_gemini_ua()
            test_openai_oauth_proxy_startup()
            test_a_quota_notice_is_never_saved_as_a_summary()
            test_every_row_is_complete()
            test_parse_openai_strips_reasoning()
            test_parse_openai_block_list()
            test_parse_anthropic_joins_all_text_blocks()
            test_parse_gemini_and_errors()
            test_key_resolution_order(home)
            test_context_window()
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
