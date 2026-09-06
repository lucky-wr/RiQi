#!/usr/bin/env python3
"""日砌 — 每日打卡"""

import http.server
import json
import os
import re
import sys
import hashlib
import secrets
import urllib.parse
import socket
from datetime import date, datetime, timedelta

# Windows 终端 GBK 编码兼容
if sys.stdout.encoding and sys.stdout.encoding.upper() in ("GBK", "GB2312", "CP936"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080

if getattr(sys, "frozen", False):
    # PyInstaller 打包后：数据放在 exe 同目录，网页资源在解压临时目录里
    SCRIPT_DIR = os.path.dirname(sys.executable)
    RESOURCE_DIR = getattr(sys, "_MEIPASS", SCRIPT_DIR)
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    RESOURCE_DIR = SCRIPT_DIR

DATA_DIR = os.path.join(SCRIPT_DIR, "data")
INDEX_FILE = os.path.join(RESOURCE_DIR, "index.html")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
os.makedirs(DATA_DIR, exist_ok=True)

INVALID_NAME_CHARS = set('\\/:*?"<>|\r\n\t')
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def valid_username(name):
    """用户名不能包含路径分隔符、不能是 . / ..，避免路径穿越。"""
    if not isinstance(name, str) or not name:
        return False
    if name in (".", ".."):
        return False
    if name != name.strip() or name.endswith((".", " ")):
        return False
    if any(ord(c) < 32 for c in name):
        return False
    if any(c in INVALID_NAME_CHARS for c in name):
        return False
    return len(name) <= 32


def safe_date_str(value):
    if not isinstance(value, str):
        return None
    if not DATE_PATTERN.match(value):
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        return None
    return value


def user_dir_for(username):
    """返回用户数据目录，并确保它位于 DATA_DIR 内部。"""
    if not valid_username(username):
        raise ValueError("invalid username")
    base = os.path.abspath(DATA_DIR)
    path = os.path.abspath(os.path.join(base, username))
    if os.path.commonpath([base, path]) != base:
        raise ValueError("invalid username")
    return path


# ==================== 用户管理 ====================

def load_users():
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"users": []}


def save_users(data):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    iterations = 200_000
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations
    ).hex()
    return f"pbkdf2${salt}${iterations}${digest}"


def _verify_legacy_hash(password, stored):
    salt, h = stored.split(":", 1)
    legacy = hashlib.sha256((salt + password).encode()).hexdigest()
    return h == legacy


def verify_password(password, stored):
    if not stored.startswith("pbkdf2$"):
        return _verify_legacy_hash(password, stored)
    try:
        _, salt, iterations, digest = stored.split("$", 3)
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations)
        ).hex()
        return candidate == digest
    except (ValueError, TypeError):
        return False


def generate_token():
    return secrets.token_hex(32)


def find_user(users_data, name):
    for u in users_data["users"]:
        if u["name"] == name:
            return u
    return None


def find_user_by_token(users_data, token):
    for u in users_data["users"]:
        if u.get("token") == token:
            return u
    return None


def user_data_file(username, date_str=None):
    if date_str is None:
        date_str = date.today().isoformat()
    date_str = safe_date_str(date_str)
    if not date_str:
        raise ValueError("invalid date")
    d = user_dir_for(username)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"tasks_{date_str}.json")


def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ==================== 保持唤醒 ====================

POWER_FILE = os.path.join(DATA_DIR, "power.json")
KEEP_AWAKE = False


def apply_keep_awake():
    """阻止屏幕自动熄灭（仅 Windows；不影响系统休眠）。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_DISPLAY_REQUIRED = 0x00000002
        flag = ES_CONTINUOUS | (ES_DISPLAY_REQUIRED if KEEP_AWAKE else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flag)
    except Exception:
        pass


def load_power_state():
    global KEEP_AWAKE
    try:
        with open(POWER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        KEEP_AWAKE = bool(data.get("enabled", False))
    except (FileNotFoundError, json.JSONDecodeError):
        KEEP_AWAKE = False
    apply_keep_awake()
    return KEEP_AWAKE


def save_power_state(enabled):
    global KEEP_AWAKE
    KEEP_AWAKE = bool(enabled)
    with open(POWER_FILE, "w", encoding="utf-8") as f:
        json.dump({"enabled": KEEP_AWAKE}, f, ensure_ascii=False, indent=2)
    apply_keep_awake()


# ==================== 保护卡管理 ====================

def protection_file(username):
    d = user_dir_for(username)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "protection.json")


def load_protection(username):
    try:
        with open(protection_file(username), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"cards": 0, "protected": [], "totalEarned": 0}


def save_protection(username, data):
    with open(protection_file(username), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_card_max(streak):
    if streak <= 20: return 2
    if streak <= 50: return 5
    return 10


def task_file_for_date(username, date_str):
    date_str = safe_date_str(date_str)
    if not date_str:
        raise ValueError("invalid date")
    d = user_dir_for(username)
    return os.path.join(d, f"tasks_{date_str}.json")


def read_tasks_for_date(username, date_str):
    path = task_file_for_date(username, date_str)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("tasks", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# ==================== 每日复盘管理 ====================

def review_file(username, date_str=None):
    if date_str is None:
        date_str = date.today().isoformat()
    date_str = safe_date_str(date_str)
    if not date_str:
        raise ValueError("invalid date")
    d = user_dir_for(username)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"review_{date_str}.json")


# ==================== 任务库管理 ====================

def library_file(username):
    d = user_dir_for(username)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "library.json")


def load_library(username):
    try:
        with open(library_file(username), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"items": []}


def save_library(username, data):
    with open(library_file(username), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def compute_stats(username, target=None):
    """计算用户统计，自动处理保护卡。
    target：可选，某月任一天，用于构建该月日历；默认今天。未来日期会被钳制到今天。"""
    today = date.today()
    if target is None or target > today:
        target = today
    yesterday = today - timedelta(days=1)

    # 加载保护卡数据
    prot = load_protection(username)
    cards = prot["cards"]
    protected_set = set(prot["protected"])
    total_earned = prot["totalEarned"]

    # 读取用户所有任务数据
    user_dir = user_dir_for(username)
    all_tasks = {}
    if os.path.exists(user_dir):
        for fname in sorted(os.listdir(user_dir)):
            if fname.startswith("tasks_") and fname.endswith(".json"):
                ds = fname[len("tasks_"):-len(".json")]
                try:
                    with open(os.path.join(user_dir, fname), "r", encoding="utf-8") as f:
                        data = json.load(f)
                    all_tasks[ds] = data.get("tasks", [])
                except (json.JSONDecodeError, FileNotFoundError):
                    continue

    if not all_tasks:
        # 没有数据时返回初始状态
        return {
            "streak": 0, "longestStreak": 0, "totalDays": 0, "fullDays": 0,
            "totalTasks": 0, "completedTasks": 0, "completionRate": 0,
            "monthTasks": 0, "monthCompleted": 0,
            "cards": 0, "cardsMax": 2, "protectedDates": [],
            "calendar": build_calendar(target, all_tasks, protected_set)
        }

    # 基础统计
    total_tasks = 0
    completed_tasks = 0
    total_days = len(all_tasks)
    full_days = 0  # 任务全部完成的天数

    for ds, tasks in all_tasks.items():
        if tasks:
            total_tasks += len(tasks)
            done = sum(1 for t in tasks if t.get("completed"))
            completed_tasks += done
            if done == len(tasks):
                full_days += 1

    completion_rate = round(completed_tasks / total_tasks, 2) if total_tasks > 0 else 0

    # ---- 计算当月统计 ----
    month_start = today.replace(day=1)
    month_tasks = 0
    month_completed = 0
    d = month_start
    while d <= today:
        ds = d.isoformat()
        tasks = all_tasks.get(ds, [])
        if tasks:
            month_tasks += len(tasks)
            month_completed += sum(1 for t in tasks if t.get("completed"))
        d += timedelta(days=1)

    # ---- 计算连续打卡 + 自动保护 ----
    used_cards = 0
    new_protected = []

    # 从昨天开始往前遍历（今天还没结束，不纳入连续判断）
    check_start = yesterday
    # 但今天如果已经全部完成，也从今天开始算
    today_tasks = all_tasks.get(today.isoformat(), [])
    if today_tasks:
        today_done = sum(1 for t in today_tasks if t.get("completed"))
        if today_done == len(today_tasks) and len(today_tasks) > 0:
            check_start = today

    dates_sorted = sorted(all_tasks.keys())
    first_date = date.fromisoformat(dates_sorted[0]) if dates_sorted else today
    # 往前最多查 365 天
    earliest = max(first_date, today - timedelta(days=365))

    streak = 0
    current = check_start

    while current >= earliest:
        ds = current.isoformat()
        tasks = all_tasks.get(ds)

        if tasks is not None and len(tasks) > 0:
            # 有任务数据
            done = sum(1 for t in tasks if t.get("completed"))
            all_done = done == len(tasks)

            if all_done:
                streak += 1
            elif ds in protected_set:
                streak += 1
            elif cards > 0 and len(tasks) - done >= 2:
                cards -= 1
                used_cards += 1
                protected_set.add(ds)
                new_protected.append(ds)
                streak += 1
            else:
                break
        elif tasks is not None and len(tasks) == 0:
            # 空任务列表，算打卡成功（无任务可做）
            streak += 1
        else:
            # 没有数据（没打卡）
            if ds in protected_set:
                streak += 1
            elif cards > 0:
                cards -= 1
                used_cards += 1
                protected_set.add(ds)
                new_protected.append(ds)
                streak += 1
            else:
                break

        current -= timedelta(days=1)

    # ---- 计算奖励保护卡 ----
    # 每连续 2 天奖励 1 张，不超过上限
    new_earned = (streak // 2) - total_earned
    if new_earned > 0:
        total_earned += new_earned
        cards += new_earned

    # 上限限制
    cards_max = get_card_max(streak)
    cards = min(cards, cards_max)

    # ---- 保存保护卡状态 ----
    prot["cards"] = cards
    prot["protected"] = sorted(protected_set)
    prot["totalEarned"] = total_earned
    save_protection(username, prot)

    # ---- 计算最长连续（含保护） ----
    longest = compute_longest_streak(all_tasks, protected_set, today, yesterday)

    # ---- 构建日历（月份由 target 决定） ----
    calendar = build_calendar(target, all_tasks, protected_set)

    return {
        "streak": streak,
        "longestStreak": longest,
        "totalDays": total_days,
        "fullDays": full_days,
        "totalTasks": total_tasks,
        "completedTasks": completed_tasks,
        "completionRate": completion_rate,
        "monthTasks": month_tasks,
        "monthCompleted": month_completed,
        "cards": cards,
        "cardsMax": cards_max,
        "cardsUsedThisRun": used_cards,
        "newCardsAwarded": max(new_earned, 0),
        "protectedDates": sorted(protected_set),
        "calendar": calendar,
    }


def build_calendar(today, all_tasks, protected_set):
    """构建当前月份的日历"""
    month_start = today.replace(day=1)
    if month_start.month == 12:
        month_end = month_start.replace(year=month_start.year + 1, month=1)
    else:
        month_end = month_start.replace(month=month_start.month + 1)

    calendar = {}
    d = month_start
    while d < month_end:
        ds = d.isoformat()
        tasks = all_tasks.get(ds)
        if ds in protected_set:
            calendar[ds] = "protected"
        elif tasks:
            done = sum(1 for t in tasks if t.get("completed"))
            if len(tasks) > 0 and done == len(tasks):
                calendar[ds] = "full"
            elif done > 0:
                calendar[ds] = "partial"
            else:
                calendar[ds] = "empty"
        else:
            calendar[ds] = "empty"
        d += timedelta(days=1)
    return calendar


def compute_longest_streak(all_tasks, protected_set, today, yesterday):
    """计算历史最长连续打卡（含保护）。按日历日逐个检查，缺卡的日子会中断连续。"""
    dates = sorted(all_tasks.keys())
    if not dates:
        return 0

    longest = 0
    current_run = 0
    d = date.fromisoformat(dates[0])
    end = today
    if d > end:
        return 0

    while d <= end:
        ds = d.isoformat()
        tasks = all_tasks.get(ds)
        ok = False
        if ds in protected_set:
            ok = True
        elif tasks is not None and len(tasks) > 0:
            done = sum(1 for t in tasks if t.get("completed"))
            ok = done == len(tasks)
        elif tasks is not None and len(tasks) == 0:
            ok = True

        if ok:
            current_run += 1
            longest = max(longest, current_run)
        else:
            current_run = 0
        d += timedelta(days=1)

    return longest


# ==================== HTTP Handler ====================

class Handler(http.server.SimpleHTTPRequestHandler):

    # ---- 辅助方法 ----

    def _send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _auth(self, token):
        if not token:
            return None
        return find_user_by_token(load_users(), token)

    def _token_from_header(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        return ""

    # ---- GET 请求 ----

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)
        get_first = lambda key: (params.get(key) or [None])[0]
        request_token = self._token_from_header() or get_first("token")

        if path == "/api/data":
            token = request_token
            user = self._auth(token)
            if not user:
                self._send_json(401, {"error": "未登录或登录已过期"})
                return
            date_str = get_first("date")
            if date_str:
                date_str = safe_date_str(date_str)
                if not date_str:
                    self._send_json(400, {"error": "日期格式不正确"})
                    return
            try:
                with open(user_data_file(user["name"], date_str), "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError, ValueError):
                data = {"tasks": []}
            self._send_json(200, data)

        elif path == "/api/stats":
            token = request_token
            user = self._auth(token)
            if not user:
                self._send_json(401, {"error": "未登录或登录已过期"})
                return
            name = user["name"]
            target = None
            month_str = get_first("month")
            if month_str:
                try:
                    parts = month_str.split("-")
                    target = date(int(parts[0]), int(parts[1]), 1)
                except (ValueError, IndexError):
                    target = None
            self._send_json(200, compute_stats(name, target))

        elif path == "/api/library":
            token = request_token
            user = self._auth(token)
            if not user:
                self._send_json(401, {"error": "未登录或登录已过期"})
                return
            self._send_json(200, load_library(user["name"]))

        elif path == "/api/review":
            token = request_token
            user = self._auth(token)
            if not user:
                self._send_json(401, {"error": "未登录或登录已过期"})
                return
            raw_date = get_first("date")
            if raw_date:
                date_str = safe_date_str(raw_date)
                if not date_str:
                    self._send_json(400, {"error": "日期格式不正确"})
                    return
            else:
                date_str = date.today().isoformat()
            try:
                with open(review_file(user["name"], date_str), "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError, ValueError):
                data = {}
            self._send_json(200, data)

        elif path == "/api/reviews":
            token = request_token
            user = self._auth(token)
            if not user:
                self._send_json(401, {"error": "未登录或登录已过期"})
                return
            user_dir = user_dir_for(user["name"])
            reviews = {}
            if os.path.exists(user_dir):
                for fname in sorted(os.listdir(user_dir)):
                    if fname.startswith("review_") and fname.endswith(".json"):
                        date_str = fname[len("review_"):-len(".json")]
                        try:
                            with open(os.path.join(user_dir, fname), "r", encoding="utf-8") as f:
                                data = json.load(f)
                            if data.get("score"):
                                reviews[date_str] = data
                        except (json.JSONDecodeError, FileNotFoundError):
                            continue
            self._send_json(200, {"reviews": reviews})

        elif path == "/api/history":
            token = request_token
            user = self._auth(token)
            if not user:
                self._send_json(401, {"error": "未登录或登录已过期"})
                return
            user_dir = user_dir_for(user["name"])
            history = {}
            if os.path.exists(user_dir):
                for fname in sorted(os.listdir(user_dir)):
                    if fname.startswith("tasks_") and fname.endswith(".json"):
                        date_str = fname[len("tasks_"):-len(".json")]
                        try:
                            with open(os.path.join(user_dir, fname), "r", encoding="utf-8") as f:
                                data = json.load(f)
                            history[date_str] = data.get("tasks", [])
                        except (json.JSONDecodeError, FileNotFoundError):
                            continue
            self._send_json(200, {"history": history})

        elif path == "/api/users/me":
            token = request_token
            user = self._auth(token)
            if user:
                self._send_json(200, {"name": user["name"]})
            else:
                self._send_json(401, {"error": "无效的登录凭证"})

        elif path == "/api/power/keep-awake":
            user = self._auth(request_token)
            if not user:
                self._send_json(401, {"error": "未登录或登录已过期"})
                return
            self._send_json(200, {"enabled": KEEP_AWAKE})

        elif path == "/" or path == "":
            try:
                with open(INDEX_FILE, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(content)
            except FileNotFoundError:
                self._send_json(404, {"error": "index.html 未找到"})
        else:
            self._send_json(404, {"error": "not found"})

    # ---- POST 请求 ----

    def do_POST(self):
        body = self._read_body()
        token = self._token_from_header() or body.get("token", "")

        if self.path == "/api/library":
            user = self._auth(token)
            if not user:
                self._send_json(401, {"error": "未登录或登录已过期"})
                return
            library_data = body.get("library", {})
            save_library(user["name"], library_data)
            self._send_json(200, {"ok": True})

        elif self.path == "/api/review":
            user = self._auth(token)
            if not user:
                self._send_json(401, {"error": "未登录或登录已过期"})
                return
            raw_date = body.get("date")
            if raw_date:
                date_str = safe_date_str(raw_date)
                if not date_str:
                    self._send_json(400, {"error": "日期格式不正确"})
                    return
            else:
                date_str = date.today().isoformat()
            review_data = body.get("review", {})
            review_data["updatedAt"] = datetime.now().isoformat()
            with open(review_file(user["name"], date_str), "w", encoding="utf-8") as f:
                json.dump(review_data, f, ensure_ascii=False, indent=2)
            self._send_json(200, {"ok": True})

        elif self.path == "/api/power/keep-awake":
            user = self._auth(token)
            if not user:
                self._send_json(401, {"error": "未登录或登录已过期"})
                return
            save_power_state(bool(body.get("enabled", False)))
            self._send_json(200, {"enabled": KEEP_AWAKE})

        elif self.path == "/api/save":
            user = self._auth(token)
            if not user:
                self._send_json(401, {"error": "未登录或登录已过期"})
                return
            raw_date = body.get("date")
            if raw_date:
                date_str = safe_date_str(raw_date)
                if not date_str:
                    self._send_json(400, {"error": "日期格式不正确"})
                    return
            else:
                date_str = date.today().isoformat()
            filepath = user_data_file(user["name"], date_str)
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump({"tasks": body.get("tasks", [])}, f, ensure_ascii=False, indent=2)
            self._send_json(200, {"ok": True})

        elif self.path == "/api/users/register":
            name = body.get("name", "").strip()
            password = body.get("password", "").strip()
            if not name or not password:
                self._send_json(400, {"error": "名字和密码不能为空"})
                return
            if len(password) < 3:
                self._send_json(400, {"error": "密码至少 3 个字符"})
                return
            if not valid_username(name):
                self._send_json(400, {"error": "名字不合法，请勿包含 / \\ : * ? \" < > | 等字符"})
                return
            users_data = load_users()
            if find_user(users_data, name):
                self._send_json(409, {"error": "该名字已被注册"})
                return
            if users_data["users"]:
                self._send_json(409, {"error": "单人模式下无法创建新账号"})
                return
            token = generate_token()
            user = {
                "name": name,
                "password": hash_password(password),
                "token": token,
                "createdAt": date.today().isoformat(),
            }
            users_data["users"].append(user)
            save_users(users_data)
            self._send_json(200, {
                "name": name,
                "token": token,
            })

        elif self.path == "/api/users/login":
            name = body.get("name", "").strip()
            password = body.get("password", "").strip()
            if not name or not password:
                self._send_json(400, {"error": "名字和密码不能为空"})
                return
            users_data = load_users()
            user = find_user(users_data, name)
            if not user:
                self._send_json(404, {"error": "用户不存在，请先注册"})
                return
            if not verify_password(password, user["password"]):
                self._send_json(401, {"error": "密码错误"})
                return
            if not user["password"].startswith("pbkdf2$"):
                user["password"] = hash_password(password)
            token = generate_token()
            user["token"] = token
            save_users(users_data)
            self._send_json(200, {
                "name": name,
                "token": token,
            })

        else:
            self._send_json(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        if args and args[0] != "GET /favicon.ico":
            try:
                msg = args[0]
                if "token=" in msg:
                    msg = re.sub(r"(token=)[^&\s]+", r"\1***", msg)
                print("  > %s" % msg)
            except UnicodeEncodeError:
                pass


# ==================== 启动 ====================

if __name__ == "__main__":
    os.chdir(SCRIPT_DIR)
    load_power_state()

    lan_ip = get_lan_ip()

    # Windows 防火墙：尝试自动添加放行规则
    if sys.platform == "win32":
        import subprocess
        python_path = sys.executable
        try:
            subprocess.run(
                ['netsh', 'advfirewall', 'firewall', 'add', 'rule',
                 'name=RiQi_%d' % PORT, 'dir=in', 'action=allow',
                 'program=' + python_path, 'protocol=tcp',
                 'localport=%d' % PORT],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5
            )
            rule_ok = True
        except Exception:
            rule_ok = False

    print()
    print("  == Ri Qi - Daily Check-in ==")
    print("  本机:  http://localhost:%d" % PORT)
    print("  局域网: http://%s:%d" % (lan_ip, PORT))
    print("  首次登录会自动创建账号（仅支持一个账号）")
    print("  按 Ctrl+C 停止服务器")
    print()

    # 防火墙提示
    if sys.platform == "win32" and lan_ip != "127.0.0.1" and not rule_ok:
        print("  [!] 如果局域网其他设备无法访问，请以管理员身份运行 fix_firewall.bat")
        print()

    httpd = http.server.HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  服务器已停止\n")
        httpd.server_close()
