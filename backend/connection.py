"""User-owned OpenAI-compatible connections; secrets never appear in responses."""
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from fastapi import HTTPException
from pydantic import BaseModel, Field, SecretStr
from .llm import Planner


class ConnectionRequest(BaseModel):
    base_url: str = Field(min_length=1, max_length=2000)
    model: str = Field(min_length=1, max_length=200)
    api_key: SecretStr = Field(default=SecretStr(''), max_length=8192)
    reasoning_effort: str = Field(default='', max_length=20)
    persist: bool = False


def normalize_base(value, hosted=False):
    value = value.strip().rstrip('/')
    if value.endswith('/chat/completions'):
        value = value[:-17].rstrip('/')
    parts = urlsplit(value)
    if parts.scheme not in ('https', 'http') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError('API 주소는 인증 정보·쿼리가 없는 http(s) 기본 주소여야 합니다.')
    if any(c.isspace() for c in value) or '\\' in value:
        raise ValueError('API 주소 형식을 확인하세요.')
    if parts.scheme == 'http' and (hosted or parts.hostname not in ('localhost', '127.0.0.1', '::1')):
        raise ValueError('HTTPS API 주소를 사용하세요. 로컬 실행에서는 localhost HTTP도 지원합니다.')
    if hosted:
        # Hosted builds use a deliberately narrow provider list, preventing a
        # visitor-controlled proxy into metadata services or internal networks.
        allowed = {'api.openai.com', 'openrouter.ai', 'api.deepseek.com', 'api.groq.com', 'api.mistral.ai', 'generativelanguage.googleapis.com'}
        if parts.hostname not in allowed or parts.port not in (None, 443):
            raise ValueError('온라인에서는 안내된 공개 API 제공자만 연결할 수 있습니다. 사내·개인 서버는 설치형에서 연결하세요.')
    return urlunsplit(parts)


def candidate(body, current, hosted=False):
    base = normalize_base(body.base_url, hosted)
    key = body.api_key.get_secret_value().strip()
    if not key:
        if base != current.base or not current.key:
            raise ValueError('새 API 주소에는 해당 제공자의 API 키를 입력하세요.')
        key = current.key
    if any(c in key for c in ('\n', '\r', '\x00')) or any(c in body.model for c in ('\n', '\r', '\x00')):
        raise ValueError('키와 모델은 한 줄로 입력하세요.')
    if body.reasoning_effort not in ('', 'none', 'minimal', 'low', 'medium', 'high'):
        raise ValueError('추론 설정을 확인하세요.')
    result = Planner(inherit_env=False)
    result.base, result.key, result.model = base, key, body.model.strip()
    if not result.model:
        raise ValueError('모델 ID를 입력하세요.')
    result.reasoning_effort = body.reasoning_effort
    return result


def persist_settings(path, planner):
    values = {'WORKMATE_LLM_BASE_URL': planner.base, 'WORKMATE_LLM_API_KEY': planner.key,
              'WORKMATE_LLM_MODEL': planner.model, 'WORKMATE_LLM_REASONING_EFFORT': planner.reasoning_effort}
    # Preserve unrelated settings. This file is excluded from every release.
    lines = path.read_text(encoding='utf-8-sig').splitlines() if path.exists() else []
    lines = [line for line in lines if line.partition('=')[0].strip() not in values]
    lines += [f'{key}={value}' for key, value in values.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.env-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write('\n'.join(lines) + '\n')
        os.chmod(temp, 0o600)
        os.replace(temp, path)
        os.environ.update(values)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def install_connection(app, ctx):
    @app.get('/api/connection/config')
    def get_config():
        hosted = bool(getattr(ctx, 'HOSTED', False))
        return {**ctx.planner.public_status(), 'base_url': ctx.planner.base,
                'has_key': bool(ctx.planner.key), 'reasoning_effort': ctx.planner.reasoning_effort,
                'can_persist': not hosted, 'hosted': hosted}

    @app.post('/api/connection/config')
    def configure(body: ConnectionRequest):
        hosted = bool(getattr(ctx, 'HOSTED', False))
        if hosted and body.persist:
            raise HTTPException(400, '온라인 연결 키는 서버 메모리에만 보관합니다.')
        new = candidate(body, ctx.planner, hosted)
        new.test_connection()
        if body.persist:
            persist_settings(ctx.ROOT / '.env', new)
        with ctx.planner._lock:
            ctx.planner.base, ctx.planner.key, ctx.planner.model = new.base, new.key, new.model
            ctx.planner.reasoning_effort = new.reasoning_effort
            ctx.planner._status.update(new._status)
        return get_config()

    @app.delete('/api/connection/config')
    def disconnect():
        with ctx.planner._lock:
            ctx.planner.key = ctx.planner.base = ctx.planner.model = ''
            ctx.planner._status.update(status='not_configured', error=None, last_checked_at=None)
        return get_config()
