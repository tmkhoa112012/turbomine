#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py — Bản web của TurboMines auto-bot (turbogg4u), deploy trên Render.

KIẾN TRÚC BẢO VỆ SOURCE:
- Toàn bộ logic cược (TurboMinesEngine: tạo round, mở ô, đoán thua, cashout,
  header/endpoint thật của turbogg4u...) CHỈ CHẠY TRÊN SERVER, không bao giờ
  gửi xuống trình duyệt. Trình duyệt chỉ nhận HTML/CSS/JS của giao diện +
  vài dòng log text qua /api/logs — không có 1 dòng nào của thuật toán cược.
- Mọi endpoint /api/* đều đòi hỏi session đã xác thực key (cookie ký bằng
  SECRET_KEY, không đoán được) — ai dùng `requests` gọi thẳng /api/* mà
  không qua bước xin key + nhập đúng key sẽ luôn nhận 401, không lấy được
  gì có ích.
- LƯU Ý THẬT: không có cách nào chặn tuyệt đối việc ai đó dùng `requests`
  để GET trang HTML/JS công khai — trình duyệt về bản chất cũng chỉ là 1
  HTTP client. Cái thực sự bảo vệ được là: (1) logic cược không hề nằm
  trong phần gửi cho trình duyệt, và (2) API không trả dữ liệu có nghĩa
  nếu không có session hợp lệ. Xem thêm phần "GIỚI HẠN" cuối file.
"""

import json
import os
import random
import secrets
import string
import threading
import time
import uuid
from functools import wraps

import requests
from flask import Flask, jsonify, render_template, request, session

# ============================================================
# CẤU HÌNH
# ============================================================
BASE_URL = "https://turbomines.turbogg4u.online"

MAP_CONFIG = {
    "3x3": {"deskSize": 9, "max_mines": 7},
    "5x5": {"deskSize": 25, "max_mines": 10},
    "7x7": {"deskSize": 49, "max_mines": 12},
    "9x9": {"deskSize": 81, "max_mines": 20},
}

HEADERS_COMMON = {
    "Content-Type": "application/json",
    "Origin": BASE_URL,
    "Referer": BASE_URL + "/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
}

LOSS_KEYWORDS = ("mine", "bomb", "boom", "lost", "explode", "dead", "fail")

KDZ_KEY_API_KEY = "fe20c77ce5b20a0266c527f021a76ee4"
KDZ_KEY_API_URL = "https://shoptdmmo.store/api/notes.php"
LINK4M_API_TOKEN = "67ff9a2f5b14c24e264778b0"
LINK4M_API_URL = "https://link4m.co/api-shorten/v2"
KDZ_KEY_TTL_SECONDS = 24 * 3600
KDZ_GAME_NAME = "turbomines-web"

RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX = 8   # toi da 8 request/60s cho moi IP tren cac endpoint nhay cam

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

# ============================================================
# TRẠNG THÁI SERVER-SIDE (theo session, KHÔNG bao giờ gửi xuống client)
# Production nhiều instance thì thay bằng Redis; ở quy mô 1 dyno Render
# dict trong RAM là đủ.
# ============================================================
_STATE_LOCK = threading.Lock()
SESSIONS = {}        # sid -> {"authed":bool,"expire":ts,"pending_key":str,"pending_expire":ts}
ENGINES = {}          # sid -> TurboMinesEngine instance đang chạy (nếu có)
_RATE = {}            # ip -> [timestamps]


def _sid():
    if "sid" not in session:
        session["sid"] = uuid.uuid4().hex
    return session["sid"]


def _get_state(sid):
    with _STATE_LOCK:
        return SESSIONS.setdefault(sid, {"authed": False, "expire": 0,
                                          "pending_key": None, "pending_expire": 0})


def _rate_limited(ip: str) -> bool:
    now = time.time()
    with _STATE_LOCK:
        hits = [t for t in _RATE.get(ip, []) if now - t < RATE_LIMIT_WINDOW]
        hits.append(now)
        _RATE[ip] = hits
        return len(hits) > RATE_LIMIT_MAX


def require_auth(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        st = _get_state(_sid())
        if not st["authed"] or st["expire"] < time.time():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        return fn(*a, **kw)
    return wrapper


# ============================================================
# KEY-GATE: sinh key -> tạo note (shoptdmmo) -> rút gọn (Link4m) -> user nhập
# ============================================================
def _kdz_generate_key(length: int = 20) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def _kdz_create_note(key: str):
    payload = {"title": f"KDZ Key - {KDZ_GAME_NAME}", "content": f"Key: {key}", "is_public": 1}
    try:
        r = requests.post(KDZ_KEY_API_URL, params={"apikey": KDZ_KEY_API_KEY},
                           json=payload, headers={"Content-Type": "application/json"}, timeout=15)
        r.raise_for_status()
        result = r.json()
        if result.get("success"):
            return result.get("url"), None
        return None, result.get("message", "Tạo note thất bại")
    except Exception as e:
        return None, str(e)


def _kdz_shorten_link4m(long_url: str):
    try:
        r = requests.get(LINK4M_API_URL, params={"api": LINK4M_API_TOKEN, "url": long_url}, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "success":
            return data.get("shortenedUrl"), None
        return None, data.get("message", "Rút gọn link thất bại")
    except Exception as e:
        return None, str(e)


# ============================================================
# ENGINE — y hệt logic turbo.py gốc, chỉ đổi print() -> self.log()
# để đẩy dòng log vào hàng đợi cho web UI poll, thay vì in ra stdout.
# ============================================================
class TurboMinesEngine:
    def __init__(self, profile_json, amount, map_key, mines, cells_to_open, total_rounds):
        if map_key not in MAP_CONFIG:
            raise ValueError(f"map_key phải là một trong {list(MAP_CONFIG)}")
        cfg = MAP_CONFIG[map_key]
        if mines > cfg["max_mines"]:
            raise ValueError(f"Map {map_key} tối đa {cfg['max_mines']} quả bom")
        if cells_to_open >= cfg["deskSize"] - mines:
            raise ValueError(f"Số ô mở phải nhỏ hơn số ô an toàn tối đa ({cfg['deskSize'] - mines})")

        self.ext_token = profile_json["token"]
        self.cid = profile_json["cid"]
        self.game_id = profile_json["gameId"]
        self.visitor_id = profile_json["visitorId"]

        self.amount = amount
        self.map_key = map_key
        self.desk_size = cfg["deskSize"]
        self.mines = mines
        self.cells_to_open = cells_to_open
        self.total_rounds = total_rounds

        self.session = requests.Session()
        self.session.headers.update(HEADERS_COMMON)

        self.player_id = None
        self.jwt = None
        self.currency = "bld"
        self.nonce = 1
        self.client_seed = str(uuid.uuid4())
        self.round_id = None

        self.wins = 0
        self.losses = 0
        self.stop_flag = False
        self.thread = None

        self._log_lock = threading.Lock()
        self.logs = []   # [(seq, text)]
        self._seq = 0

    def log(self, msg: str):
        with self._log_lock:
            self._seq += 1
            self.logs.append((self._seq, msg))
            if len(self.logs) > 500:
                self.logs = self.logs[-500:]

    def logs_since(self, since: int):
        with self._log_lock:
            return [l for l in self.logs if l[0] > since]

    @staticmethod
    def _check(r, tag, log_fn):
        if not r.ok:
            log_fn(f"[{tag}] HTTP {r.status_code} — {r.text[:300]}")
        r.raise_for_status()

    def load_profile(self):
        r = self.session.post(f"{BASE_URL}/api/common/profile",
                               json={"token": self.ext_token, "cid": self.cid,
                                     "gameId": self.game_id, "visitorId": self.visitor_id})
        self._check(r, "profile", self.log)
        data = r.json()
        self.player_id = data["id"]
        self.jwt = data["token"]
        self.currency = data.get("currency", "bld")
        self.session.headers.update({"apikey": self.player_id, "authorization": self.jwt})
        self.log(f"[profile] OK — id={self.player_id} currency={self.currency}")
        return data

    def create_round(self):
        r = self.session.post(f"{BASE_URL}/api/games/create",
                               json={"clientSeed": self.client_seed, "nonce": self.nonce,
                                     "size": self.mines, "deskSize": self.desk_size, "theme": "turbomines"})
        self._check(r, "create", self.log)
        data = r.json()
        self.round_id = data["roundId"]
        self.log(f"[create] roundId={self.round_id} map={self.map_key} mines={self.mines}")
        return data

    def open_cell(self, index):
        r = self.session.post(f"{BASE_URL}/api/bets/place",
                               json={"theme": "turbomines", "roundId": self.round_id, "index": index,
                                     "clientSeed": self.client_seed, "nonce": self.nonce,
                                     "amount": self.amount, "currency": self.currency})
        self._check(r, "place", self.log)
        return r.json()

    def cashout(self):
        r = self.session.post(f"{BASE_URL}/api/bets/cashout", json={"roundId": self.round_id})
        self._check(r, "cashout", self.log)
        data = r.json()
        self.log(f"[cashout] payout={data.get('payout')} coeff={data.get('coefficient')}")
        return data

    @staticmethod
    def _looks_like_loss(data):
        blob = json.dumps(data, ensure_ascii=False).lower()
        return any(k in blob for k in LOSS_KEYWORDS)

    def run(self):
        try:
            self.load_profile()
        except Exception as e:
            self.log(f"[error] Không load được profile: {e}")
            return
        for i in range(1, self.total_rounds + 1):
            if self.stop_flag:
                self.log("[bot] Đã dừng theo yêu cầu.")
                break
            self.log(f"===== VÁN {i}/{self.total_rounds} =====")
            try:
                self.create_round()
            except Exception as e:
                self.log(f"[create] lỗi: {e}")
                continue

            indices = random.sample(range(self.desk_size), self.cells_to_open)
            hit_mine = False
            for idx in indices:
                if self.stop_flag:
                    break
                try:
                    result = self.open_cell(idx)
                except Exception as e:
                    self.log(f"[place] lỗi: {e}")
                    hit_mine = True
                    break
                if self._looks_like_loss(result):
                    self.log(f"[bot] Dính bom ở ô {idx} -> dừng ván.")
                    hit_mine = True
                    break
                time.sleep(0.5)

            if hit_mine:
                self.losses += 1
            else:
                try:
                    self.cashout()
                    self.wins += 1
                except Exception as e:
                    self.log(f"[cashout] lỗi: {e}")
                    self.losses += 1

            self.nonce += 1
            self.log(f"[bot] Tổng: {self.wins} thắng / {self.losses} thua")
        self.log("[bot] KẾT THÚC phiên chạy.")

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_flag = True


# ============================================================
# ROUTES
# ============================================================
@app.route("/")
def index():
    _sid()
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    st = _get_state(_sid())
    authed = st["authed"] and st["expire"] > time.time()
    eng = ENGINES.get(_sid())
    return jsonify({
        "ok": True,
        "authed": authed,
        "expire": st["expire"] if authed else None,
        "running": bool(eng and eng.thread and eng.thread.is_alive()),
    })


@app.route("/api/request-key", methods=["POST"])
def api_request_key():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "?"
    if _rate_limited(ip):
        return jsonify({"ok": False, "error": "Thử quá nhanh, đợi 1 phút rồi thử lại."}), 429

    key = _kdz_generate_key(20)
    note_url, err = _kdz_create_note(key)
    if not note_url:
        return jsonify({"ok": False, "error": f"Không tạo được note key: {err}"}), 502
    short_url, shorten_err = _kdz_shorten_link4m(note_url)
    final_url = short_url or note_url

    st = _get_state(_sid())
    st["pending_key"] = key
    st["pending_expire"] = time.time() + 600   # 10 phut de nhap key

    return jsonify({"ok": True, "url": final_url,
                     "warning": None if short_url else f"Rút gọn Link4m lỗi ({shorten_err}), dùng link note gốc."})


@app.route("/api/verify-key", methods=["POST"])
def api_verify_key():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "?"
    if _rate_limited(ip):
        return jsonify({"ok": False, "error": "Thử quá nhanh, đợi 1 phút rồi thử lại."}), 429

    body = request.get_json(silent=True) or {}
    entered = str(body.get("key", "")).strip()
    st = _get_state(_sid())

    if not st["pending_key"] or st["pending_expire"] < time.time():
        return jsonify({"ok": False, "error": "Chưa xin key hoặc key đã hết hạn nhập, bấm 'Lấy key' lại."}), 400
    if entered != st["pending_key"]:
        return jsonify({"ok": False, "error": "Key không đúng."}), 400

    st["authed"] = True
    st["expire"] = time.time() + KDZ_KEY_TTL_SECONDS
    st["pending_key"] = None
    return jsonify({"ok": True, "expire": st["expire"]})


@app.route("/api/start", methods=["POST"])
@require_auth
def api_start():
    sid = _sid()
    existing = ENGINES.get(sid)
    if existing and existing.thread and existing.thread.is_alive():
        return jsonify({"ok": False, "error": "Đang có phiên chạy, hãy dừng trước khi chạy mới."}), 400

    body = request.get_json(silent=True) or {}
    try:
        profile = body.get("profile")
        if isinstance(profile, str):
            profile = json.loads(profile)
        if not isinstance(profile, dict) or not all(k in profile for k in ("token", "cid", "gameId", "visitorId")):
            raise ValueError("Profile JSON thiếu trường token/cid/gameId/visitorId")

        engine = TurboMinesEngine(
            profile_json=profile,
            amount=int(body.get("amount", 100)),
            map_key=str(body.get("map", "5x5")),
            mines=int(body.get("mines", 3)),
            cells_to_open=int(body.get("cells", 3)),
            total_rounds=int(body.get("rounds", 5)),
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    ENGINES[sid] = engine
    engine.start()
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
@require_auth
def api_stop():
    engine = ENGINES.get(_sid())
    if engine:
        engine.stop()
    return jsonify({"ok": True})


@app.route("/api/logs")
@require_auth
def api_logs():
    since = int(request.args.get("since", 0))
    engine = ENGINES.get(_sid())
    if not engine:
        return jsonify({"ok": True, "logs": [], "seq": since, "running": False, "wins": 0, "losses": 0})
    new_logs = engine.logs_since(since)
    last_seq = new_logs[-1][0] if new_logs else since
    return jsonify({
        "ok": True,
        "logs": [t for _, t in new_logs],
        "seq": last_seq,
        "running": bool(engine.thread and engine.thread.is_alive()),
        "wins": engine.wins,
        "losses": engine.losses,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
