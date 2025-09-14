# qrLoger.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
os.environ.setdefault("ZBAR_DEBUG", "0")

import argparse
import base64
import hashlib
import hmac
import json
import logging
import re
import struct
import sys
import time
from pathlib import Path
from typing import Dict, Optional, Tuple, List

import requests
from PIL import ImageGrab
from pyzbar.pyzbar import decode, ZBarSymbol

# protobuf (из пакета steam)
from steam.protobufs import steammessages_auth_pb2 as pb

# ---------- логгер ----------
LOG_FMT = "%(asctime)s | %(levelname)s | %(message)s"


def setup_logging(level_name: str = "INFO", log_file: Optional[str] = None, quiet: bool = False):
    level = getattr(logging, level_name.upper(), logging.INFO)
    if quiet and level < logging.WARNING:
        level = logging.WARNING

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(level=level, format=LOG_FMT, handlers=handlers)

    for noisy in ("urllib3", "requests", "PIL", "pyzbar", "steam"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, level))


# ---------- константы ----------
BASE_URL = "https://api.steampowered.com/IAuthenticationService/"
HTTP_TIMEOUT = 15
DEFAULT_QR_TIMEOUT = 30
DEFAULT_POLL_SECONDS = 90

# коды выхода процесса
RC_OK = 0
RC_FAIL_GENERIC = 1
RC_INVALID_PASSWORD = 2
RC_INVALID_HWND = 4

HTTPS = requests.Session()
HTTPS.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/127.0.0.0 Safari/537.36",
    "Accept-Language": "ru,en;q=0.9",
})

# ---------- HTTP helpers ----------
def http_post_input_pb(url: str, req_msg, params: Optional[Dict[str, str]] = None,
                       accept_proto: bool = True, bearer: Optional[str] = None) -> requests.Response:
    if not hasattr(req_msg, "SerializeToString"):
        raise TypeError("req_msg должен быть protobuf-экземпляром")
    data = {"input_protobuf_encoded": base64.b64encode(req_msg.SerializeToString()).decode("ascii")}
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/x-protobuf" if accept_proto else "application/json",
        "X-Requested-With": "com.valvesoftware.android.steam.community",
        "Origin": "https://steamcommunity.com",
        "Referer": "https://steamcommunity.com/",
    }
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    return HTTPS.post(url, headers=headers, params=params or {}, data=data, timeout=HTTP_TIMEOUT)


def http_post_form(url: str, data: Dict[str, str], *, accept_json: bool = True) -> requests.Response:
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if accept_json:
        headers["Accept"] = "application/json"
    return HTTPS.post(url, headers=headers, data=data, timeout=HTTP_TIMEOUT)


def http_post_json(url: str, data: Dict[str, object]) -> requests.Response:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://steamcommunity.com",
        "Referer": "https://steamcommunity.com/",
        "X-Requested-With": "com.valvesoftware.android.steam.community",
    }
    return HTTPS.post(url, headers=headers, json=data, timeout=HTTP_TIMEOUT)


# ---------- maFile ----------
def load_mafile_from_path(path: str) -> Tuple[Path, Dict]:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"maFile не найден: {p}")
    d = json.loads(p.read_text(encoding="utf-8"))
    logging.debug(f"Используем mafile: {p.name}, SteamID: {p.stem}")
    return p, d


def load_mafile_from_json_arg(txt: str) -> Dict:
    pp = Path(txt)
    if pp.exists():
        return json.loads(pp.read_text(encoding="utf-8"))
    return json.loads(txt)


def load_mafile_from_stdin() -> Dict:
    return json.load(sys.stdin)


def get_shared_secret_b64(ma: Dict) -> str:
    b64 = ma.get("shared_secret") or ma.get("sharedSecret")
    if not b64:
        raise SystemExit("В maFile отсутствует shared_secret")
    return b64


def get_shared_secret_bytes(ma: Dict) -> bytes:
    import base64 as _b64
    return _b64.b64decode(get_shared_secret_b64(ma))


def extract_existing_token(ma: Dict) -> Optional[str]:
    sess = ma.get("Session") or {}
    return sess.get("AccessToken") or sess.get("OAuthToken")


def set_tokens_in_ma(ma: Dict, access_token: Optional[str], refresh_token: Optional[str]) -> Tuple[Dict, bool]:
    changed = False
    sess = ma.setdefault("Session", {})
    if access_token and sess.get("AccessToken") != access_token:
        sess["AccessToken"] = access_token
        sess["OAuthToken"] = access_token
        changed = True
    if refresh_token and sess.get("RefreshToken") != refresh_token:
        sess["RefreshToken"] = refresh_token
        changed = True
    return ma, changed


def save_mafile(path: Optional[Path], ma: Dict):
    if not path:
        return
    txt = json.dumps(ma, ensure_ascii=False, indent=2)
    try:
        path.with_suffix(path.suffix + ".bak").write_text(txt, encoding="utf-8")
    except Exception:
        pass
    path.write_text(txt, encoding="utf-8")


# ---------- Steam TOTP ----------
STEAM_ALPHABET = b"23456789BCDFGHJKMNPQRTVWXY"

def _hmac_sha1(key: bytes, msg: bytes) -> bytes:
    import hashlib, hmac
    return hmac.new(key, msg, hashlib.sha1).digest()

def generate_steam_twofactor_code(shared_secret_b64: str, ts: Optional[int] = None) -> str:
    import base64 as _b64, struct as _st
    if ts is None:
        ts = int(time.time())
    time_block = _st.pack(">Q", ts // 30)
    key = _b64.b64decode(shared_secret_b64)
    h = _hmac_sha1(key, time_block)
    off = h[-1] & 0x0F
    code_int = _st.unpack(">I", h[off:off+4])[0] & 0x7FFFFFFF
    out = []
    for _ in range(5):
        out.append(STEAM_ALPHABET[code_int % len(STEAM_ALPHABET)])
        code_int //= len(STEAM_ALPHABET)
    return bytes(out).decode("ascii")


# ---------- JSON-credentials (новые токены) ----------
def _get_password_rsa_key_json(account_name: str) -> tuple[str, str, int]:
    url = BASE_URL + "GetPasswordRSAPublicKey/v1/"
    resp = HTTPS.get(url, params={"account_name": account_name}, timeout=HTTP_TIMEOUT,
                     headers={"Accept": "application/json"})
    resp.raise_for_status()
    r = resp.json().get("response", {})
    mod = r.get("publickey_mod")
    exp = r.get("publickey_exp")
    ts  = r.get("timestamp")
    if not (mod and exp and ts):
        raise RuntimeError(f"RSA key JSON malformed: {resp.text[:200]}")
    return mod, exp, int(ts)


def _rsa_encrypt_password(password: str, n_hex: str, e_hex: str) -> str:
    try:

        from Cryptodome.PublicKey import RSA
        from Cryptodome.Cipher import PKCS1_v1_5

    except Exception as e:
        raise RuntimeError("pycryptodome is required (pip install pycryptodome)") from e

    n = int(n_hex, 16); e = int(e_hex, 16)
    key = RSA.construct((n, e))
    cipher = PKCS1_v1_5.new(key)
    enc = cipher.encrypt(password.encode("utf-8"))
    import base64 as _b64
    return _b64.b64encode(enc).decode("ascii")


def _begin_auth_credentials_json(login: str, enc_pwd_b64: str, ts: int) -> tuple[int, bytes]:
    url = BASE_URL + "BeginAuthSessionViaCredentials/v1/"

    data_json = {
        "account_name": login,
        "encrypted_password": enc_pwd_b64,
        "encryption_timestamp": str(ts),
        "remember_login": True,
        "persistence": 1,
        "platform_type": 2,
        "website_id": "Community",
        "device_friendly_name": "python-qrauto"
    }
    try:
        rj = http_post_json(url, data_json)
        xres = rj.headers.get("x-eresult")
        try:
            body = rj.json()
        except Exception:
            body = {}
        resp = (body.get("response") or {})
        cid = resp.get("client_id")
        rid = resp.get("request_id")
        if cid and rid:
            import base64 as _b64, binascii
            try:
                rid_bytes = _b64.b64decode(rid)
            except Exception:
                rid_bytes = bytes.fromhex(rid)
            return int(cid), rid_bytes
        if xres == "5":
            logging.debug("BeginAuth(JSON) дал x-eresult=5 — пробую form-urlencoded…")
        else:
            logging.debug(f"BeginAuth(JSON) неполный ответ: {body} | x-eresult={xres} — пробую form-urlencoded…")
    except Exception as e:
        logging.debug(f"BeginAuth(JSON) исключение {e} — пробую form-urlencoded…")

    data_form = {
        "account_name": login,
        "encrypted_password": enc_pwd_b64,
        "encryption_timestamp": str(ts),
        "remember_login": "true",
        "persistence": "1",
        "platform_type": "2",
        "website_id": "Community",
        "device_friendly_name": "python-qrauto"
    }
    rf = http_post_form(url, data_form, accept_json=True)
    xres2 = rf.headers.get("x-eresult")
    try:
        body2 = rf.json()
    except Exception:
        body2 = {}

    resp2 = (body2.get("response") or {})
    cid2 = resp2.get("client_id")
    rid2 = resp2.get("request_id")
    if not (cid2 and rid2):
        raise RuntimeError(f"BeginAuth(form) malformed: {json.dumps(body2)[:200]} | x-eresult={xres2}")

    import base64 as _b64
    try:
        rid_bytes2 = _b64.b64decode(rid2)
    except Exception:
        rid_bytes2 = bytes.fromhex(rid2)
    return int(cid2), rid_bytes2


def update_with_steamguard_code(steamid: int, client_id: int, code: str, token: Optional[str]) -> Tuple[bool, str]:
    req = pb.CAuthentication_UpdateAuthSessionWithSteamGuardCode_Request()
    req.client_id = client_id
    req.steamid = int(steamid)
    req.code = code
    req.code_type = 3  # DeviceCode (TOTP)
    params = {"access_token": token} if token else {}
    resp = http_post_input_pb(BASE_URL + "UpdateAuthSessionWithSteamGuardCode/v1/", req, params=params,
                              accept_proto=True, bearer=(token or None))
    ok = (resp.status_code == 200)
    dbg = f"UpdateAuthSessionWithSteamGuardCode: HTTP {resp.status_code}"
    if not ok:
        dbg += f" | {resp.text[:200]}"
    return ok, dbg


def poll_auth_status(client_id: int, request_id: Optional[bytes], *, debug_payload: bool = False,
                     bearer_token: Optional[str] = None) -> Tuple[bool, Dict, str, str]:
    req = pb.CAuthentication_PollAuthSessionStatus_Request()
    req.client_id = client_id
    if request_id:
        req.request_id = request_id
    import base64 as _b64
    payload_b64 = _b64.b64encode(req.SerializeToString()).decode("ascii")

    def _do_call(use_bearer: bool):
        resp = http_post_input_pb(
            BASE_URL + "PollAuthSessionStatus/v1/",
            req,
            params={},
            accept_proto=True,
            bearer=(bearer_token if use_bearer else None),
        )
        ok = (resp.status_code == 200)
        out: Dict = {}
        dbg = f"PollAuthSessionStatus: HTTP {resp.status_code}"
        if debug_payload:
            dbg += f" | input_protobuf_encoded={payload_b64}"
        if ok:
            try:
                msg = pb.CAuthentication_PollAuthSessionStatus_Response()
                msg.ParseFromString(resp.content)
                if getattr(msg, "access_token", None):
                    out["access_token"] = msg.access_token
                if getattr(msg, "refresh_token", None):
                    out["refresh_token"] = msg.refresh_token
                if hasattr(msg, "had_remote_interaction"):
                    out["had_remote_interaction"] = bool(msg.had_remote_interaction)
                if getattr(msg, "new_client_id", None):
                    out["new_client_id"] = int(msg.new_client_id)
                if getattr(msg, "new_challenge_url", None):
                    out["new_challenge_url"] = msg.new_challenge_url
                tail = []
                if "access_token" in out: tail.append("access_token")
                if "refresh_token" in out: tail.append("refresh_token")
                if "had_remote_interaction" in out: tail.append(f"had_remote_interaction={out['had_remote_interaction']}")
                if "new_client_id" in out: tail.append(f"new_client_id={out['new_client_id']}")
                if "new_challenge_url" in out: tail.append("new_challenge_url")
                dbg += " | " + (", ".join(tail) if tail else "no_fields")
            except Exception as e:
                dbg += f" | parse_err: {e}"
        else:
            dbg += f" | {resp.text[:200]}"
        return ok, out, dbg

    ok1, out1, dbg1 = _do_call(False)
    if ok1 and not out1 and bearer_token:
        ok2, out2, dbg2 = _do_call(True)
        return ok2, out2, dbg1 + " || " + dbg2, payload_b64
    return ok1, out1, dbg1, payload_b64


# ---------- QR / HWND helpers ----------
def find_qr_url(timeout: int) -> str:
    logging.debug("Ищу QR-код на экране...")
    t0 = time.time()
    while time.time() - t0 < timeout:
        img = ImageGrab.grab()
        codes = decode(img, symbols=[ZBarSymbol.QRCODE])
        if codes:
            qr = codes[0].data.decode("utf-8", errors="ignore")
            logging.info("QR найден: %s", qr)
            return qr
        logging.debug("Пока не нашёл QR на экране... (убедитесь, что окно Steam видно)")
        time.sleep(2)
    raise TimeoutError("Не удалось найти QR на экране")


def _hwnd_client_rect(hwnd: int) -> Tuple[int, int, int, int]:
    import win32gui
    l, t, r, b = win32gui.GetClientRect(hwnd)
    sx, sy = win32gui.ClientToScreen(hwnd, (0, 0))
    return (sx, sy, sx + max(1, r - l), sy + max(1, b - t))


def find_qr_in_hwnd(hwnd: int, timeout: int) -> str:
    try:
        rect = _hwnd_client_rect(hwnd)
    except Exception as e:
        logging.error(f"HWND невалиден/недоступен: {e}")
        raise SystemExit(RC_INVALID_HWND)

    logging.info(f"Сканирую QR внутри окна HWND=0x{hwnd:x} ({rect[0]},{rect[1]},{rect[2]},{rect[3]})...")
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            rect = _hwnd_client_rect(hwnd)
        except Exception as e:
            logging.error(f"HWND невалиден/недоступен: {e}")
            raise SystemExit(RC_INVALID_HWND)
        try:
            img = ImageGrab.grab(bbox=rect)
        except Exception as e:
            logging.error(f"Не удалось захватить регион окна: {e}")
            time.sleep(1.0)
            continue
        codes = decode(img, symbols=[ZBarSymbol.QRCODE])
        if codes:
            qr = codes[0].data.decode("utf-8", errors="ignore")
            logging.info("QR найден: %s", qr)
            return qr
        logging.debug("Пока не нашёл QR в окне... (убедитесь, что QR виден в выбранном окне)")
        time.sleep(1.5)
    raise TimeoutError("Не удалось найти QR в указанном окне")


def parse_qr(qr_url: str) -> Tuple[int, int]:
    m = re.search(r"https?://s\.team/q/(\d+)/(\d+)", qr_url)
    if not m:
        raise ValueError(f"Некорректный QR-URL: {qr_url}")
    return int(m.group(1)), int(m.group(2))


def _is_hwnd_alive(hwnd: int) -> bool:
    try:
        import win32gui
        return bool(win32gui.IsWindow(hwnd))
    except Exception:
        return True  # если нет win32gui — считаем живым, чтобы не ломать флоу


def _is_qr_visible_in_hwnd(hwnd: int) -> bool:
    try:
        rect = _hwnd_client_rect(hwnd)
        img = ImageGrab.grab(bbox=rect)
        codes = decode(img, symbols=[ZBarSymbol.QRCODE])
        return bool(codes)
    except Exception:
        return False


def rescan_qr_client_id_if_changed(login_hwnd: Optional[int], cur_client_id: int, *, tries: int = 6, delay: float = 0.5) -> int:
    if not login_hwnd:
        return cur_client_id
    try:
        rect = _hwnd_client_rect(login_hwnd)
    except Exception:
        return cur_client_id

    for _ in range(max(1, tries)):
        try:
            img = ImageGrab.grab(bbox=rect)
            codes = decode(img, symbols=[ZBarSymbol.QRCODE])
            if codes:
                url = codes[0].data.decode("utf-8", errors="ignore")
                m = re.search(r"https?://s\.team/q/(\d+)/(\d+)", url)
                if m:
                    new_cid = int(m.group(2))
                    if new_cid != cur_client_id:
                        logging.debug(f"QR challenge обновился: {cur_client_id} → {new_cid}")
                        return new_cid
        except Exception:
            pass
        time.sleep(delay)
    return cur_client_id


# ---------- основной флоу ----------
def parse_hwnd_value(s: str) -> int:
    s = s.strip()
    try:
        return int(s, 0)  # поймёт 0x...
    except ValueError:
        try:
            return int(s, 16)  # голый hex
        except ValueError:
            return int(s)      # десятичный


def ensure_request_id_after_approve(cur_client_id: int, token: str, initial_request_id: Optional[bytes]) -> Optional[bytes]:
    if initial_request_id:
        return initial_request_id
    deadline = time.time() + 30.0
    rid = None
    while time.time() < deadline and not rid:
        okb, rid_try, dbg, code = get_auth_session_info(cur_client_id, token)
        logging.debug(f"ensure_request_id: {dbg}")
        if okb and rid_try:
            rid = rid_try
            break
        time.sleep(0.5)
    return rid


def update_with_mobile_confirm(steamid: int, version: int, client_id: int,
                               secret: bytes, token: str) -> Tuple[bool, str, int]:
    req = pb.CAuthentication_UpdateAuthSessionWithMobileConfirmation_Request()
    req.steamid = int(steamid)
    req.version = version
    req.client_id = client_id
    req.signature = hmac.new(secret, struct.pack("<IQQ", version, client_id, steamid), hashlib.sha1).digest()
    req.confirm = True
    req.persistence = 1
    resp = http_post_input_pb(BASE_URL + "UpdateAuthSessionWithMobileConfirmation/v1/", req,
                              params={"access_token": token}, accept_proto=True, bearer=token)
    ok = (resp.status_code == 200)
    dbg = f"Approve[mobile]: HTTP {resp.status_code}"
    if not ok:
        dbg += f" | {resp.text[:200]}"
    return ok, dbg, resp.status_code


def get_auth_session_info(client_id: int, token: str) -> Tuple[bool, Optional[bytes], str, int]:
    req = pb.CAuthentication_GetAuthSessionInfo_Request()
    req.client_id = client_id
    resp = http_post_input_pb(BASE_URL + "GetAuthSessionInfo/v1/", req,
                              params={"access_token": token}, accept_proto=True, bearer=token)
    ok = (resp.status_code == 200)
    request_id = None
    dbg = f"GetAuthSessionInfo: HTTP {resp.status_code}"
    if ok:
        try:
            msg = pb.CAuthentication_GetAuthSessionInfo_Response()
            msg.ParseFromString(resp.content)
            if getattr(msg, "request_id", None):
                request_id = msg.request_id
                dbg += " | request_id OK | " + request_id.hex()
        except Exception as e:
            dbg += f" | parse_err: {e}"
    else:
        dbg += f" | {resp.text[:200]}"
    return ok, request_id, dbg, resp.status_code


def do_flow(*,
            qr_url: Optional[str],
            poll_payload_b64: Optional[str],
            mafile_path: Optional[Path],
            save_to: Optional[Path],
            ma: Dict,
            login: str,
            password: str,
            access_token: str,
            poll_seconds: int,
            exit_on_interaction: bool,
            debug_payload: bool,
            login_hwnd: Optional[int],
            exit_on_window_close: bool,
            exit_on_qr_disappear: bool,
            qr_disappear_consecutive: int,
            qr_recheck_interval: float) -> Tuple[bool, Dict, Dict]:

    import base64 as _b64

    secret = get_shared_secret_bytes(ma)
    steamid = int(ma.get("steamid") or ma.get("Session", {}).get("SteamID") or 0)

    request_id_override = None
    version = 1
    client_id = None

    if poll_payload_b64:
        def _parse_varint(buf: bytes, i: int) -> Tuple[int, int]:
            shift = 0; val = 0
            while i < len(buf):
                b = buf[i]; i += 1
                val |= (b & 0x7F) << shift
                if (b & 0x80) == 0: break
                shift += 7
            return val, i

        def _parse_poll_payload_b64(b64: str) -> Tuple[Optional[int], Optional[bytes]]:
            try:
                raw = _b64.b64decode(b64)
            except Exception:
                return None, None
            i = 0; cid = None; rid = None
            while i < len(raw):
                key, i = _parse_varint(raw, i)
                field, wtype = key >> 3, key & 7
                if wtype == 0:
                    val, i = _parse_varint(raw, i)
                    if field == 1: cid = val
                elif wtype == 2:
                    ln, i = _parse_varint(raw, i)
                    chunk = raw[i:i+ln]; i += ln
                    if field == 2: rid = chunk
                elif wtype == 1: i += 8
                elif wtype == 5: i += 4
                else: break
            return cid, rid

        cid, rid = _parse_poll_payload_b64(poll_payload_b64)
        if cid: client_id = cid
        if rid:
            request_id_override = rid
            logging.debug(f"Использую request_id из --poll-payload: {rid.hex()} (client_id={client_id})")

    if client_id is None:
        if not qr_url:
            qr_url = find_qr_url(timeout=DEFAULT_QR_TIMEOUT)
        version, client_id = parse_qr(qr_url)
        logging.debug(f"Парсинг challenge: version={version} client_id={client_id}")

    def approve_with(token: str) -> Tuple[bool, str, int]:
        return update_with_mobile_confirm(steamid, version, client_id, secret, token)

    def acquire_tokens_via_credentials(login: str, password: str, ma: dict) -> tuple[Optional[str], Optional[str], dict]:
        mod, exp, ts = _get_password_rsa_key_json(login)
        enc_b64 = _rsa_encrypt_password(password, mod, exp)
        client_id_c, request_id_c = _begin_auth_credentials_json(login, enc_b64, ts)
        logging.debug(f"BeginAuth(JSON): client_id={client_id_c}, request_id={request_id_c.hex()}")
        try:
            code = generate_steam_twofactor_code(get_shared_secret_b64(ma))
            ok_code, dbg_code = update_with_steamguard_code(steamid, client_id_c, code, token=None)
            logging.debug(dbg_code + " | via-credentials")
        except Exception:
            pass

        t0 = time.time()
        last = {}
        while time.time() - t0 < 120:
            ok, data, dbg, _ = poll_auth_status(client_id_c, request_id_c, debug_payload=debug_payload)
            logging.debug(dbg)
            if data.get("access_token") or data.get("refresh_token"):
                return data.get("access_token"), data.get("refresh_token"), data
            last = data or last
            time.sleep(1.0)
        logging.error(f"Credentials(JSON): не дождался токенов (last={last})")
        return None, None, last

    def run_cycle(token: str) -> Tuple[bool, Dict]:
        nonlocal client_id, version

        # bind
        ok_bind, request_id, dbg_bind, code = get_auth_session_info(client_id, token)
        logging.info(dbg_bind)
        if code == 401:
            logging.info("GetAuthSessionInfo: 401 — получаю новый токен через JSON-credentials…")
            try:
                new_at, new_rt, snap = acquire_tokens_via_credentials(login, password, ma)
                if not new_at:
                    return False, {"error": "credentials-json failed", "snap": snap}
                ma2, _ = set_tokens_in_ma(ma, new_at, new_rt)
                save_mafile(mafile_path or save_to, ma2)
                token = new_at
                time.sleep(0.6)
                client_id = rescan_qr_client_id_if_changed(login_hwnd, client_id)
                ok_bind, request_id, dbg_bind, code = get_auth_session_info(client_id, token)
                logging.info(dbg_bind)
                if code != 200:
                    return False, {"error": f"bind_after_credentials_http_{code}"}
            except Exception as e:
                return False, {"error": f"credentials-json exception: {e}"}

        if request_id_override:
            request_id = request_id_override
            logging.debug(f"Перезаписал request_id из браузера: {request_id.hex()}")

        # approve
        ok_upd, dbg_upd, code2 = approve_with(token)
        logging.info(dbg_upd)
        if code2 == 401:
            logging.info("Approve: 401 — получаю новый токен через JSON-credentials…")
            try:
                new_at, new_rt, snap = acquire_tokens_via_credentials(login, password, ma)
                if not new_at:
                    return False, {"error": "credentials-json failed on approve", "snap": snap}
                ma2, _ = set_tokens_in_ma(ma, new_at, new_rt)
                save_mafile(mafile_path or save_to, ma2)
                token = new_at
                time.sleep(0.6)
                client_id = rescan_qr_client_id_if_changed(login_hwnd, client_id)
                ok_upd, dbg_upd, code2 = approve_with(token)
                logging.info("Approve(after credentials): " + dbg_upd)
                if code2 != 200:
                    return False, {"error": f"approve_http_{code2}"}
            except Exception as e:
                return False, {"error": f"credentials-json exception on approve: {e}"}
        elif not ok_upd:
            return False, {"error": "approve_failed"}

        # ждём request_id до 30с
        request_id = ensure_request_id_after_approve(client_id, token, request_id)
        logging.debug(f"QR poll starts: client_id={client_id}, request_id={(request_id.hex() if request_id else 'none')}")

        # QR-poll + наблюдение за окном/QR
        start = time.time()
        last_progress = start
        last_snapshot: Dict = {}
        saw_interaction = False
        last_totp_epoch = None
        last_reapprove_ts = 0.0
        last_rebind_ts = 0.0

        qr_absent_streak = 0
        last_qr_check_ts = 0.0

        def push_totp_now():
            nonlocal last_totp_epoch
            now = int(time.time())
            epoch = now // 30
            if last_totp_epoch is None or epoch != last_totp_epoch:
                try:
                    code = generate_steam_twofactor_code(get_shared_secret_b64(ma), now)
                    _ok1, dbg1 = update_with_steamguard_code(steamid, client_id, code, token)
                    logging.debug(dbg1 + " | push TOTP (bearer)")
                    _ok2, dbg2 = update_with_steamguard_code(steamid, client_id, code, None)
                    logging.debug(dbg2 + " | push TOTP (no-bearer)")
                    last_totp_epoch = epoch
                except Exception:
                    pass

        push_totp_now()

        while True:
            # 1) «внешние» условия выхода
            if login_hwnd and exit_on_window_close and not _is_hwnd_alive(login_hwnd):
                logging.info("Окно логина закрыто — завершаю успехом (finish_mode=window_closed).")
                return True, {"finish_mode": "window_closed", **last_snapshot}

            now_ts = time.time()
            if login_hwnd and exit_on_qr_disappear and (now_ts - last_qr_check_ts) >= max(0.1, qr_recheck_interval):
                last_qr_check_ts = now_ts
                if _is_qr_visible_in_hwnd(login_hwnd):
                    qr_absent_streak = 0
                else:
                    qr_absent_streak += 1
                    logging.debug(f"[QR] not visible streak = {qr_absent_streak}/{qr_disappear_consecutive}")
                    if qr_absent_streak >= max(1, qr_disappear_consecutive):
                        logging.info("QR исчез из окна — завершаю успехом (finish_mode=qr_disappeared).")
                        return True, {"finish_mode": "qr_disappeared", **last_snapshot}

            # 2) таймаут прогресса
            if time.time() - last_progress > poll_seconds:
                logging.error(f"QR-сессия: нет прогресса {poll_seconds}s. last={last_snapshot}")
                if exit_on_interaction and saw_interaction:
                    return True, {"finish_mode": "had_remote_interaction_timeout", **last_snapshot}
                return False, last_snapshot

            # 3) poll
            ok_poll, data, dbg_poll, _ = poll_auth_status(client_id, request_id, debug_payload=debug_payload, bearer_token=token)
            logging.debug(f"[QR] {dbg_poll}")

            if ok_poll:
                last_progress = time.time()

            if data:
                if data.get("had_remote_interaction") is True:
                    saw_interaction = True
                last_snapshot.update(data)

            if exit_on_interaction and data.get("had_remote_interaction") is True:
                logging.debug("QR-сессия: had_remote_interaction=True — успех для клиента.")
                return True, {"finish_mode": "qr_interaction", **last_snapshot}

            if data.get("access_token") or data.get("refresh_token"):
                ma2, _ = set_tokens_in_ma(ma, data.get("access_token"), data.get("refresh_token"))
                save_mafile(mafile_path or save_to, ma2)
                return True, {"finish_mode": "qr_tokens", **last_snapshot}

            if any(k in data for k in ("access_token", "refresh_token", "new_client_id", "new_challenge_url")) or \
               data.get("had_remote_interaction") is True:
                last_progress = time.time()

            if saw_interaction:
                push_totp_now()

            # keepalive approve каждые 10с
            if time.time() - last_reapprove_ts > 10:
                _ok_upd2, dbg2, _ = approve_with(token)
                logging.debug("[QR] keepalive approve: " + dbg2)
                last_reapprove_ts = time.time()

            # если request_id нет — каждые 10с пытаемся восстановить
            if request_id is None and (time.time() - last_rebind_ts > 10.0):
                new_cid = rescan_qr_client_id_if_changed(login_hwnd, client_id)
                if new_cid != client_id:
                    client_id = new_cid
                okb2, rid2, db2, _ = get_auth_session_info(client_id, token)
                logging.debug("[QR] rebind (no rid): " + db2)
                if okb2 and rid2:
                    request_id = rid2
                _ok_upd2, dbg2, _ = approve_with(token)
                logging.debug("[QR] re-approve (no rid): " + dbg2)
                last_rebind_ts = time.time()
                continue

            # ротация server-side
            if data.get("new_challenge_url") or data.get("new_client_id"):
                try:
                    if data.get("new_challenge_url"):
                        v2, cid2 = parse_qr(data["new_challenge_url"])
                        version, client_id = v2, cid2
                    elif data.get("new_client_id"):
                        client_id = int(data["new_client_id"])
                    okb2, rid2, db2, _ = get_auth_session_info(client_id, token)
                    logging.debug("[QR] " + db2)
                    if okb2 and rid2 and not request_id_override:
                        request_id = rid2
                    _ok_upd2, dbg2, _ = approve_with(token)
                    logging.debug("[QR] re-approve (rotated): " + dbg2)
                    last_progress = time.time()
                except Exception as e:
                    logging.warning(f"[QR] ротация: не удалось переодобрить: {e}")

            time.sleep(1)

    ok, snap = run_cycle(access_token)
    return ok, snap, ma


# ---------- CLI ----------
def parse_cli() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Steam QR Approver (JSON credentials fallback, без protobuf refresh)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--mafile", help="путь к maFile")
    g.add_argument("--mafile-json", help="путь к JSON или inline JSON строка")
    g.add_argument("--stdin-mafile", action="store_true", help="читать mafile JSON из STDIN")

    p.add_argument("--login", required=True)
    p.add_argument("--password", required=True)

    p.add_argument("--qr", help="QR-URL https://s.team/q/<ver>/<client_id>")
    p.add_argument("--poll-payload", help="base64 input_protobuf_encoded из DevTools")

    # HWND / окно
    p.add_argument("--hwnd", "--hwid", dest="hwnd",
                   help="HWND окна (hex или int), чтобы сканировать/наблюдать только его")

    # тайминги
    p.add_argument("--timeout", type=int, default=DEFAULT_QR_TIMEOUT)
    p.add_argument("--poll-seconds", type=int, default=DEFAULT_POLL_SECONDS)

    # поведения успеха
    p.add_argument("--wait-tokens", action="store_true",
                   help="ждать именно выдачи токенов (иначе успех по had_remote_interaction)")
    p.add_argument("--force-login", action="store_true",
                   help="игнорировать токен из mafile и сразу обновить через JSON-credentials")

    # НОВЫЕ флаги наблюдения за окном/QR:
    p.add_argument("--exit-on-window-close", action="store_true",
                   help="завершить успехом, если окно (HWND) закрылось")
    p.add_argument("--exit-on-qr-disappear", action="store_true",
                   help="завершить успехом, если QR исчез из окна")
    p.add_argument("--qr-disappear-consecutive", type=int, default=3,
                   help="сколько подряд проверок без QR нужно для успеха (по умолчанию 3)")
    p.add_argument("--qr-recheck-interval", type=float, default=0.75,
                   help="период проверки наличия QR в окне (сек)")

    # логирование
    p.add_argument("--log-level", default="INFO",
                   choices=["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"])
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--log-file")
    p.add_argument("--debug-payload", action="store_true")

    return p.parse_args()


def login():
    try:
        args = parse_cli()
        setup_logging(args.log_level, args.log_file, args.quiet)

        # загрузка ma
        mafile_path: Optional[Path] = None
        if args.mafile:
            mafile_path, ma = load_mafile_from_path(args.mafile)
        elif args.mafile_json:
            ma = load_mafile_from_json_arg(args.mafile_json)
            logging.debug("mafile принят как JSON (без чтения файла)")
        else:
            ma = load_mafile_from_stdin()
            logging.debug("mafile принят из STDIN (JSON)")

        # сверка логина
        acc = (ma.get("account_name") or ma.get("accountName") or "").strip()
        if not acc:
            logging.error("В maFile отсутствует account_name — не могу сверить с логином.")
            raise SystemExit(RC_FAIL_GENERIC)
        if acc.lower() != args.login.strip().lower():
            logging.error(f"Логин '{args.login}' != account_name в mafile '{acc}'")
            raise SystemExit(RC_FAIL_GENERIC)

        at = extract_existing_token(ma)

        # при необходимости — обновим токены через JSON-credentials
        if args.force_login or not at:
            try:
                mod, exp, ts = _get_password_rsa_key_json(args.login)
                enc_b64 = _rsa_encrypt_password(args.password, mod, exp)
                cid, rid = _begin_auth_credentials_json(args.login, enc_b64, ts)
                logging.info(f"BeginAuth(JSON): client_id={cid}, request_id={rid.hex()}")
                try:
                    code = generate_steam_twofactor_code(get_shared_secret_b64(ma))
                    ok_code, dbg_code = update_with_steamguard_code(int(ma.get("steamid") or ma.get("Session", {}).get("SteamID") or 0), cid, code, token=None)
                    logging.debug(dbg_code + " | via-credentials")
                except Exception:
                    pass
                t0 = time.time(); last = {}
                while time.time() - t0 < 120:
                    ok, data, dbg, _ = poll_auth_status(cid, rid, debug_payload=args.debug_payload)
                    logging.debug(dbg)
                    if data.get("access_token") or data.get("refresh_token"):
                        at, rt = data.get("access_token"), data.get("refresh_token")
                        ma, _ = set_tokens_in_ma(ma, at, rt)
                        save_mafile(mafile_path, ma)
                        break
                    last = data or last
                    time.sleep(1.0)
                if not extract_existing_token(ma):
                    logging.error(f"Не удалось получить токены через JSON-credentials. last={last}")
                    raise SystemExit(RC_FAIL_GENERIC)
                logging.info("Токены обновлены через JSON-credentials.")
                at = extract_existing_token(ma)
            except Exception as e:
                msg = str(e)
                if "x-eresult=5" in msg or "InvalidPassword" in msg:
                    logging.error(f"JSON-credentials ошибка: InvalidPassword ({msg})")
                    raise SystemExit(RC_INVALID_PASSWORD)
                logging.error(f"JSON-credentials ошибка: {e}")
                raise SystemExit(RC_FAIL_GENERIC)

        # QR источник: hwnd > poll-payload > qr > поиск на экране
        qr_url = args.qr
        if args.poll_payload:
            pass
        elif args.hwnd:
            hwnd = parse_hwnd_value(args.hwnd)
            try:
                qr_url = find_qr_in_hwnd(hwnd, timeout=args.timeout)
            except SystemExit:
                raise
            except TimeoutError as te:
                logging.error(str(te))
                raise SystemExit(RC_FAIL_GENERIC)
            except Exception as e:
                logging.error(f"Ошибка сканирования QR в окне: {e}")
                raise SystemExit(RC_FAIL_GENERIC)
        else:
            try:
                qr_url = find_qr_url(timeout=args.timeout)
            except TimeoutError as te:
                logging.error(str(te))
                raise SystemExit(RC_FAIL_GENERIC)

        ok, snapshot, updated_ma = do_flow(
            qr_url=qr_url,
            poll_payload_b64=args.poll_payload,
            mafile_path=mafile_path,
            save_to=mafile_path,
            ma=ma,
            login=args.login,
            password=args.password,
            access_token=at,
            poll_seconds=args.poll_seconds,
            exit_on_interaction=(not args.wait_tokens),
            debug_payload=args.debug_payload,
            login_hwnd=parse_hwnd_value(args.hwnd) if args.hwnd else None,
            exit_on_window_close=bool(args.exit_on_window_close),
            exit_on_qr_disappear=bool(args.exit_on_qr_disappear),
            qr_disappear_consecutive=int(args.qr_disappear_consecutive),
            qr_recheck_interval=float(args.qr_recheck_interval),
        )

        save_mafile(mafile_path, updated_ma)

        if ok:
            logging.info("Готово.")
            redacted = {k: (str(v)[:16] + "..." if isinstance(v, str) else v) for k, v in (snapshot or {}).items()}
            logging.info(f"Итоговое состояние (masked): {redacted}")
            raise SystemExit(RC_OK)
        else:
            snap_txt = json.dumps(snapshot, ensure_ascii=False)
            if "x-eresult=5" in snap_txt or "InvalidPassword" in snap_txt:
                logging.error(f"Не получилось завершить (InvalidPassword). Итоговое состояние: {snapshot}")
                raise SystemExit(RC_INVALID_PASSWORD)
            logging.error(f"Не получилось завершить. Итоговое состояние: {snapshot}")
            raise SystemExit(RC_FAIL_GENERIC)

    except SystemExit as se:
        sys.exit(se.code if isinstance(se.code, int) else RC_FAIL_GENERIC)
    except Exception as e:
        logging.error(f"Фатальная ошибка: {e}")
        sys.exit(RC_FAIL_GENERIC)


if __name__ == "__main__":
    login()
