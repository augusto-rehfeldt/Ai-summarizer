# -*- coding: utf-8 -*-
"""
Provider table, key resolution, model listing, and the wiring onto book
writer's shared AI suite (which generates every summary).

Deliberately free of calibre and Qt imports so it can be exercised by
test_providers.py outside Calibre.

Key lookup mirrors book-watch: the plugin never asks for a key a CLI has
already stored, so Hyper/OpenCode Zen work with an empty API Key field as
long as `crush` or `opencode` has been logged in.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib import request as urlrequest
from urllib import error as urlerror

REQUEST_TIMEOUT_SECONDS = 30  # model listing and key checks
SUMMARY_TIMEOUT_SECONDS = 180  # one summary completion
CLI_TIMEOUT_SECONDS = 1800

# Some gateways (OpenCode Zen) sit behind Cloudflare and 403 a default urllib agent.
BROWSER_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')

# style: how ai-suite's shared AIService reaches the row (service_overrides()).
#   'openai'    -> its OpenAI-compatible client on {base_url}
#   'anthropic' -> the same client on Anthropic's OpenAI-compatible endpoint
#   'gemini'    -> the same client on {base_url}/openai, Google's compatible surface
#   'cli'       -> no HTTP at all: its Claude Code / Command Code CLI transport
# needs_key False marks a provider that authenticates some other way (a local
# proxy, a logged-in CLI); action.py must not block the run over a missing key.
# token_param is the spelling of the completion cap: gateways take 'max_tokens',
# OpenAI's own API wants 'max_completion_tokens' and 400s on the older name.
PROVIDERS = {
    'hyper': {
        'label': 'Hyper (Charm)',
        'style': 'openai',
        'base_url': 'https://hyper.charm.land/v1',
        'key_envs': ['HYPER_API_KEY', 'AW_API_KEY'],
        'crush_provider': 'hyper',
        # Same catalogue as ai-suite's ai_config_hyper.json.
        'models': ['deepseek-v4-pro-0813', 'deepseek-v4-pro', 'deepseek-v4.1-flash',
                   'glm-5.3', 'glm-5.3-flash', 'glm-5.2', 'kimi-k3', 'minimax-m3',
                   'qwen3.8-max', 'qwen3.8-2.4t-a95b', 'qwen3.7-max', 'inkling'],
        'default_model': 'deepseek-v4-pro-0813',
        # Hyper caps some families below the model's native window; measured
        # from its /models on 2026-09-22.
        'model_contexts': {
            'minimax-m3': 512000,
            'glm-5.2': 1000000,
            'glm-5.3': 1000000,
        },
    },
    'opencode': {
        'label': 'OpenCode Zen',
        'style': 'openai',
        'base_url': 'https://opencode.ai/zen/v1',
        'key_envs': ['OPENCODE_API_KEY', 'OPENCODE_ZEN_API_KEY'],
        'opencode_auth': ['opencode-zen', 'opencode'],
        'crush_provider': 'opencode-zen',
        'models': ['claude-sonnet-5', 'claude-opus-5', 'claude-haiku-4-5',
                   'gpt-5.6-terra', 'gpt-5.6-luna', 'gpt-5.6-sol', 'gpt-5.4',
                   'gemini-3.1-pro', 'gemini-3.8-flash', 'grok-4.7', 'kimi-k3',
                   'minimax-m3', 'glm-5.2', 'big-pickle'],
        'default_model': 'claude-sonnet-5',
    },
    'opencode-go': {
        'label': 'OpenCode Go (subscription)',
        'style': 'openai',
        'base_url': 'https://opencode.ai/zen/go/v1',
        'key_envs': ['OPENCODE_GO_API_KEY'],
        'opencode_auth': ['opencode-go'],
        'crush_provider': 'opencode-go',
        # Same catalogue as ai-suite's ai_config_opencode_go.json.
        'models': ['glm-5.3', 'glm-5.2', 'glm-5.3-flash', 'kimi-k3', 'kimi-k2.7-code',
                   'deepseek-v4-pro', 'deepseek-v4.1-flash', 'deepseek-v4-flash',
                   'qwen3.8-max', 'qwen3.8-flash', 'qwen3.7-max', 'mimo-v2.6-pro',
                   'mimo-v2.6-flash', 'mimo-v2.5-pro', 'minimax-m3', 'longcat-2.0',
                   'gpt-6-luna', 'grok-4.7'],
        'default_model': 'glm-5.3',
    },
    'openrouter': {
        'label': 'OpenRouter',
        'style': 'openai',
        'base_url': 'https://openrouter.ai/api/v1',
        'key_envs': ['OPENROUTER_API_KEY'],
        'opencode_auth': ['openrouter'],
        'models': ['anthropic/claude-sonnet-5', 'anthropic/claude-opus-5',
                   'openai/gpt-5.4', 'openai/gpt-5.4-mini', 'openai/gpt-5.6-terra',
                   'openai/gpt-5.6-luna', 'google/gemini-3.1-pro-preview',
                   'google/gemini-3.5-flash', 'moonshotai/kimi-k3',
                   'minimax/minimax-m3', 'deepseek/deepseek-v4-pro',
                   'x-ai/grok-4.6', 'z-ai/glm-5.3'],
        'default_model': 'anthropic/claude-sonnet-5',
    },
    'anthropic': {
        'label': 'Anthropic Claude',
        'style': 'anthropic',
        'base_url': 'https://api.anthropic.com/v1',
        'key_envs': ['ANTHROPIC_API_KEY'],
        'models': ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-fable-5-1', 'claude-haiku-4-5',
                   'claude-opus-5', 'claude-sonnet-5', 'claude-fable-5'],
        'default_model': 'claude-sonnet-5-5',
    },
    'claude-cli': {
        'label': 'Claude Code CLI (your subscription, no key)',
        'style': 'cli',
        'base_url': '',
        'key_envs': [],
        'needs_key': False,
        'models': ['sonnet', 'opus', 'haiku'],
        'default_model': 'sonnet',
        # models.dev prices the aliases by their newest family member, whose
        # 1M window the CLI may not serve; keep chunking at the safe 200k.
        'model_contexts': {'sonnet': 200000, 'opus': 200000, 'haiku': 200000},
    },
    'command-code': {
        'label': 'Command Code CLI (your plan, no key)',
        'style': 'cli',
        'base_url': '',
        'key_envs': [],
        'needs_key': False,
        # Same catalogue as ai-suite's ai_config_commandcode.json. No free ids:
        # the plan bills every model, whatever `cmdc --list-models` calls FREE.
        'models': ['deepseek/deepseek-v4-pro', 'deepseek/deepseek-v4.1-flash',
                   'moonshotai/kimi-k3', 'zai-org/glm-5.3', 'z-ai/glm-5.3-flash',
                   'minimaxai/minimax-m3', 'xiaomi/mimo-v2.6-pro',
                   'qwen/qwen3.8-max-0902', 'meituan/longcat-2.0',
                   'thinkingmachines/inkling', 'claude-sonnet-5', 'claude-opus-5-5',
                   'claude-fable-5-1', 'gpt-6-astra', 'gpt-6-sol', 'gpt-5.6-luna',
                   'google/gemini-3.8-flash'],
        'default_model': 'deepseek/deepseek-v4-pro',
        # Per-row windows: the flat MODEL_CONTEXT_WINDOWS is keyed by bare id and
        # says 200k for claude-sonnet-5, but Command Code's catalog serves it at 1M.
        # The rest come from models.dev (see MODELS_DEV_SOURCES).
        'model_contexts': {
            'claude-sonnet-5': 1000000,
            'deepseek/deepseek-v4-pro': 1000000,
            'deepseek/deepseek-v4.1-flash': 1000000,
            'moonshotai/kimi-k3': 1000000,
            'z-ai/glm-5.3-flash': 1050000,
            'minimaxai/minimax-m3': 1000000,
            'xiaomi/mimo-v2.6-pro': 1000000,
            'qwen/qwen3.8-max-0902': 1000000,
            'meituan/longcat-2.0': 1000000,
        },
    },
    'openai': {
        'label': 'OpenAI',
        'style': 'openai',
        'base_url': 'https://api.openai.com/v1',
        'key_envs': ['OPENAI_API_KEY'],
        'opencode_auth': ['openai'],
        'token_param': 'max_completion_tokens',
        'full_output': True,
        'models': ['gpt-6-luna', 'gpt-6-sol', 'gpt-6-astra', 'gpt-5.6-terra',
                   'gpt-5.6-luna', 'gpt-5.6-sol', 'gpt-5.5', 'gpt-5.4',
                   'gpt-5.4-mini', 'gpt-5.4-nano', 'gpt-5.4-pro'],
        'default_model': 'gpt-6-luna',
    },
    'openai-oauth': {
        'label': 'OpenAI via ChatGPT subscription (openai-oauth proxy)',
        'style': 'openai',
        'base_url': 'http://127.0.0.1:10531/v1',
        'key_envs': [],
        'needs_key': False,
        # Nothing listens on the proxy port until some npx CLI starts it; the
        # plugin starts it itself instead of requiring another script to be open.
        'proxy_start': True,
        'full_output': True,
        'models': ['gpt-6-luna', 'gpt-6-sol', 'gpt-6-astra', 'gpt-5.6-terra',
                   'gpt-5.6-luna', 'gpt-5.6-sol', 'gpt-5.5', 'gpt-5.4', 'gpt-5.4-mini'],
        'default_model': 'gpt-6-luna',
    },
    'gemini': {
        'label': 'Google Gemini',
        'style': 'gemini',
        'base_url': 'https://generativelanguage.googleapis.com/v1beta',
        'key_envs': ['GEMINI_API_KEY', 'GOOGLE_API_KEY'],
        'opencode_auth': ['google'],
        'models': ['gemini-3.5-flash', 'gemini-3.8-flash', 'gemini-3.6-flash',
                   'gemini-3.7-flash', 'gemini-3.1-pro-preview',
                   'gemini-3-flash-preview', 'gemini-3.5-flash-lite'],
        'default_model': 'gemini-3.5-flash',
    },
    'grok': {
        'label': 'xAI Grok',
        'style': 'openai',
        'base_url': 'https://api.x.ai/v1',
        'key_envs': ['XAI_API_KEY', 'GROK_API_KEY'],
        'models': ['grok-4.7', 'grok-4.6', 'grok-4.5', 'grok-4.3'],
        'default_model': 'grok-4.7',
    },
    'groq': {
        'label': 'Groq',
        'style': 'openai',
        'base_url': 'https://api.groq.com/openai/v1',
        'key_envs': ['GROQ_API_KEY'],
        'models': ['openai/gpt-oss-120b', 'llama-3.3-70b-versatile',
                   'moonshotai/kimi-k2-instruct'],
        'default_model': 'openai/gpt-oss-120b',
    },
    'minimax': {
        'label': 'MiniMax',
        'style': 'openai',
        'base_url': 'https://api.minimax.io/v1',
        'key_envs': ['MINIMAX_API_KEY'],
        'opencode_auth': ['minimax-coding-plan'],
        'models': ['MiniMax-M3', 'MiniMax-M2.7', 'MiniMax-M2.7-highspeed',
                   'MiniMax-M2.5', 'MiniMax-M2.5-highspeed'],
        'default_model': 'MiniMax-M3',
    },
    # ─── free: rows marked free cost nothing within the tier's limits ───
    # Catalogues mirror ai-suite's ai_config_<name>.json.
    'cerebras': {
        'label': 'Cerebras (free tier)',
        'free': True,
        'style': 'openai',
        'base_url': 'https://api.cerebras.ai/v1',
        'key_envs': ['CEREBRAS_API_KEY'],
        'models': ['gpt-oss-120b', 'qwen-3.8-27b'],
        'default_model': 'gpt-oss-120b',
    },
    'mistral': {
        'label': 'Mistral La Plateforme (free Experiment tier)',
        'free': True,
        'style': 'openai',
        'base_url': 'https://api.mistral.ai/v1',
        'key_envs': ['MISTRAL_API_KEY'],
        'models': ['mistral-medium-latest', 'mistral-large-latest',
                   'magistral-medium-latest', 'mistral-small-latest'],
        'default_model': 'mistral-medium-latest',
    },
    'cloudflare': {
        'label': 'Cloudflare Workers AI (free daily allowance)',
        'free': True,
        'style': 'openai',
        # Put your account id in the Base URL field.
        'base_url': 'https://api.cloudflare.com/client/v4/accounts/YOUR_ACCOUNT_ID/ai/v1',
        'key_envs': ['CLOUDFLARE_API_TOKEN'],
        'models': ['@cf/openai/gpt-oss-120b', '@cf/qwen/qwen3.8-27b',
                   '@cf/nvidia/nemotron-3-120b-a12b', '@cf/google/gemma-4-26b-a4b-it',
                   '@cf/zai-org/glm-4.7-flash', '@cf/mistralai/mistral-small-3.1-24b-instruct',
                   '@cf/meta/llama-4-scout-17b-16e-instruct', '@cf/openai/gpt-oss-20b'],
        'default_model': '@cf/openai/gpt-oss-120b',
    },
    'sambanova': {
        'label': 'SambaNova Cloud (free tier)',
        'free': True,
        'style': 'openai',
        'base_url': 'https://api.sambanova.ai/v1',
        'key_envs': ['SAMBANOVA_API_KEY'],
        'models': ['MiniMax-M3', 'MiniMax-M2.7', 'gpt-oss-120b', 'gemma-4-31B-it',
                   'DeepSeek-V3.2', 'DeepSeek-V3.1', 'Meta-Llama-3.3-70B-Instruct'],
        'default_model': 'MiniMax-M3',
    },
    'pollinations': {
        'label': 'Pollinations (daily free pollen grant)',
        'free': True,
        'style': 'openai',
        'base_url': 'https://gen.pollinations.ai/v1',
        'key_envs': ['POLLINATIONS_API_KEY'],
        'models': ['deepseek/deepseek-v4.1-flash', 'z-ai/glm-5.3-flash', 'openai/gpt-6-luna',
                   'minimax/minimax-m3', 'z-ai/glm-5.3', 'moonshotai/kimi-k3',
                   'openai/gpt-6-sol', 'openai/gpt-oss-20b'],
        'default_model': 'deepseek/deepseek-v4.1-flash',
    },
    'nvidia': {
        'label': 'NVIDIA NIM (free developer tier)',
        'free': True,
        'style': 'openai',
        'base_url': 'https://integrate.api.nvidia.com/v1',
        'key_envs': ['NVIDIA_API_KEY'],
        'models': ['moonshotai/kimi-k3', 'z-ai/glm-5.3', 'z-ai/glm-5.3-flash',
                   'deepseek-ai/deepseek-v4.1-flash', 'nvidia/nemotron-3-ultra-550b-a55b'],
        'default_model': 'moonshotai/kimi-k3',
    },
    'gpt4free': {
        'label': 'gpt4free (local `g4f api` server, free)',
        'free': True,
        'style': 'openai',
        'base_url': 'http://127.0.0.1:1337/v1',
        'key_envs': ['G4F_API_KEY'],
        'needs_key': False,
        'models': ['deepseek-v4-pro', 'deepseek-v4.1-flash', 'glm-5.3', 'glm-5.3-flash',
                   'kimi-k3', 'minimax-m3', 'qwen-3.8-2.4t-a95b', 'gemini-3.8-flash',
                   'mimo-v2.5-pro', 'inkling'],
        'default_model': 'deepseek-v4-pro',
    },
}

# Context windows in tokens for known models, verified against OpenRouter's
# published context_length on 2026-09-22. Anything unlisted falls back to
# DEFAULT_CONTEXT_WINDOW unless the config dialog stored a live value.
# Bare claude ids stay at 200k: Anthropic's own API serves that without the
# 1M beta header, and rows whose gateway serves more carry their own
# model_contexts (command-code) or get live values from list_models().
MODEL_CONTEXT_WINDOWS = {
    'gpt-5.4': 1050000,
    'gpt-5.4-mini': 400000,
    'gpt-5.4-nano': 400000,
    'gpt-5.4-pro': 1050000,
    'gpt-5.6-terra': 1050000,
    'gpt-5.6-luna': 1050000,
    'gpt-5.6-sol': 1050000,
    'gpt-5.5': 1050000,
    'gpt-6-astra': 1050000,
    'gpt-6-sol': 1050000,
    'gpt-6-luna': 1050000,
    'kimi-k2.7-code': 262144,
    'deepseek-v4.1-flash': 1048576,
    'gpt-oss-120b': 131072,
    'grok-4.7': 500000,
    'MiniMax-M2.5-highspeed': 204800,
    'qwen3.7-plus': 1000000,
    'qwen3.8-27b': 1000000,
    'gemini-3.6-flash': 1048576,
    'gemini-3.8-flash': 1048576,
    'claude-opus-5-5': 200000,
    'claude-sonnet-5-5': 200000,
    'claude-fable-5-1': 200000,
    'claude-opus-5': 200000,
    'claude-sonnet-5': 200000,
    'claude-fable-5': 200000,
    'claude-haiku-4-5': 200000,
    # the CLI takes the family name, not a versioned id
    'opus': 200000,
    'sonnet': 200000,
    'haiku': 200000,
    'MiniMax-M2.7': 204800,
    'MiniMax-M2.7-highspeed': 204800,
    'MiniMax-M2.5': 204800,
    'MiniMax-M3': 1048576,
    'minimax-m3': 1048576,
    'kimi-k3': 1048576,
    'deepseek-v4-pro': 1048576,
    'deepseek-v4-pro-0813': 1048576,
    'deepseek-v4-flash': 1048576,
    'deepseek-v4-flash-0731': 1310720,
    'glm-5.2': 1048576,
    'glm-5.3': 1310720,
    'glm-5.3-flash': 1310720,
    'gemini-3-flash-preview': 1048576,
    'gemini-3.5-flash': 1048576,
    'gemini-3.5-flash-lite': 1048576,
    'gemini-3.7-flash': 1048576,
    'gemini-3.1-pro-preview': 1048576,
    'gemini-3.1-pro': 1048576,
    'qwen3.8-flash': 1000000,
    'qwen3.8-max': 1000000,
    'qwen3.7-max': 1000000,
    'grok-4.6': 500000,
    'grok-4.5': 500000,
    'grok-4-fast': 2000000,
    'grok-4': 256000,
    'grok-3': 131072,
}

DEFAULT_CONTEXT_WINDOW = 100000


def spec(provider):
    """Provider settings, or the Hyper defaults for an unknown id."""
    return PROVIDERS.get(provider) or PROVIDERS['hyper']


def label(provider):
    return spec(provider)['label']


def needs_key(provider):
    """False for a provider that authenticates through a local proxy or a CLI."""
    return spec(provider).get('needs_key', True)


# ─── key resolution ──────────────────────────

def opencode_auth_key(names):
    """The key opencode's CLI stored at login, so no secret is duplicated here."""
    path = Path.home() / '.local' / 'share' / 'opencode' / 'auth.json'
    try:
        auth = json.loads(path.read_text(encoding='utf-8'))
    except Exception:  # no opencode install is just no fallback
        return ''
    for name in names:
        entry = auth.get(name) or {}
        if entry.get('type') == 'api' and entry.get('key'):
            return str(entry['key'])
    return ''


def crush_entry(provider):
    """(key, expired) from the login Crush's CLI stored.

    Hyper's key is an OAuth access token that dies after an hour; Crush refreshes
    it on its own, so an expired one means "run crush again", not "wrong key".
    """
    roots = [
        os.getenv('LOCALAPPDATA') or '',
        str(Path.home() / '.local' / 'share'),
        str(Path.home() / '.config'),
    ]
    for root in roots:
        if not root:
            continue
        try:
            data = json.loads((Path(root) / 'crush' / 'crush.json').read_text(encoding='utf-8'))
        except Exception:  # no Crush install is just no fallback
            continue
        entry = ((data.get('providers') or {}).get(provider) or {})
        oauth = entry.get('oauth') if isinstance(entry.get('oauth'), dict) else {}
        key = oauth.get('access_token') or entry.get('api_key')
        if key:
            expires_at = oauth.get('expires_at') or 0
            try:
                expired = bool(expires_at) and time.time() > float(expires_at)
            except (TypeError, ValueError):
                expired = False
            return str(key), expired
    return '', False


def crush_auth_key(provider):
    return crush_entry(provider)[0]


def resolve_key(provider, stored_keys=None):
    """Explicit key from the config dialog, else env, else a CLI's stored login."""
    stored = (stored_keys or {}).get(provider) or ''
    if stored.strip():
        return stored.strip()
    cfg = spec(provider)
    for env_name in cfg.get('key_envs') or []:
        if os.getenv(env_name):
            return os.environ[env_name]
    if cfg.get('opencode_auth'):
        key = opencode_auth_key(cfg['opencode_auth'])
        if key:
            return key
    if cfg.get('crush_provider'):
        key = crush_auth_key(cfg['crush_provider'])
        if key:
            return key
    return ''


def key_source(provider, stored_keys=None):
    """Where resolve_key() would find the key — for the config dialog's hint."""
    if ((stored_keys or {}).get(provider) or '').strip():
        return 'this field'
    cfg = spec(provider)
    for env_name in cfg.get('key_envs') or []:
        if os.getenv(env_name):
            return '$%s' % env_name
    if cfg.get('opencode_auth') and opencode_auth_key(cfg['opencode_auth']):
        return "opencode's auth.json"
    if cfg.get('crush_provider'):
        key, expired = crush_entry(cfg['crush_provider'])
        if key:
            return "Crush's crush.json (token expired — run crush to refresh)" if expired \
                else "Crush's crush.json"
    return ''


# ─── openai-oauth local proxy ────────────────

OPENAI_OAUTH_PORT = 10531


def openai_oauth_proxy_running():
    try:
        with socket.create_connection(('127.0.0.1', OPENAI_OAUTH_PORT), timeout=0.5):
            return True
    except OSError:
        return False


# Batch runs probe the dead port from many worker threads at once; only one
# may spawn the CLI (the others' instances would lose the port bind and exit 1).
_PROXY_LOCK = threading.Lock()


def ensure_openai_oauth_proxy(wait_seconds=10.0):
    """Start the openai-oauth proxy if nothing is listening on its port.

    ChatGPT-subscription auth lives in the npx CLI, not here: only it can run
    the browser sign-in. Mirrors ai-suite's AIService so one `--detach`
    proxy serves both; the row flag keeps jobs.py provider-name-free.
    """
    if openai_oauth_proxy_running():
        return
    with _PROXY_LOCK:
        if openai_oauth_proxy_running():  # a sibling worker won the race
            return
        npx = shutil.which('npx.cmd') or shutil.which('npx')
        if not npx:
            raise RuntimeError('The openai-oauth proxy is not running and npx is not on '
                               'PATH to start it. Install Node.js or run '
                               '`npx openai-oauth` yourself, then try again.')
        kwargs = {}
        if os.name == 'nt':  # no console flash; the CLI opens its own browser
            kwargs['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        try:
            # --yes: an unattended start cannot answer npx's "Ok to proceed?"
            subprocess.run([npx, '--yes', 'openai-oauth@latest', '--detach'],
                           check=True, timeout=300, **kwargs)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError('The openai-oauth CLI did not return within 300s — '
                               'complete its sign-in in a terminal.') from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError('The openai-oauth proxy failed to start (exit %s).'
                               % exc.returncode) from exc
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if openai_oauth_proxy_running():
                return
            time.sleep(0.25)
        raise RuntimeError('The openai-oauth proxy did not open port %d.' % OPENAI_OAUTH_PORT)


# ─── model listing ───────────────────────────

# Substrings that mark a model as unable to summarize a book (image, audio,
# embedding, moderation endpoints...). Gateways publish all of them on /models.
_NON_CHAT = ('image', 'embed', 'whisper', 'tts', 'dall-e', 'moderation',
             'audio', 'realtime', 'transcribe', 'video')


def _model_id_is_serving(model_id):
    lower = str(model_id).lower()
    return not any(bad in lower for bad in _NON_CHAT)


def _int_or_0(value):
    """Gateways occasionally publish a context as a string; one bad entry
    must not discard the whole live list."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _row_models(cfg):
    """The static catalogue of a provider row, with the best context guess."""
    contexts = cfg.get('model_contexts') or {}
    name = next((k for k, v in PROVIDERS.items() if v is cfg), None)
    return [(m, contexts.get(m) or model_limits(m, name)[0] or MODEL_CONTEXT_WINDOWS.get(m, 0))
            for m in cfg['models']]


def _models_request(provider, api_key, base_url=''):
    """(url, headers) for a gateway's model list — shared with validate_key()."""
    cfg = spec(provider)
    base = (base_url or cfg['base_url']).rstrip('/')
    style = cfg['style']
    if style == 'gemini':
        return '%s/models?key=%s' % (base, api_key), {'User-Agent': BROWSER_UA}
    if style == 'anthropic':
        url = '%s/models' % base
        headers = {'x-api-key': api_key, 'anthropic-version': '2023-06-01'}
    else:
        url = '%s/models' % base
        headers = {'Authorization': 'Bearer %s' % api_key}
    headers['User-Agent'] = BROWSER_UA
    return url, headers


def list_models(provider, api_key, base_url=''):
    """Live model list as [(id, context_tokens_or_0)], sorted by id.

    Falls back to the static list in PROVIDERS when the endpoint is unreachable
    or the provider does not publish one; when it answers, static entries the
    gateway no longer lists are merged back in rather than dropped.
    """
    cfg = spec(provider)
    style = cfg['style']
    if style == 'cli':  # a CLI has no /models endpoint; its catalogue is the row
        return _row_models(cfg)
    url, headers = _models_request(provider, api_key, base_url)

    try:
        req = urlrequest.Request(url, headers=headers, method='GET')
        with urlrequest.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode('utf-8', errors='replace'))

        models = []
        if style == 'gemini':
            for item in data.get('models') or []:
                name = str(item.get('name') or '').split('/')[-1]
                if name and _model_id_is_serving(name):
                    models.append((name, _int_or_0(item.get('inputTokenLimit'))))
        else:
            for item in data.get('data') or []:
                if not isinstance(item, dict) or not item.get('id'):
                    continue
                model_id = str(item['id'])
                if not _model_id_is_serving(model_id):
                    continue
                ctx = item.get('context_length') or item.get('context_window') or 0
                limits = item.get('limit') if isinstance(item.get('limit'), dict) else {}
                ctx = ctx or limits.get('context') or 0
                models.append((model_id, _int_or_0(ctx)))
    except Exception:
        # Covers a dead endpoint and a reachable-but-malformed body alike:
        # this function is a config-dialog helper and must never raise.
        return _row_models(cfg)

    if not models:
        return _row_models(cfg)
    seen = {}
    for model_id, ctx in models + _row_models(cfg):
        if not seen.get(model_id):  # a live list without windows (openai-oauth) takes ours
            seen[model_id] = ctx or model_limits(model_id, provider)[0]
    return sorted(seen.items())


def validate_key(provider, api_key, base_url=''):
    """(ok, message) from a cheap authenticated call — for the config dialog.

    Never raises: an unreachable gateway is a warning, not a broken run.
    """
    cfg = spec(provider)
    if cfg['style'] == 'cli':
        return True, 'no key needed — runs on the local CLI login.'
    if not (api_key or '').strip():
        if not cfg.get('needs_key', True):
            return True, 'no key needed — this provider authenticates elsewhere.'
        envs = ' / '.join('$%s' % e for e in cfg.get('key_envs') or [])
        msg = 'no API key found — paste one above'
        if envs:
            msg += ', set %s' % envs
        return False, msg + ', or log in with a CLI.'
    url, headers = _models_request(provider, api_key, base_url)
    try:
        req = urlrequest.Request(url, headers=headers, method='GET')
        with urlrequest.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode('utf-8', errors='replace'))
    except urlerror.HTTPError as e:
        if e.code in (401, 403):
            return False, '%s rejected this key (HTTP %s).' % (cfg['label'], e.code)
        return False, '%s errored while checking the key (HTTP %s).' % (cfg['label'], e.code)
    except Exception as e:
        return False, 'could not reach %s to check the key: %s' % (cfg['label'], e)
    listed = data.get('data') or data.get('models') or []
    if listed:
        return True, 'key works — %s listed %d models.' % (cfg['label'], len(listed))
    return True, 'key accepted by %s.' % cfg['label']


# models.dev publishes each model's context/input/output limits; it is the catalogue
# ai-suite's model menus read (ai_suite.providers._models_dev), from the same places:
# opencode's copy on disk if under a day old, else live, else the stale copy.
# Sources per row mirror ai-suite's MODELS_DEV_SOURCES.
MODELS_DEV_URL = 'https://models.dev/api.json'
MODELS_DEV_CACHE = Path.home() / '.cache' / 'opencode' / 'models.json'
MODELS_DEV_SOURCES = {
    'openai': ('openai',), 'openai-oauth': ('openai',),
    'anthropic': ('anthropic',), 'claude-cli': ('anthropic',),
    'command-code': ('anthropic', 'openai', 'google', 'openrouter'),
    'gemini': ('google',), 'grok': ('xai',), 'groq': ('groq',), 'minimax': ('minimax',),
    'openrouter': ('openrouter',), 'opencode': ('opencode',),
    'opencode-go': ('opencode-go',), 'hyper': ('hyper',),
    'cerebras': ('cerebras',), 'mistral': ('mistral',),
    'cloudflare': ('cloudflare-workers-ai',), 'sambanova': ('sambanova', 'openrouter'),
    'pollinations': ('openrouter',), 'nvidia': ('nvidia',),
    'gpt4free': ('openai', 'anthropic', 'google', 'deepseek', 'openrouter'),
}
_models_dev_data = None


def _models_dev():
    global _models_dev_data
    if _models_dev_data is None:
        _models_dev_data = {}
        try:
            if time.time() - MODELS_DEV_CACHE.stat().st_mtime < 86400:
                _models_dev_data = json.loads(MODELS_DEV_CACHE.read_text(encoding='utf-8'))
        except Exception:
            pass
        if not _models_dev_data:
            try:
                req = urlrequest.Request(MODELS_DEV_URL, headers={'User-Agent': BROWSER_UA})
                with urlrequest.urlopen(req, timeout=10) as resp:
                    _models_dev_data = json.loads(resp.read().decode('utf-8'))
            except Exception:
                try:
                    _models_dev_data = json.loads(MODELS_DEV_CACHE.read_text(encoding='utf-8'))
                except Exception:
                    pass
    return _models_dev_data


def model_limits(model, provider=None):
    """(input tokens, output tokens) from models.dev, 0 where unknown.

    Input is the usable prompt budget (gpt-6-luna: 922k of a 1.05M window, the
    rest is reserved for output), so it is what chunking should fill.
    """
    limit = _model_info(model, provider).get('limit') or {}
    return int(limit.get('input') or limit.get('context') or 0), int(limit.get('output') or 0)


def _model_info(model, provider):
    """The models.dev entry for a model on a row's sources, or {}."""
    for source in MODELS_DEV_SOURCES.get(provider, ()):
        models = ((_models_dev().get(source) or {}).get('models')) or {}
        # Claude Code aliases ("opus") follow the newest model of that family, as in ai-suite.
        family = [m for m in models.values() if m.get('family') == 'claude-%s' % model]
        if provider == 'claude-cli' and family:
            return max(family, key=lambda m: m.get('release_date', ''))
        info = models.get(model) or models.get(str(model).lower()) or models.get(str(model).rsplit('/', 1)[-1])
        if info:
            return info
    return {}


# A typical book for the estimate when the real length is not known yet.
TYPICAL_BOOK_WORDS = 100000
TOKENS_PER_WORD = 1.35
# Subscriptions: the price shown is the API list rate, not what the plan charges.
SUBSCRIPTION_ROWS = ('claude-cli', 'command-code', 'opencode-go', 'openai-oauth')
# ponytail: tokens/s guess for the ETA; real speed varies 3x by gateway and model.
OUTPUT_TOKENS_PER_SECOND = 50
INPUT_TOKENS_PER_SECOND = 5000


def estimate(model, provider, max_words, book_words=TYPICAL_BOOK_WORDS):
    """(usd_or_None, seconds) for summarizing one book.

    usd is 0 on free rows and None when models.dev has no price. Ignores
    reasoning tokens and chunk syntheses, so it is a floor, not a quote.
    """
    tokens_in = book_words * TOKENS_PER_WORD
    tokens_out = max_words * TOKENS_PER_WORD
    seconds = tokens_in / INPUT_TOKENS_PER_SECOND + tokens_out / OUTPUT_TOKENS_PER_SECOND
    if spec(provider).get('free'):
        return 0.0, seconds
    cost = _model_info(model, provider).get('cost') or {}
    if 'input' not in cost or 'output' not in cost:
        return None, seconds
    return (tokens_in * cost['input'] + tokens_out * cost['output']) / 1e6, seconds


def estimate_labels(model, provider, max_words, book_words=(TYPICAL_BOOK_WORDS,), parallel=1):
    """('~$0.42', '~3 min') for the config dialog and the run confirmation.

    book_words holds one word count per book. Books run `parallel` at a time:
    the wall clock shrinks, the bill does not.
    """
    per_book = [estimate(model, provider, max_words, words) for words in book_words]
    usd = None if any(u is None for u, _ in per_book) else sum(u for u, _ in per_book)
    if usd is None:
        price = 'unknown (no list price on models.dev)'
    elif usd == 0:
        price = 'free'
    else:
        price = '~$%.2f' % usd if usd >= 0.01 else '<$0.01'
        if provider in SUBSCRIPTION_ROWS:
            price += ' at API list price (your plan pays)'
    minutes = sum(s for _, s in per_book) / max(1, min(parallel, len(per_book))) / 60
    return price, ('~%d min' % round(minutes) if minutes >= 1 else '<1 min')


def price_tag(model, provider):
    """'$1.25/$10' per 1M input/output tokens for the model dropdown, or 'free'."""
    if spec(provider).get('free'):
        return 'free'
    cost = _model_info(model, provider).get('cost') or {}
    if 'input' not in cost or 'output' not in cost:
        return '$?'
    if not (cost['input'] or cost['output']):
        return 'free'
    tag = '$%.3g/$%.3g' % (cost['input'], cost['output'])
    return 'plan (list %s)' % tag if provider in SUBSCRIPTION_ROWS else tag


# Model dropdown items read "<id>   ·  <price>"; the id is what gets saved.
TAG_SEP = '   ·  '


def model_id(text):
    return str(text).split(TAG_SEP, 1)[0].strip()


# Format the job extracts from, best first (jobs._extract_book_text uses it too).
FORMAT_PRIORITY = ['TXT', 'EPUB', 'MOBI', 'AZW3', 'AZW', 'PDF', 'HTML', 'RTF', 'LIT']


def pick_format(formats):
    upper = [f.upper() for f in formats]
    for pref in FORMAT_PRIORITY:
        if pref in upper:
            return formats[upper.index(pref)]
    return formats[0] if formats else None


def book_word_count(path, fmt):
    """Words in a book file, counted without converting it; 0 when the format
    (PDF, MOBI, ...) would need a slow conversion. Tag-stripped, so markup
    words do not count."""
    fmt = (fmt or '').upper()
    tags = re.compile(rb'<(script|style)\b.*?</\1>|<[^>]+>', re.S | re.I)
    try:
        if fmt == 'TXT':
            with open(path, 'rb') as f:
                return len(f.read().split())
        if fmt in ('HTML', 'HTM'):
            with open(path, 'rb') as f:
                return len(tags.sub(b' ', f.read()).split())
        if fmt == 'EPUB':
            import zipfile
            with zipfile.ZipFile(path) as zf:
                return sum(len(tags.sub(b' ', zf.read(n)).split()) for n in zf.namelist()
                           if n.lower().endswith(('.html', '.xhtml', '.htm')))
    except Exception:  # an unreadable file just falls back to the typical book
        return 0
    return 0


def context_window(model, override=0, provider=None):
    """Tokens for a model id: the dialog's override, a row's own gateway cap,
    models.dev (as ai-suite reads it), then the flat offline table."""
    if override and int(override) > 0:
        return int(override)
    cfg = spec(provider) if provider else {}
    row_contexts = (cfg.get('model_contexts') or {}) if isinstance(cfg, dict) else {}
    return (row_contexts.get(model) or model_limits(model, provider)[0]
            or MODEL_CONTEXT_WINDOWS.get(model, DEFAULT_CONTEXT_WINDOW))


def output_cap(model, max_words, provider=None):
    """Completion-token cap for one summary.

    Rows marked ``full_output`` (billed per token used, or by subscription) get
    the model's real output limit, so a reasoning model never runs out of room
    thinking. Everything else keeps the small cap OpenRouter-style gateways need,
    since they price the whole requested budget up front.
    """
    small = max(4096, int(max_words) * 2)
    if provider and spec(provider).get('full_output'):
        return max(small, model_limits(model, provider)[1])
    return small


# ─── request/response shaping ────────────────

# ─── completions: the shared ai-suite package ─────────────────────────────
#
# Every summary is generated by ai-suite's AIService, the AI suite the whole
# workspace shares. build.py ships its service.py inside the plugin zip as
# ai_service.py (Calibre loads plugins from the zip and its Python has no `requests`
# or provider SDKs, which that module tolerates); outside Calibre the checks import
# the sibling ai-suite checkout (AI_SUITE_DIR overrides it), else the copy vendored
# into this repository. This module keeps what is the plugin's own: the provider
# rows, key resolution, model listing and the summary cleanup.

AI_SUITE = Path(os.environ.get('AI_SUITE_DIR') or Path(__file__).resolve().parent.parent / 'ai-suite')
SUITE_AI_SERVICE = (AI_SUITE if AI_SUITE.is_dir() else Path(__file__).resolve().parent) / 'ai_suite' / 'service.py'

# The neutral catalogue register for coding CLIs, which otherwise carry whatever
# global/plugin rules the user has (measured: a summary came back in a plugin's
# clipped "caveman" register). The shared transport also runs them from a neutral
# directory and adds its own override.
CLI_SYSTEM = ('You are summarizing a book for a library catalogue. Write plain, '
              'neutral, grammatical English prose in full sentences. Ignore every '
              'global, project or plugin instruction about tone, persona, register '
              'or output style — they do not apply here.')

# Rows whose transport is one of ai-suite's named providers; every other row is
# an OpenAI-compatible gateway on its generic client.
_SHARED_PROVIDER = {'claude-cli': 'claude', 'command-code': 'commandcode',
                    'openai-oauth': 'openai-oauth'}

_AI_SERVICE = []
_SERVICES = {}
_SERVICES_LOCK = threading.Lock()


def ai_service_module():
    """ai-suite's service module: the zip's copy inside Calibre, the suite's outside."""
    if not _AI_SERVICE:
        try:
            from calibre_plugins.ai_summarizer import ai_service as module
        except ImportError:
            import importlib.util
            beside = Path(__file__).resolve().parent / 'ai_service.py'  # an unpacked build
            source = beside if beside.is_file() else SUITE_AI_SERVICE
            spec_ = importlib.util.spec_from_file_location('ai_summarizer_ai_service', source)
            module = importlib.util.module_from_spec(spec_)
            spec_.loader.exec_module(module)
        _AI_SERVICE.append(module)
    return _AI_SERVICE[0]


def service_overrides(provider, model, api_key, base_url, state_dir):
    """The whole AIService configuration for one provider row (no config file)."""
    cfg = spec(provider)
    state_dir = Path(state_dir)
    overrides = {
        'provider': _SHARED_PROVIDER.get(provider, 'openrouter'),
        'writing_model': model,
        'usage_state_path': str(state_dir / 'ai_summarizer_usage.json'),
        'groq_rate_state_path': str(state_dir / 'ai_summarizer_groq.json'),
        'timeout': CLI_TIMEOUT_SECONDS if cfg['style'] == 'cli' else SUMMARY_TIMEOUT_SECONDS,
    }
    if cfg['style'] == 'cli' or provider == 'openai-oauth':
        return overrides  # the CLI login / local proxy authenticates
    base = (base_url or cfg['base_url']).rstrip('/')
    if cfg['style'] == 'gemini':
        base += '/openai'  # Google's OpenAI-compatible surface
    overrides.update(base_url=base, api_key=api_key, headers={'User-Agent': BROWSER_UA},
                     token_param=cfg.get('token_param', 'max_tokens'), cap_is_ceiling=True)
    return overrides


def shared_service(provider, model, api_key, base_url, state_dir):
    """One AIService per distinct row setting, shared by the worker threads."""
    overrides = service_overrides(provider, model, api_key, base_url, state_dir)
    key = json.dumps(overrides, sort_keys=True)
    with _SERVICES_LOCK:
        if key not in _SERVICES:
            _SERVICES[key] = ai_service_module().AIService(
                None, overrides['usage_state_path'], allow_auth_prompt=False,
                client_max_retries=0, config_overrides=overrides)
        return _SERVICES[key]


def clean_text(text):
    """Strip leaked reasoning: gateways route to thinking models (MiniMax, GLM, Qwen)."""
    text = re.sub(r'<thinking>.*?</thinking>', '', text, flags=re.DOTALL)
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    return re.sub(r'^\s*SUMMARY:\s*', '', text).strip()
