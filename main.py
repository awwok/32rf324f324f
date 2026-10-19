"""Mio BotHost transport. All profiles and conversation states live in the site's DB."""
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import signal
import time
import urllib.error
import urllib.parse
import urllib.request

LOG = logging.getLogger('mio-bot')
ALLOWED = {'sendMessage', 'sendPhoto', 'sendVoice', 'answerCallbackQuery'}


class RemoteError(Exception):
    def __init__(self, code, retry_after=0):
        self.code, self.retry_after = int(code), int(retry_after)
        super().__init__(f'Remote service returned code {self.code}')


class Worker:
    def __init__(self):
        self.token = os.environ.get('BOT_TOKEN', '')
        self.site = os.environ.get('MIO_SITE_URL', '').rstrip('/')
        self.secret = os.environ.get('MIO_BRIDGE_SECRET', '')
        if not self.token or len(self.secret) < 32:
            raise ValueError('Set BOT_TOKEN and MIO_BRIDGE_SECRET (at least 32 characters)')
        u = urllib.parse.urlsplit(self.site)
        if u.scheme != 'https' or not u.hostname or u.path or u.query or u.fragment or u.username:
            raise ValueError('MIO_SITE_URL must be the HTTPS site origin without a path')
        self.running = True

    @staticmethod
    def request(url, data, headers=None, timeout=30):
        request = urllib.request.Request(url, data=data, headers=headers or {}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(8 * 1024 * 1024)
        except urllib.error.HTTPError as error:
            try:
                info = json.loads(error.read(16384))
                wait = info.get('parameters', {}).get('retry_after', 0)
            except (ValueError, TypeError):
                wait = 0
            raise RemoteError(error.code, wait) from None
        info = json.loads(raw)
        if info.get('ok') is False or info.get('success') is False:
            raise RemoteError(info.get('error_code', 503), info.get('parameters', {}).get('retry_after', 0))
        return info

    def bridge(self, action, **kwargs):
        raw = json.dumps({'action': action, **kwargs}, ensure_ascii=False, separators=(',', ':')).encode()
        stamp, nonce = str(int(time.time())), secrets.token_hex(32)
        signature = hmac.new(self.secret.encode(), stamp.encode() + b'\n' + nonce.encode() + b'\n' + raw, hashlib.sha256).hexdigest()
        return self.request(self.site + '/api/telegram.php', raw, {
            'Content-Type': 'application/json', 'X-Mio-Request': '1',
            'X-Mio-Bot-Time': stamp, 'X-Mio-Bot-Nonce': nonce, 'X-Mio-Bot-Signature': signature,
        })

    def telegram(self, method, params, timeout=30):
        params = dict(params)
        photo = params.get('photo', '')
        if method == 'sendPhoto' and isinstance(photo, str) and photo.startswith(('img/', 'uploads/', './img/', './uploads/', '/img/', '/uploads/')):
            photo = self.site + '/' + photo.removeprefix('./').lstrip('/')
            params['photo'] = photo
        if method == 'sendPhoto' and isinstance(photo, str) and photo.startswith('data:image/'):
            encoded = photo.split(',', 1)[1]
            image = base64.b64decode(encoded, validate=True)
            if len(image) > 5 * 1024 * 1024:
                raise RemoteError(400)
            mime = photo[5:photo.index(';')]
            boundary = 'Mio' + secrets.token_hex(16)
            parts = []
            for key, value in params.items():
                if key == 'photo':
                    continue
                value = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
            parts.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="avatar"\r\nContent-Type: {mime}\r\n\r\n'.encode(), image, f'\r\n--{boundary}--\r\n'.encode()])
            raw, content = b''.join(parts), 'multipart/form-data; boundary=' + boundary
        else:
            raw, content = json.dumps(params, ensure_ascii=False).encode(), 'application/json'
        return self.request('https://api.telegram.org/bot' + self.token + '/' + method, raw, {'Content-Type': content}, timeout)

    def photo_extra(self, update):
        photos = update.get('message', {}).get('photo', [])
        choices = [p for p in photos if p.get('file_size', 0) <= 2 * 1024 * 1024]
        if not choices:
            return {}
        photo = max(choices, key=lambda p: p.get('width', 0) * p.get('height', 0))
        info = self.telegram('getFile', {'file_id': photo['file_id']})['result']
        path = info.get('file_path', '')
        if not path or '..' in path or not all(c.isalnum() or c in '/_-. ' for c in path):
            return {}
        with urllib.request.urlopen('https://api.telegram.org/file/bot' + self.token + '/' + path, timeout=20) as response:
            image = response.read(2 * 1024 * 1024 + 1)
        if len(image) > 2 * 1024 * 1024 or not image.startswith(b'\xff\xd8\xff'):
            return {}
        return {'photo': 'data:image/jpeg;base64,' + base64.b64encode(image).decode()}

    def drain(self):
        jobs = self.bridge('worker_pending')['jobs']
        for job in jobs:
            if not self.running:
                return
            if job['method'] not in ALLOWED:
                self.bridge('worker_ack', id=job['id'], failed=True, error_code=400)
                continue
            try:
                self.telegram(job['method'], job['params'])
            except RemoteError as error:
                if error.code == 429:
                    time.sleep(min(60, max(1, error.retry_after)))
                    return
                if error.code == 400 and job['method'] == 'sendPhoto':
                    p = job['params']
                    try:
                        self.telegram('sendMessage', {'chat_id': p['chat_id'], 'text': p.get('caption', 'Анкета Mio'), 'reply_markup': p.get('reply_markup', {})})
                    except RemoteError as fallback:
                        if fallback.code not in (400, 403, 404):
                            raise
                        self.bridge('worker_ack', id=job['id'], failed=True, error_code=fallback.code)
                        continue
                elif error.code in (400, 403, 404):
                    self.bridge('worker_ack', id=job['id'], failed=True, error_code=error.code)
                    continue
                else:
                    raise
            self.bridge('worker_ack', id=job['id'])

    def run(self):
        self.telegram('deleteWebhook', {'drop_pending_updates': False})
        self.telegram('setMyCommands', {'commands': [
            {'command': 'start', 'description': 'Главное меню'},
            {'command': 'browse', 'description': 'Смотреть анкеты'},
            {'command': 'profile', 'description': 'Создать или изменить анкету'},
            {'command': 'pause', 'description': 'Скрыть анкету'},
            {'command': 'delete', 'description': 'Удалить анкету знакомств'},
            {'command': 'cancel', 'description': 'Отменить действие'},
        ]})
        self.telegram('setChatMenuButton', {'menu_button': {'type': 'web_app', 'text': 'Mio App', 'web_app': {'url': self.site + '/telegram-app.html'}}})
        offset, backoff = 0, 1
        LOG.info('Worker started; profiles are stored on the Mio site')
        while self.running:
            try:
                self.drain()
                updates = self.telegram('getUpdates', {'offset': offset, 'timeout': 10, 'allowed_updates': ['message', 'callback_query']}, timeout=20)['result']
                for update in updates:
                    extra = self.photo_extra(update)
                    self.bridge('worker_update', update=update, extra=extra)
                    offset = update['update_id'] + 1
                    self.drain()
                backoff = 1
            except RemoteError as error:
                LOG.warning('Service unavailable (%s); retrying', error.code)
                if error.code == 401:
                    raise ValueError('Check bot token, bridge secret and server clock') from None
                time.sleep(min(60, max(backoff, error.retry_after)))
                backoff = min(60, backoff * 2)
            except (OSError, ValueError, KeyError):
                # Never log request URLs, tokens, signed updates, user content or secrets.
                LOG.warning('Connection or response error; retrying')
                time.sleep(backoff)
                backoff = min(60, backoff * 2)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    worker = Worker()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: setattr(worker, 'running', False))
    worker.run()
