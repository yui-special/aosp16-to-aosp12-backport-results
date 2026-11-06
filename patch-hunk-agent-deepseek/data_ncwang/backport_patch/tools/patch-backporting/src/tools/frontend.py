"""Independent frontend requests: configuration and validated per-item checkpoints."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def implementation_id(*paths):
    return [hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in paths]


def stage_options(stage):
    config = json.loads(os.environ.get('FRONTEND_OPTIONS', '{}'))
    if not isinstance(config, dict) or not isinstance(config.get(stage, {}), dict):
        raise ValueError('frontend and frontend.' + stage + ' must be objects')
    local = config.get(stage, {})
    workers = local.get('workers', config.get('workers', 3))
    resume = local.get('resume', config.get('resume', True))
    options = local.get('request_options', {'temperature': 0})
    timeout = local.get('read_timeout', 75 if stage == 'mapping' else 60)
    prefix = 'MAP' if stage == 'mapping' else stage.upper()
    if prefix + '_REQUEST_OPTIONS' in os.environ:
        options = json.loads(os.environ[prefix + '_REQUEST_OPTIONS'])
    if prefix + '_READ_TIMEOUT' in os.environ:
        timeout = float(os.environ[prefix + '_READ_TIMEOUT'])
    if type(workers) is not int or workers < 1 or type(resume) is not bool:
        raise ValueError('frontend workers must be positive integers; resume must be boolean')
    if (not isinstance(options, dict) or set(options) & {'model', 'messages', 'stream'}
            or not isinstance(timeout, (int, float)) or not 0 < timeout < float('inf')):
        raise ValueError('invalid frontend request_options or read_timeout')
    return dict(workers=workers, resume=resume, request_options=options, read_timeout=timeout)


def write_json(path, value):
    """Publish complete JSON atomically beside the existing stage output."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as out:
            temporary = out.name
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.write('\n')
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


class Checkpoint:
    def __init__(self, output, identity, resume=True):
        self.path = Path(str(output) + '.checkpoint.json')
        self.identity = digest([identity, implementation_id(__file__)])
        self.entries = {}
        if resume:
            try:
                saved = json.loads(self.path.read_text())
                if saved.get('identity') == self.identity and isinstance(saved.get('entries'), dict):
                    self.entries = saved['entries']
            except (OSError, ValueError, AttributeError):
                pass

    def get(self, key, validate):
        entry = self.entries.get(str(key))
        if not isinstance(entry, dict) or 'value' not in entry or digest(entry['value']) != entry.get('digest'):
            return None
        value = deepcopy(entry['value'])
        try:
            validate(value)
        except (ValueError, TypeError, KeyError, AttributeError):
            return None
        return value

    def put(self, key, value):
        self.entries[str(key)] = dict(digest=digest(value), value=deepcopy(value))
        write_json(self.path, dict(identity=self.identity, entries=self.entries))


def run_tasks(items, work, checkpoint, validate, workers, cacheable=lambda value: True):
    """Yield (key, value, reused); only the coordinator writes checkpoints.

    Keep at most workers requests in flight. On failure, retain successful in-flight
    work, cancel queued work and propagate the failure. Never cache invalid output.
    The caller restores hunk/group order when publishing the final stage result.
    """
    pending = []
    for key, payload in items:
        value = checkpoint.get(key, validate)
        if value is not None and cacheable(value):
            yield key, value, True
        else:
            pending.append((key, payload))
    remaining = iter(pending)
    first_error = None
    with ThreadPoolExecutor(max_workers=workers) as pool:
        active = {}
        def submit():
            item = next(remaining, None)
            if item is not None:
                key, payload = item
                active[pool.submit(work, payload)] = key
        for _ in range(min(workers, len(pending))):
            submit()
        while active:
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                key = active.pop(future)
                try:
                    value = future.result()
                    validate(value)
                    if cacheable(value):
                        checkpoint.put(key, value)
                    yield key, value, False
                except Exception as exc:
                    first_error = first_error or exc
            if first_error is None:
                for _ in done:
                    submit()
        if first_error is not None:
            raise first_error
