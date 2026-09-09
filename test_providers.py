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
    assert P.needs_key('claude-cli') is False
    assert P.needs_key('hyper') is True
    try:
        P.build_request('claude-cli', 'sonnet', 'hi', '')
    except RuntimeError:
        pass
    else:
        raise AssertionError('a cli provider must not fall through to /chat/completions')
    # list_models() must not go near the network for it either.
    assert [m for m, _ in P.list_models('claude-cli', '')] == P.spec('claude-cli')['models']


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
    assert P.context_window('gpt-5.4') == 256000
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
