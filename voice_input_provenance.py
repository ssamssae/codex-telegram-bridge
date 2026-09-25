"""Local display-only voice receipts. Never an authentication or approval signal."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

TTL = 120


def receipt_dir():
    return Path.home() / '.local/state/jarvis-session-input/provenance'


def _write(path, data):
    temp = path.with_suffix('.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data, stream)
    os.replace(temp, path)


def record_voice_input(request, session_path, *, directory=None, now=None, pane_pid=None):
    if request.get('origin') != 'microphone' or request.get('engine') != 'codex':
        return None
    directory = Path(directory) if directory is not None else receipt_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for old in directory.glob('*.json'):
        try:
            if time.time() - old.stat().st_mtime > 86400:
                old.unlink()
        except OSError:
            pass
    session = Path(session_path).resolve() if session_path is not None else None
    if session is None and (not isinstance(pane_pid, int) or pane_pid <= 0):
        return None
    path = directory / (str(uuid.UUID(request['id'])) + '.json')
    _write(path, dict(version=1, program='jarvis', origin='microphone',
                     node=request['node'], engine='codex', session=str(session) if session else None, pane_pid=pane_pid,
                     start_source='google-home-matter' if request.get('start_source') == 'google-home-matter' else 'microphone',
                     capture_node=request.get('capture_node') if isinstance(request.get('capture_node'), str) and 0 < len(request['capture_node']) <= 64 else None,
                     after_offset=session.stat().st_size if session else 0,
                     sha256=hashlib.sha256(request['text'].encode()).hexdigest(),
                     created_at=time.time() if now is None else now))
    return path


def bind_voice_input_session(receipt, session_path):
    """Bind only the exact received user event in the receiver's verified live session."""
    path = Path(receipt)
    data = json.loads(path.read_text())
    target = str(Path(session_path).resolve())
    previous = data.get('session')
    if previous == target or 'matched_offset' in data:
        return
    offset = 0
    with Path(session_path).open('rb') as stream:
        for line in stream:
            try:
                event = json.loads(line)
                payload = event.get('payload', {})
                # /clear can leave the previous JSONL open until the next submit.
                # A cross-session rebind must prove this event followed injection.
                if previous is not None:
                    stamp = datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00')).timestamp()
                    if not 0 <= stamp - data['created_at'] <= TTL:
                        offset += len(line)
                        continue
                body = None
                if payload.get('role') == 'user':
                    content = payload.get('content', [])
                    body = content if isinstance(content, str) else ''.join(
                        part.get('text', '') for part in content if isinstance(part, dict))
                elif payload.get('type') == 'user_message':
                    body = payload.get('message', '')
                if isinstance(body, str) and hashlib.sha256(body.encode()).hexdigest() == data['sha256']:
                    data['session'] = str(Path(session_path).resolve())
                    data['after_offset'] = offset
                    _write(path, data)
                    return
            except (ValueError, TypeError, AttributeError, KeyError):
                pass
            offset += len(line)


def match_voice_input(text, session_path, event_offset, node, *, directory=None, now=None, details=False, pane_pid=None, resolve_session=None):
    """Bind once to a concrete JSONL event; retries of that event remain valid."""
    if not session_path or not isinstance(event_offset, int) or event_offset < 0:
        return False
    directory = Path(directory) if directory is not None else receipt_dir()
    now = time.time() if now is None else now
    expected = hashlib.sha256(text.encode()).hexdigest()
    matches = []
    for path in directory.glob('*.json'):
        try:
            data = json.loads(path.read_text())
            if (data.get('version') != 1 or data.get('program') != 'jarvis'
                    or data.get('origin') != 'microphone' or data.get('engine') != 'codex'
                    or data.get('node') != node or data.get('sha256') != expected
                    or not 0 <= now - data['created_at'] <= TTL
                    or data.get('matched_offset', event_offset) != event_offset):
                continue
            expected_session = str(Path(session_path).resolve())
            if data.get('session') is None:
                # A pending first input binds only to this exact live pane's root session.
                if (not isinstance(pane_pid, int) or pane_pid <= 0
                        or data.get('pane_pid') != pane_pid or resolve_session is None):
                    continue
                active_session = resolve_session(pane_pid)
                if active_session is None or str(Path(active_session).resolve()) != expected_session:
                    continue
                data['session'] = expected_session
            elif data.get('session') != expected_session:
                if (not isinstance(pane_pid, int) or pane_pid <= 0
                        or data.get('pane_pid') != pane_pid or resolve_session is None
                        or 'matched_offset' in data):
                    continue
                active_session = resolve_session(pane_pid)
                if active_session is None or str(Path(active_session).resolve()) != expected_session:
                    continue
                bind_voice_input_session(path, session_path)
                data = json.loads(path.read_text())
                if data.get('session') != expected_session:
                    continue
            if event_offset < data['after_offset']:
                continue
            # A different user event after injection invalidates this pending receipt.
            # Do not label a later manually repeated phrase as voice input.
            if 'matched_offset' not in data:
                gap = event_offset - data['after_offset']
                if gap > 1024 * 1024:
                    continue
                with Path(session_path).open('rb') as stream:
                    stream.seek(data['after_offset'])
                    prior = stream.read(gap).decode('utf-8')
                interrupted = False
                for line in prior.splitlines():
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    payload = record.get('payload', {})
                    if isinstance(payload, dict) and (
                        payload.get('role') == 'user' or payload.get('type') == 'user_message'
                        or (isinstance(payload.get('item'), dict)
                            and payload['item'].get('type') == 'UserMessage')):
                        interrupted = True
                        break
                if interrupted:
                    continue
            matches.append((path, data))
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
    if len(matches) != 1:
        return False
    path, data = matches[0]
    data['matched_offset'] = event_offset
    try:
        _write(path, data)
    except OSError:
        return False
    return data if details else True
