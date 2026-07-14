#!/usr/bin/env python3
"""日砌 — 每日打卡"""

import http.server
import json
import os
import sys
import hashlib
import secrets
import shutil
import urllib.parse
import socket
from datetime import date, datetime, timedelta

# Windows 终端 GBK 编码兼容
if sys.stdout.encoding and sys.stdout.encoding.upper() in ("GBK", "GB2312", "CP936"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
os.makedirs(DATA_DIR, exist_ok=True)

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
        salt = secrets.token_hex(8)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return salt + ":" + h


def verify_password(password, stored):
    salt, h = stored.split(":", 1)
    return hash_password(password, salt) == stored


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
    d = os.path.join(DATA_DIR, username)
    os.makedirs(d, exist_ok=True)
    if date_str is None:
        date_str = date.today().isoformat()
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


# ==================== 保护卡管理 ====================

def protection_file(username):
    d = os.path.join(DATA_DIR, username)
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
    d = os.path.join(DATA_DIR, username)
    return os.path.join(d, f"tasks_{date_str}.json")


def read_tasks_for_date(username, date_str):
    path = task_file_for_date(username, date_str)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("tasks", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def compute_stats(username):
    """计算用户统计，自动处理保护卡"""
    today = date.today()
    yesterday = today - timedelta(days=1)

    # 加载保护卡数据
    prot = load_protection(username)
    cards = prot["cards"]
    protected_set = set(prot["protected"])
    total_earned = prot["totalEarned"]

    # 读取用户所有任务数据
    user_dir = os.path.join(DATA_DIR, username)
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
            "calendar": build_calendar(today, all_tasks, protected_set)
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
            elif cards > 0:
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

    # ---- 构建日历 ----
    calendar = build_calendar(today, all_tasks, protected_set)

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
    """计算历史最长连续打卡（含保护）"""
    dates = sorted(all_tasks.keys())
    if not dates:
        return 0

    longest = 0
    current_run = 0

    for ds in dates:
        if ds > today.isoformat():
            continue
        tasks = all_tasks.get(ds, [])

        if tasks and len(tasks) > 0:
            done = sum(1 for t in tasks if t.get("completed"))
            if done == len(tasks) or ds in protected_set:
                current_run += 1
                longest = max(longest, current_run)
            else:
                current_run = 0
        elif tasks and len(tasks) == 0:
            current_run += 1
            longest = max(longest, current_run)
        else:
            # 没有任务数据的日期
            if ds in protected_set:
                current_run += 1
                longest = max(longest, current_run)
            else:
                current_run = 0

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
        self.send_header("Access-Control-Allow-Origin", "*")
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

    def _require_admin(self, token):
        user = self._auth(token)
        if user and user.get("isAdmin"):
            return user
        return None

    # ---- GET 请求 ----

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)
        get_first = lambda key: (params.get(key) or [None])[0]

        if path == "/api/data":
            token = get_first("token")
            user = self._auth(token)
            if not user:
                self._send_json(401, {"error": "未登录或登录已过期"})
                return
            date_str = get_first("date")
            try:
                with open(user_data_file(user["name"], date_str), "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                data = {"tasks": []}
            self._send_json(200, data)

        elif path == "/api/stats":
            token = get_first("token")
            user = self._auth(token)
            if not user:
                self._send_json(401, {"error": "未登录或登录已过期"})
                return
            name = user["name"]
            self._send_json(200, compute_stats(name))

        elif path == "/api/history":
            token = get_first("token")
            user = self._auth(token)
            if not user:
                self._send_json(401, {"error": "未登录或登录已过期"})
                return
            user_dir = os.path.join(DATA_DIR, user["name"])
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
            token = get_first("token")
            user = self._auth(token)
            if user:
                self._send_json(200, {"name": user["name"], "isAdmin": user.get("isAdmin", False)})
            else:
                self._send_json(401, {"error": "无效的登录凭证"})

        elif path == "/api/users/list":
            user = self._require_admin(get_first("token"))
            if not user:
                self._send_json(403, {"error": "仅管理员可操作"})
                return
            users_data = load_users()
            safe_list = [
                {"name": u["name"], "isAdmin": u.get("isAdmin", False)}
                for u in users_data["users"]
            ]
            self._send_json(200, {"users": safe_list})

        elif path == "/api/admin/progress":
            user = self._require_admin(get_first("token"))
            if not user:
                self._send_json(403, {"error": "仅管理员可查看"})
                return
            users_data = load_users()
            result = {}
            for u in users_data["users"]:
                path_ = os.path.join(DATA_DIR, u["name"], f"tasks_{date.today().isoformat()}.json")
                try:
                    with open(path_, "r", encoding="utf-8") as f:
                        result[u["name"]] = json.load(f)
                except (FileNotFoundError, json.JSONDecodeError):
                    result[u["name"]] = {"tasks": []}
            self._send_json(200, result)

        elif path == "/" or path == "":
            try:
                with open("index.html", "rb") as f:
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
            super().do_GET()

    # ---- POST 请求 ----

    def do_POST(self):
        body = self._read_body()

        if self.path == "/api/save":
            token = body.get("token", "")
            user = self._auth(token)
            if not user:
                self._send_json(401, {"error": "未登录或登录已过期"})
                return
            filepath = user_data_file(user["name"], body.get("date"))
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
            users_data = load_users()
            if find_user(users_data, name):
                self._send_json(409, {"error": "该名字已被注册"})
                return
            is_admin = len(users_data["users"]) == 0
            token = generate_token()
            user = {
                "name": name,
                "password": hash_password(password),
                "token": token,
                "isAdmin": is_admin,
                "createdAt": date.today().isoformat(),
            }
            users_data["users"].append(user)
            save_users(users_data)
            self._send_json(200, {
                "name": name,
                "token": token,
                "isAdmin": is_admin,
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
            token = generate_token()
            user["token"] = token
            save_users(users_data)
            self._send_json(200, {
                "name": name,
                "token": token,
                "isAdmin": user.get("isAdmin", False),
            })

        elif self.path == "/api/users/create":
            admin = self._require_admin(body.get("token", ""))
            if not admin:
                self._send_json(403, {"error": "仅管理员可操作"})
                return
            name = body.get("name", "").strip()
            password = body.get("password", "").strip()
            if not name or not password:
                self._send_json(400, {"error": "名字和密码不能为空"})
                return
            if len(password) < 3:
                self._send_json(400, {"error": "密码至少 3 个字符"})
                return
            users_data = load_users()
            if find_user(users_data, name):
                self._send_json(409, {"error": "该名字已被注册"})
                return
            user = {
                "name": name,
                "password": hash_password(password),
                "token": "",
                "isAdmin": False,
                "createdAt": date.today().isoformat(),
            }
            users_data["users"].append(user)
            save_users(users_data)
            self._send_json(200, {"ok": True})

        elif self.path == "/api/users/remove":
            admin = self._require_admin(body.get("token", ""))
            if not admin:
                self._send_json(403, {"error": "仅管理员可操作"})
                return
            target = body.get("name", "")
            if target == admin["name"]:
                self._send_json(400, {"error": "不能移除自己"})
                return
            users_data = load_users()
            users_data["users"] = [u for u in users_data["users"] if u["name"] != target]
            save_users(users_data)
            target_dir = os.path.join(DATA_DIR, target)
            if os.path.exists(target_dir):
                shutil.rmtree(target_dir)
            self._send_json(200, {"ok": True})

        elif self.path == "/api/users/reset-password":
            admin = self._require_admin(body.get("token", ""))
            if not admin:
                self._send_json(403, {"error": "仅管理员可操作"})
                return
            target = body.get("name", "")
            new_password = body.get("password", "")
            if not target or not new_password:
                self._send_json(400, {"error": "参数不完整"})
                return
            if len(new_password) < 3:
                self._send_json(400, {"error": "密码至少 3 个字符"})
                return
            users_data = load_users()
            user = find_user(users_data, target)
            if not user:
                self._send_json(404, {"error": "用户不存在"})
                return
            user["password"] = hash_password(new_password)
            user["token"] = ""
            save_users(users_data)
            self._send_json(200, {"ok": True})

        else:
            self._send_json(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        if args and args[0] != "GET /favicon.ico":
            try:
                print("  > %s" % args[0])
            except UnicodeEncodeError:
                pass


# ==================== 启动 ====================

if __name__ == "__main__":
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)

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
    print("  第一个注册的用户自动成为管理员")
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
