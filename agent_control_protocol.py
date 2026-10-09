"""Authenticated, opt-in local control of an existing bridge. No model or process launch."""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import http.client
import http.server
import json
import os
from pathlib import Path
import re
import secrets
import socket
import socketserver
import stat
import threading
import time

NODES = ()
ENGINES = ('claude', 'codex', 'cursor', 'grok')
MAX_BODY = 65536


class ControlError(ValueError):
    pass


def validate_current(request, current):
    """Also called under the native input lock immediately before dispatch."""
    if not request.get('session') or request['session'] != current.get('session'):
        raise ControlError('session_changed')
    action = request['action']
    if action not in current.get('capabilities', []):
        raise ControlError('action_unavailable')
    if current.get('draft'):
        raise ControlError('composer_not_empty')
    question = current.get('question')
    if action == 'send' and (question or current.get('status') != 'idle'):
        raise ControlError('agent_not_idle')
    if action == 'stop' and (question or current.get('status') != 'busy'):
        raise ControlError('no_running_turn')
    if action == 'stop' and 'turn' in request and (
            not request['turn'] or request['turn'] != current.get('turn')):
        raise ControlError('turn_changed')
    if action == 'answer' and (not question or question.get('id') != request.get('question')
            or request.get('option') not in [o['value'] for o in question.get('options', [])]):
        raise ControlError('question_changed')


def private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ControlError('unsafe_directory')
    os.chmod(path, 0o700)
    return path


def write_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.' + secrets.token_hex(8) + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def safe_private_file(path, kind=stat.S_ISREG):
    info = Path(path).lstat()
    if not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ControlError('unsafe_endpoint')


class Controller:
    def __init__(self, node, engine, root, snapshot, perform, *, allowed_nodes=NODES):
        if node not in allowed_nodes or engine not in ENGINES:
            raise ControlError('invalid_endpoint')
        self.node, self.engine = node, engine
        self.root = private_dir(root)
        self.observe, self.perform = snapshot, perform
        self.lock = threading.RLock()

    def snapshot(self):
        with self.lock:
            result = self.observe()
            result.update(node=self.node, engine=self.engine, connected=True, observed_at=time.time())
            return result

    def execute(self, request):
        if not isinstance(request, dict):
            raise ControlError('invalid_request')
        if (request.get('node'), request.get('engine')) != (self.node, self.engine):
            raise ControlError('wrong_endpoint')
        rid = request.get('id', '')
        if not isinstance(rid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', rid):
            raise ControlError('invalid_request_id')
        action = request.get('action')
        if action not in ('send', 'stop', 'answer'):
            raise ControlError('unsupported_action')
        if action == 'send':
            text = request.get('text')
            if (not isinstance(text, str) or not text.strip() or len(text.encode()) > 16000
                    or text.lstrip().startswith('/')
                    or any(ord(c) < 32 and c not in '\n\t' or ord(c) == 127 for c in text)):
                raise ControlError('invalid_prompt')
        digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        with self.lock, (self.root/'action.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = self.root/(rid+'.json')
            if path.exists():
                safe_private_file(path)
                previous = json.loads(path.read_text())
                if previous['digest'] != digest:
                    raise ControlError('request_id_conflict')
                # A crash after intent, before completion, is NOT safe to replay.
                return {**previous, 'status': 'uncertain'} if previous['status'] == 'dispatching' else previous
            validate_current(request, self.snapshot())
            receipt = {'id': rid, 'node': self.node, 'engine': self.engine,
                       'digest': digest, 'action': action, 'status': 'dispatching', 'at': time.time()}
            write_json(path, receipt)
            try:
                outcome = self.perform(request)
                receipt.update({k: v for k, v in (outcome or {'status': 'accepted'}).items()
                                if k in ('status', 'error', 'job', 'detail')})
            except ControlError as exc:
                receipt.update(status='rejected', error=str(exc))
            except Exception:
                receipt.update(status='uncertain', error='outcome_unknown_no_automatic_retry')
            write_json(path, receipt)
            return receipt
