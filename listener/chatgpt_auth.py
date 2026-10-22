"""Sign in with ChatGPT public-client OAuth; credentials never leave protected storage.

This module deliberately does not read Codex credentials or accept API keys.
Run ``python -m listener.chatgpt_auth login`` for the interactive browser flow.
"""
from __future__ import annotations

import base64
import contextlib
import ctypes
import hashlib
import json
import math
import os
import secrets
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from listener.provider_errors import ProviderError

ISSUER = "https://auth.openai.com"
DISCOVERY_URL = ISSUER + "/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
INITIAL_CLIENT = "dynamic_agent_client"
AGENT_NAME = "Agents MY Orchestrator"
DIRECT_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke " + DIRECT_SCOPE
_REFRESH_MARGIN = 60
_TERMINAL_REFRESH_CODES = (
    "invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
    "refresh_token_invalidated", "refresh_token_reused",
)


def _error(kind, message, retry_at=None):
    # Never pass through server messages, exception strings, URLs, or token values.
    return ProviderError("chatgpt", kind, message, retry_at=retry_at)


class _TerminalRefreshError(ProviderError):
    """A documented terminal failure that invalidates the saved token set."""


def _windows():
    return sys.platform == "win32"


def _dpapi(data: bytes, decrypt=False) -> bytes:
    """Windows user-bound encryption, with UI disabled (no machine-wide flag)."""
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buf = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        fn = crypt.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        fn = crypt.CryptProtectData
        fn.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(source), "Agents MY ChatGPT", None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise _error("configuration", "Не удалось открыть защищённое хранилище ChatGPT")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def _atomic_write(path: Path, data: bytes, private=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    if private and not _windows():
        path.parent.chmod(0o700)
    fd, name = tempfile.mkstemp(prefix=".chatgpt-", dir=path.parent)
    try:
        if not _windows():
            os.chmod(name, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@dataclass(repr=False)
class _Attempt:
    state: str
    nonce: str
    verifier: str
    redirect_uri: str
    client_id: str
    host_id: str
    subject: str | None


class ChatGPTAuth:
    def __init__(self, root=Path("."), http=None, clock=time.time):
        self.root, self.clock = Path(root), clock
        self.http = http or httpx.Client(timeout=30, follow_redirects=False)
        self.path = self.root / "runs" / "private" / "chatgpt-credentials.dat"
        self.lock_path = self.path.with_suffix(".lock")
        self.host_path = self.root / "runs" / "chat" / "chatgpt-host.json"
        self._mutex = threading.RLock()
        self._metadata = None

    @contextlib.contextmanager
    def _locked(self):
        """Serialize rotating refresh tokens across threads and listener processes."""
        with self._mutex:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            with self.lock_path.open("a+b") as fh:
                if not _windows():
                    self.lock_path.chmod(0o600)
                fh.seek(0, os.SEEK_END)
                if not fh.tell():
                    fh.write(b"0")
                    fh.flush()
                deadline = time.monotonic() + 35
                while True:
                    try:
                        fh.seek(0)
                        if _windows():
                            import msvcrt
                            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise _error("network", "ChatGPT: обновление входа уже выполняется") from None
                        time.sleep(0.05)
                try:
                    yield
                finally:
                    fh.seek(0)
                    if _windows():
                        import msvcrt
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _load(self):
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            return {}
        try:
            if _windows():
                if not data.startswith(b"dpapi:v1\n"):
                    raise ValueError()
                data = _dpapi(data[9:], decrypt=True)
            else:
                if self.path.stat().st_mode & 0o077:
                    raise ValueError()
                if not data.startswith(b"json:v1\n"):
                    raise ValueError()
                data = data[8:]
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ValueError()
            for name in ("client_id", "subject", "issuer", "access_token", "refresh_token", "id_token"):
                if name in result and not isinstance(result[name], str):
                    raise ValueError()
            if "scopes" in result and (not isinstance(result["scopes"], list) or
                                       any(not isinstance(scope, str) for scope in result["scopes"])):
                raise ValueError()
            for name in ("expires_at", "earliest_refresh_at"):
                if name in result and (isinstance(result[name], bool) or
                        not isinstance(result[name], (int, float)) or not math.isfinite(result[name])):
                    raise ValueError()
            return result
        except (ValueError, TypeError, OSError):
            raise _error("configuration", "Хранилище ChatGPT повреждено или не защищено") from None

    def _save(self, record):
        data = json.dumps(record, ensure_ascii=False).encode("utf-8")
        data = b"dpapi:v1\n" + _dpapi(data) if _windows() else b"json:v1\n" + data
        _atomic_write(self.path, data)

    def _host(self, retained=None):
        try:
            host = json.loads(self.host_path.read_text(encoding="utf-8"))["ext_agent_host_id"]
            if not isinstance(host, str) or not host.startswith("urn:uuid:"):
                raise ValueError()
            uuid.UUID(host[9:])
            if retained and retained != host:
                raise ValueError()
            return host
        except FileNotFoundError:
            host = retained or "urn:uuid:" + str(uuid.uuid4())
            if not isinstance(host, str) or not host.startswith("urn:uuid:"):
                raise _error("configuration", "Некорректный постоянный идентификатор хоста ChatGPT") from None
            try:
                uuid.UUID(host[9:])
            except ValueError:
                raise _error("configuration", "Некорректный постоянный идентификатор хоста ChatGPT") from None
            _atomic_write(self.host_path, json.dumps({"ext_agent_host_id": host}).encode(), private=False)
            return host
        except (ValueError, KeyError, TypeError):
            raise _error("configuration", "Некорректный постоянный идентификатор хоста ChatGPT") from None

    @staticmethod
    def _official(url):
        parts = urlsplit(str(url))
        if (parts.scheme != "https" or parts.netloc != "auth.openai.com" or
                parts.username or parts.password or parts.fragment):
            raise _error("configuration", "OIDC endpoint должен принадлежать auth.openai.com")
        return url

    def _get_json(self, url):
        try:
            response = self.http.get(self._official(url), follow_redirects=False)
        except httpx.HTTPError:
            raise _error("network", "Сервер входа ChatGPT недоступен") from None
        if response.status_code != 200:
            raise _error("network", "Сервер входа ChatGPT временно недоступен")
        try:
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (ValueError, TypeError):
            raise _error("configuration", "Сервер входа ChatGPT вернул некорректные метаданные") from None

    def _discovery(self):
        if self._metadata is None:
            meta = self._get_json(DISCOVERY_URL)
            if meta.get("issuer") != ISSUER:
                raise _error("configuration", "Неожиданный издатель OIDC ChatGPT")
            for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                if not isinstance(meta.get(key), str):
                    raise _error("configuration", "Неполные метаданные OIDC ChatGPT")
                self._official(meta[key])
            self._metadata = meta
        return self._metadata

    def _post_token(self, data):
        endpoint = self._discovery()["token_endpoint"]
        try:
            response = self.http.post(endpoint, data=data, follow_redirects=False)
        except httpx.HTTPError:
            raise _error("network", "Не удалось обновить вход ChatGPT: сеть недоступна") from None
        try:
            result = response.json()
        except ValueError:
            result = {}
        if not isinstance(result, dict):
            result = {}
        if response.status_code == 200 and not result.get("error"):
            return result
        code = result.get("error")
        if code in _TERMINAL_REFRESH_CODES:
            raise _TerminalRefreshError("chatgpt", "auth",
                                        f"ChatGPT: вход истёк или отозван ({code}); выполните вход снова")
        if code in ("invalid_client", "unauthorized_client", "invalid_scope"):
            raise _error("configuration", "ChatGPT: параметры регистрации отклонены")
        if response.status_code == 429:
            try:
                delay = max(1, min(float(response.headers.get("Retry-After", "60")), 3600))
            except ValueError:
                delay = 60
            raise _error("rate_limit", "ChatGPT: временно ограничено обновление входа", self.clock() + delay)
        if response.status_code >= 500:
            raise _error("network", "ChatGPT: временная ошибка сервера входа")
        raise _error("auth", "ChatGPT: вход не завершён; повторите авторизацию")

    def _validate_id(self, token, client_id, nonce=None, subject=None, require_nonce=True):
        try:
            import jwt
        except ImportError:
            raise _error("configuration", "Для проверки входа требуется PyJWT[crypto]") from None
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in ("RS256", "ES256") or not header.get("kid"):
                raise ValueError()
            jwks = self._get_json(self._discovery()["jwks_uri"])
            matches = [key for key in jwks.get("keys", []) if key.get("kid") == header["kid"]]
            if len(matches) != 1:
                raise ValueError()
            jwk = jwt.PyJWK.from_dict(matches[0], algorithm=header["alg"])
            claims = jwt.decode(token, jwk.key, algorithms=[header["alg"]], audience=client_id,
                                issuer=ISSUER, options={"require": ["iss", "aud", "exp", "iat", "sub"],
                                                       "verify_exp": False, "verify_iat": False,
                                                       "verify_nbf": False})
            now = self.clock()
            if not isinstance(claims["sub"], str) or not claims["sub"]:
                raise ValueError()
            for field in ("exp", "iat", "nbf"):
                if field in claims and (isinstance(claims[field], bool) or
                        not isinstance(claims[field], (int, float)) or not math.isfinite(claims[field])):
                    raise ValueError()
            if claims["exp"] <= now or claims["iat"] > now + 60 or claims.get("nbf", 0) > now + 60:
                raise ValueError()
            if require_nonce or "nonce" in claims:
                if not isinstance(claims.get("nonce"), str) or not secrets.compare_digest(claims["nonce"], nonce or ""):
                    raise ValueError()
            if subject is not None and claims["sub"] != subject:
                raise ValueError()
            if (isinstance(claims["aud"], list) and len(claims["aud"]) > 1) or "azp" in claims:
                if claims.get("azp") != client_id:
                    raise ValueError()
            return claims
        except ProviderError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, jwt.PyJWTError):
            raise _error("auth", "ChatGPT: подпись, аккаунт или параметры ID token не прошли проверку") from None

    def _credentials(self, result, client_id, host_id, nonce, previous=None):
        previous = previous or {}
        scope = result.get("scope")
        scopes = scope.split() if isinstance(scope, str) else previous.get("scopes", [])
        if DIRECT_SCOPE not in scopes:
            raise _error("auth", "Вход выполнен без разрешения использовать план ChatGPT")
        if (not isinstance(result.get("access_token"), str) or not result["access_token"] or
                not isinstance(result.get("refresh_token"), str) or not result["refresh_token"] or
                str(result.get("token_type", "")).lower() != "bearer"):
            raise _error("auth", "ChatGPT вернул неполные учётные данные")
        try:
            expiry = float(result["expires_in"])
            if not math.isfinite(expiry) or expiry <= 0:
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise _error("auth", "ChatGPT вернул некорректное время действия входа") from None
        id_token = result.get("id_token")
        if id_token:
            claims = self._validate_id(id_token, client_id, nonce, previous.get("subject"),
                                       require_nonce=not bool(previous.get("access_token")))
        elif previous.get("subject") and previous.get("access_token"):
            id_token = previous.get("id_token")
            claims = {"sub": previous["subject"], "email": previous.get("email", "")}
        else:
            raise _error("auth", "ChatGPT не вернул ID token для проверки аккаунта")
        record = {"client_id": client_id, "ext_agent_host_id": host_id, "issuer": ISSUER,
                  "subject": claims["sub"], "email": claims.get("email", ""), "nonce": nonce,
                  "access_token": result["access_token"], "refresh_token": result["refresh_token"],
                  "id_token": id_token, "token_type": "Bearer", "scopes": scopes,
                  "expires_at": self.clock() + expiry, "saved_at": self.clock()}
        earliest = result.get("earliest_refresh_at")
        if isinstance(earliest, (int, float)) and math.isfinite(earliest):
            record["earliest_refresh_at"] = earliest
        return record

    def _begin_login(self, redirect_uri):
        with self._locked():
            record = self._load()
            host_id = self._host(record.get("ext_agent_host_id"))
        attempt = _Attempt(secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64),
                           redirect_uri, record.get("client_id") or INITIAL_CLIENT, host_id,
                           record.get("subject"))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(attempt.verifier.encode()).digest()).rstrip(b"=").decode()
        params = {"client_id": attempt.client_id, "ext_agent_host_id": host_id, "response_type": "code",
                  "redirect_uri": redirect_uri, "scope": SCOPES, "resource": RESOURCE, "state": attempt.state,
                  "nonce": attempt.nonce, "code_challenge_method": "S256", "code_challenge": challenge}
        if attempt.client_id == INITIAL_CLIENT:
            params["agent_name_hint"] = AGENT_NAME
        else:
            if record.get("id_token"):
                params["id_token_hint"] = record["id_token"]
            if record.get("email"):
                params["login_hint"] = record["email"]
        return attempt, self._discovery()["authorization_endpoint"] + "?" + urlencode(params)

    def _complete_login(self, query, attempt):
        # Validate even error callbacks, before persisting any supplied client ID.
        if any(len(values) != 1 for values in query.values()) or not secrets.compare_digest(
                query.get("state", [""])[0].encode("utf-8"), attempt.state.encode("utf-8")):
            raise _error("auth", "ChatGPT: callback не соответствует попытке входа")
        if query.get("error"):
            raise _error("auth", "Вход ChatGPT отменён или разрешение не предоставлено")
        client_id = query.get("client_id", [attempt.client_id])[0]
        if not client_id or client_id == INITIAL_CLIENT or (attempt.client_id != INITIAL_CLIENT and
                                                           client_id != attempt.client_id):
            raise _error("auth", "ChatGPT: callback содержит другую или неполную регистрацию")
        code = query.get("code", [""])[0]
        if not code:
            raise _error("auth", "ChatGPT: callback не содержит код авторизации")
        with self._locked():
            previous = self._load()
            if previous.get("client_id") not in (None, client_id):
                raise _error("auth", "Выбранная регистрация ChatGPT изменилась; повторите вход")
            if attempt.client_id == INITIAL_CLIENT:
                # Retain issued registration even when exchange fails; never persist the code.
                self._save({"client_id": client_id, "ext_agent_host_id": attempt.host_id})
            result = self._post_token({"grant_type": "authorization_code", "client_id": client_id,
                                       "code": code, "code_verifier": attempt.verifier,
                                       "redirect_uri": attempt.redirect_uri, "resource": RESOURCE})
            # Reauthorization always requires a newly validated ID token, even if old tokens exist.
            selected = {"subject": attempt.subject} if attempt.subject else {}
            record = self._credentials(result, client_id, attempt.host_id, attempt.nonce, selected)
            self._save(record)
        return self.status()

    def access_token(self) -> str:
        with self._locked():
            record = self._load()
            if DIRECT_SCOPE not in record.get("scopes", []) or not record.get("subject"):
                raise _error("auth", "Выполните вход: python -m listener.chatgpt_auth login")
            now = self.clock()
            if record.get("access_token") and record.get("expires_at", 0) > now + _REFRESH_MARGIN:
                return record["access_token"]
            if record.get("earliest_refresh_at", 0) > now:
                if record.get("access_token") and record.get("expires_at", 0) > now:
                    return record["access_token"]
                raise _error("network", "Обновление входа ChatGPT пока недоступно", record["earliest_refresh_at"])
            if not record.get("refresh_token"):
                raise _error("auth", "ChatGPT: требуется повторный вход")
            try:
                result = self._post_token({"grant_type": "refresh_token", "client_id": record["client_id"],
                                           "refresh_token": record["refresh_token"], "resource": RESOURCE})
            except _TerminalRefreshError:
                self._save({k: v for k, v in record.items() if k in
                            ("client_id", "ext_agent_host_id", "issuer", "subject", "email")})
                raise
            refreshed = self._credentials(result, record["client_id"], record["ext_agent_host_id"],
                                          record.get("nonce"), record)
            self._save(refreshed)
            return refreshed["access_token"]

    def status(self) -> dict:
        """Local metadata only; no network calls, refreshes, or credential fields."""
        try:
            record = self._load()
        except (ProviderError, OSError):
            return {"provider": "chatgpt", "ready": False, "reason": "protected_storage_unavailable"}
        granted = DIRECT_SCOPE in record.get("scopes", [])
        usable = bool(granted and record.get("subject") and
                      (record.get("refresh_token") or (record.get("access_token") and
                                                       record.get("expires_at", 0) > self.clock())))
        return {"provider": "chatgpt", "ready": usable, "reason": "signed_in" if usable else "login_required",
                "account": record.get("email") or record.get("subject") or "",
                "client_id": record.get("client_id") or "", "expires_at": record.get("expires_at")}

    def ready(self) -> bool:
        return self.status()["ready"]

    def login(self, timeout=180, opener=None):
        """Open system browser; receive exactly one state-bound loopback callback."""
        auth = self
        completed = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Callback URLs contain OAuth codes; never log HTTP request targets.

            def do_GET(self):
                parts = urlsplit(self.path)
                query = parse_qs(parts.query, keep_blank_values=True)
                state = query.get("state", [""])
                if (parts.path != "/auth/callback" or len(state) != 1 or
                        not secrets.compare_digest(state[0].encode("utf-8"), attempt.state.encode("utf-8"))):
                    self.send_error(400, "Invalid callback")
                    return
                try:
                    completed.append(auth._complete_login(query, attempt))
                    message = b"ChatGPT sign-in completed. You can close this window."
                except ProviderError as exc:
                    completed.append(exc)
                    message = b"ChatGPT sign-in was not completed. Return to the application."
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(message)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(message)

        class Server(HTTPServer):
            def get_request(self):
                sock, address = super().get_request()
                sock.settimeout(5)
                return sock, address

        with Server(("127.0.0.1", 0), Handler) as server:
            server.timeout = 1
            redirect = f"http://127.0.0.1:{server.server_port}/auth/callback"
            attempt, url = self._begin_login(redirect)
            if not (opener or webbrowser.open)(url):
                raise _error("configuration", "Не удалось открыть системный браузер для входа ChatGPT")
            deadline = time.monotonic() + timeout
            while not completed and time.monotonic() < deadline:
                server.handle_request()
        if not completed:
            raise _error("auth", "Истекло время ожидания входа ChatGPT; повторите вход")
        if isinstance(completed[0], ProviderError):
            raise completed[0]
        return completed[0]


def main(argv=None):
    command = (argv or sys.argv[1:] or ["status"])[0]
    if command not in ("login", "status"):
        print("usage: python -m listener.chatgpt_auth login|status", file=sys.stderr)
        return 2
    auth = ChatGPTAuth(root=Path(__file__).resolve().parents[1])
    try:
        result = auth.login() if command == "login" else auth.status()
    except ProviderError as exc:
        print(f"ChatGPT [{exc.kind}]: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print("ChatGPT [configuration]: не удалось открыть локальное хранилище или браузер.", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
