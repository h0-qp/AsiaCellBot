"""
عميل API تطبيق Asiacell — مستخرج من تحليل APK الإصدار 5.1.4 (2026-10-11)
=========================================================================
الخادم: https://odpapp.asiacell.com
تدفق الدخول (مؤكد من كود التطبيق المفكوك):
  1) POST /api/v1/captcha                -> {requireCaptcha, captcha{resourceUrl, originSource}}
  2) POST /api/v1/login                   body {username, captchaCode}
                                          -> {success, message, requireCaptcha, nextUrl}  و nextUrl هو الـ PID
  3) POST /api/v1/login/resend            body {username, captchaCode}  (إعادة إرسال OTP)
  4) POST /api/v1/smsvalidation           body {PID, passcode, token}
                                          -> {accessToken, refreshToken, tokenType, fullName, userId, userType, username, ...}
بعدها كل الطلبات بهيدر Authorization: Bearer <accessToken>
الردود بمغلّف {success, message, data} وأغلب الشاشات DynamicData {headers, bodies}.
"""

import http.client
import json
import urllib.request
import urllib.error

BASE = "https://odpapp.asiacell.com"
APP_UA = "okhttp/4.12.0"

# هيدرات التطبيق الثابتة — من MyNetworkInterceptor داخل الـ APK (مجرّبة حيًا 2026-10-11)
API_KEY = "1ccbc4c913bc4ce785a0a2de444aa0d6"  # مفتاح عميل التطبيق المدمج بالـ APK (نفسه لكل التثبيتات)


def app_headers(device_id: str) -> dict:
    return {
        "X-ODP-API-KEY": API_KEY,
        "Cache-Control": "no-cache",
        "DeviceID": device_id,
        "X-OS-Version": "14",
        "X-Device-Type": "android",
        "X-ODP-APP-VERSION": "5.1.4",
        "X-FROM-APP": "odp",
        "X-ODP-CHANNEL": "mobile",
        "X-SCREEN-TYPE": "MOBILE",
    }


class AsiacellAPI:
    def __init__(self, access_token: str | None = None, refresh_token: str | None = None,
                 device_id: str | None = None):
        import uuid
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.device_id = device_id or str(uuid.uuid4())

    # ---------- نقل ----------
    def request(self, method: str, path: str, body: dict | None = None, params: dict | None = None):
        url = BASE + path
        if params:
            from urllib.parse import urlencode
            url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        last_exc = None
        for attempt in range(3):
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("User-Agent", APP_UA)
            req.add_header("Accept", "application/json")
            for k, v in app_headers(self.device_id).items():
                req.add_header(k, v)
            if data is not None:
                req.add_header("Content-Type", "application/json")
            if self.access_token:
                req.add_header("Authorization", f"Bearer {self.access_token}")
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    raw = r.read().decode("utf-8", "replace")
                    try:
                        return r.status, json.loads(raw or "{}")
                    except json.JSONDecodeError:
                        # جدار F5 يرجع أحيانًا صفحة "Access Blocked" — إعادة محاولة للقراءة فقط
                        if method == "GET" and "Access Blocked" in raw and attempt < 2:
                            import time
                            time.sleep(2.5)
                            continue
                        return r.status, {"raw": raw}
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", "replace")
                try:
                    return e.code, json.loads(raw or "{}")
                except json.JSONDecodeError:
                    return e.code, {"raw": raw}
            except (urllib.error.URLError, http.client.HTTPException, ConnectionError) as e:
                last_exc = e
                continue
        return 0, {"success": False, "message": f"connection failed: {last_exc}"}

    def get(self, path, params=None):
        return self.request("GET", path, params=params)

    def post(self, path, body=None, params=None):
        return self.request("POST", path, body=body if body is not None else {}, params=params)

    # ---------- تسجيل الدخول ----------
    def captcha(self):
        """POST /api/v1/captcha -> هل الكابتشا مطلوبة + رابط صورتها"""
        return self.post("/api/v1/captcha")

    def login(self, username: str, captcha_code: str = ""):
        """الخطوة 1: طلب OTP. الرد فيه nextUrl = PID اللازم للتأكيد"""
        return self.post("/api/v1/login", {"username": username, "captchaCode": captcha_code})

    def resend_otp(self, username: str, captcha_code: str = ""):
        return self.post("/api/v1/login/resend", {"username": username, "captchaCode": captcha_code})

    def confirm_otp(self, pid: str, passcode: str, token: str = ""):
        """الخطوة 2: تأكيد OTP -> التوكنات.
        ردّ النجاح الحقيقي مسطّح وبأسماء snake_case: {success, access_token, refresh_token, ...}"""
        status, resp = self.post("/api/v1/smsvalidation",
                                 {"PID": pid, "passcode": passcode, "token": token})
        inner = unwrap(resp)
        tok = inner.get("access_token") or inner.get("accessToken")
        if isinstance(inner, dict) and tok:
            self.access_token = tok
            self.refresh_token = inner.get("refresh_token") or inner.get("refreshToken")
        return status, resp

    def logout(self):
        return self.post("/api/v1/logout")

    # ---------- الحساب ----------
    def profile_view(self):
        """نظرة عامة على الحساب (الرصيد والباقات) — DynamicData"""
        return self.get("/api/v1/profile/view")

    def profile_view2(self):
        return self.get("/api/v1/profile/view2")

    def subscriptions(self):
        return self.get("/api/v1/profile/subscriptions")

    def profile(self):
        return self.get("/api/v1/profile")

    def active_offers(self):
        return self.get("/api/v1/offer/active-offers")

    def home(self):
        return self.get("/api/v2/home")

    def notifications(self):
        return self.get("/api/v1/notifications")

    # ---------- الباقات ----------
    def addon_list_v3(self):
        return self.get("/api/v3/addon/list")

    def addon_home(self):
        return self.get("/api/v1/addon")

    def addon_subscribe_payload(self, payload: dict):
        """اشتراك باقة — payload كما يبنيه التطبيق (addOnId/actionKey...)"""
        return self.post("/api/v1/addon", payload)

    def addon_send_gift(self, addon_id: int, receiver_msisdn: str):
        return self.post("/api/v1/addon/send-as-gift",
                         {"addOnId": addon_id, "receiverMsisdn": receiver_msisdn})

    def addon_home_v1(self):
        """شاشة الباقات الرئيسية (فئات + عروض مميزة) — GET /api/v1/addon"""
        return self.get("/api/v1/addon")

    def addon_screen(self, addon_id):
        """شاشة فئة أو تفاصيل باقة — GET /api/v1/addon/{id}"""
        return self.get(f"/api/v1/addon/{addon_id}")

    def addon_summary_v2(self, addon_id):
        """ملخص ما قبل الاشتراك (السعر/الحجم/رصيدك) — GET /api/v2/addon/summary/{id}"""
        return self.get(f"/api/v2/addon/summary/{addon_id}")

    def addon_subscribe(self, addon_id):
        """تنفيذ الاشتراك فعليًا — POST /api/v1/addon بجسم {addOnId, actionKey: subscribe} (خصم من الرصيد).
        ملاحظة مجرّبة: الشكل بالكواري وحده يرجع «الخدمة غير متاحة» — الجسم هو الصحيح."""
        return self.post("/api/v1/addon", {"addOnId": addon_id, "actionKey": "subscribe"})

    # ---------- الشحن والتحويل ----------
    def top_up_voucher(self, msisdn: str, voucher: str, recharge_type: int = 0):
        return self.post("/api/v1/top-up",
                         {"msisdn": msisdn, "voucher": voucher, "rechargeType": recharge_type})

    def credit_transfer_start(self, payload: dict):
        return self.post("/api/v1/credit-transfer/start", payload)

    def credit_transfer_do(self, payload: dict):
        return self.post("/api/v1/credit-transfer/do-transfer", payload)

    def shukran(self):
        return self.get("/api/v1/shukran")

    def shukran_action(self, action_name: str):
        """تنفيذ طلب شكراً — POST /api/v1/shukran/action?actionName=requestCredit|requestInternet"""
        return self.post("/api/v1/shukran/action", {}, params={"actionName": action_name})

    # ---------- سجل الاستخدام CDR (يتطلب OTP خاص) ----------
    def cdr_send_otp(self):
        """POST /api/v1/cdr/send-otp -> nextUrl فيه PID مثل الدخول"""
        return self.post("/api/v1/cdr/send-otp", {})

    def cdr_confirm(self, pid: str, passcode: str):
        """POST /api/v1/cdr/confirm {PID, passcode}"""
        return self.post("/api/v1/cdr/confirm", {"PID": pid, "passcode": passcode})

    def cdr_summary(self):
        return self.get("/api/v1/cdr/summary")

    def cdr_detail(self, type_: str = "all", page: int = 0, limit: int = 20):
        return self.get("/api/v1/cdr/detail", params={"type": type_, "page": page, "limit": limit})

    # ---------- خطوطي والدعم ----------
    def my_lines(self):
        return self.get("/api/v1/multi-line")

    def tickets(self):
        return self.get("/api/v1/resolution-center")

    def ticket_categories(self):
        return self.get("/api/v1/resolution-center/categories")

    def watch_home(self):
        """ملخص نظيف: رصيد + استخدام بيانات — GET /api/v1/watch/home"""
        return self.get("/api/v1/watch/home")

    def avocado_boarding(self):
        """باقات YOOZ (أفوكادو) — GET /api/v1/avocado/boarding"""
        return self.get("/api/v1/avocado/boarding")

    def upload_profile_image(self, img_bytes: bytes):
        """رفع صورة الحساب — multipart POST /api/v1/profile/upload (يجرب اسمي حقل)"""
        import uuid as _uuid
        for part_name in ("file", "image"):
            boundary = "----asiacell" + _uuid.uuid4().hex[:12]
            body = (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{part_name}"; filename="profile.jpg"\r\n'
                "Content-Type: image/jpeg\r\n\r\n"
            ).encode() + img_bytes + f"\r\n--{boundary}--\r\n".encode()
            url = BASE + "/api/v1/profile/upload"
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("User-Agent", APP_UA)
            req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
            for k, v in app_headers(self.device_id).items():
                req.add_header(k, v)
            if self.access_token:
                req.add_header("Authorization", f"Bearer {self.access_token}")
            try:
                with urllib.request.urlopen(req, timeout=40) as r:
                    raw = r.read().decode("utf-8", "replace")
                    try:
                        resp = json.loads(raw or "{}")
                    except json.JSONDecodeError:
                        resp = {"raw": raw}
                    if isinstance(resp, dict) and resp.get("success"):
                        return r.status, resp
                    last = (r.status, resp)
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", "replace")
                try:
                    last = (e.code, json.loads(raw or "{}"))
                except json.JSONDecodeError:
                    last = (e.code, {"raw": raw})
            except Exception as e:
                last = (0, {"success": False, "message": str(e)})
        return last

    # ---------- العجلة اليومية ----------
    def spin_home_v3(self, type_: str | None = None):
        return self.get("/api/v3/spinwheel/home", params={"type": type_} if type_ else None)

    def spin_game_v3(self, ticket_id):
        return self.get("/api/v3/spinwheel/game", params={"ticketId": ticket_id})

    def spin_reward_v3(self, ticket_id):
        return self.get("/api/v3/spinwheel/reward", params={"ticketId": ticket_id})

    def spin_history_v3(self):
        return self.get("/api/v3/spinwheel/history")

    def spin_ui_v2(self):
        return self.get("/api/v2/spinwheel/ui")

    def spin_me_v2(self):
        return self.get("/api/v2/spinwheel/me")

    def spin_home_v2(self):
        return self.get("/api/v2/spinwheel")

    def spin_confirm_v2(self):
        return self.post("/api/v2/spinwheel/confirm")

    def spin_reward_v2(self, reward):
        return self.get("/api/v2/spinwheel/reward", params={"reward": reward})


def unwrap(resp):
    """ردود الخادم بمغلّف BaseResponse: {success, message, data: {...}} — يرجع الـ data إن وجد"""
    if isinstance(resp, dict) and isinstance(resp.get("data"), dict):
        return resp["data"]
    return resp if isinstance(resp, dict) else {}


def render_dynamic(resp, max_lines: int = 60) -> str:
    """عارض عربي مرتب لأي رد من الخادم: يفك ترميز nextAction، يخفي الحقول التقنية،
    ويعرض العناوين والقيم والأزمنة بشكل مقروء — بدون JSON خام."""
    from urllib.parse import parse_qs, unquote, urlparse

    lines: list[str] = []

    def add(text):
        text = str(text).strip()
        if not text:
            return
        text = unquote(text) if "%D" in text else text
        if text not in lines:
            lines.append(text)

    SKIP_KEYS = {
        "success", "analyticData", "analyticAdjust", "analyticContentSquare",
        "backgroundColor", "backgroundImage", "color", "titleColor", "descColor",
        "unitColor", "timeStampColor", "seperator", "icon", "image", "imageUrl",
        "photo", "background", "vid", "groupId", "viewId", "screen", "theme",
        "inverted", "invertedTextColor", "style", "tag", "tags", "filter",
        "startupActions", "geofence", "watch", "popup", "quickGuide", "nmfloId",
        "grandPrize", "ruleId", "userId", "id", "action", "screenTitle",
        "timeStamp", "selectedIndex", "luckyId", "dayIndex", "playable",
        "bundleKey", "weight", "barColor", "expireLabelColor", "subscribeDate",
        "unlimited", "roaming", "homeDisplay", "fup", "freeUnitRefName",
        "freeAppOnly", "activeBundle", "expireSoon", "remainingTitle",
        "remainingVolume", "totalVolume", "expireLabel",
        "featureImage", "label", "credit", "headers",
    }
    LABELS = {
        "firstName": "الاسم", "lastName": "اللقب", "fullName": "الاسم الكامل",
        "name": "الاسم", "phone": "الرقم", "phoneNumber": "الرقم", "msisdn": "الرقم",
        "username": "الرقم", "email": "الإيميل", "balance": "الرصيد",
        "value": "القيمة", "amount": "المبلغ", "price": "السعر",
        "validity": "الصلاحية", "expiry": "انتهاء الصلاحية", "expireDate": "انتهاء الصلاحية",
        "validUntil": "صالح لغاية", "date": "التاريخ", "loginDate": "التاريخ",
        "unit": "النوع", "status": "الحالة", "type": "النوع",
        "remaining": "المتبقي", "used": "المستخدم", "total": "المجموع", "active": "مفعّل",
    }

    def fmt_time(rt) -> str:
        if not isinstance(rt, dict):
            return ""
        h, mnt = rt.get("hours"), rt.get("minutes")
        parts = []
        if h:
            parts.append(f"{h} ساعة")
        if mnt:
            parts.append(f"{mnt} دقيقة")
        return " و".join(parts)

    def walk(node, depth=0):
        if len(lines) >= max_lines or node is None:
            return
        if isinstance(node, dict):
            # nextAction برابط مشفّر: confirm?msg=...&title=...
            na = node.get("nextAction")
            if isinstance(na, str) and "msg=" in na:
                q = parse_qs(urlparse(na.replace("?", "?", 1)).query or na.split("?", 1)[-1])
                title = (q.get("title") or [""])[0]
                msg = (q.get("msg") or [""])[0]
                if title:
                    add(f"⚠️ {title}")
                if msg:
                    add(msg)
            if node.get("message") and str(node["message"]).strip().lower() not in ("success", ""):
                add(node["message"])
            # عناوين رئيسية
            for key in ("screenTitle", "title", "subTitle", "desc", "description", "gpTitle"):
                v = node.get(key)
                if isinstance(v, str) and v.strip():
                    add(v)
            # وقت متبقٍّ للعجلة
            if isinstance(node.get("remainingTime"), dict):
                t = fmt_time(node["remainingTime"])
                if t:
                    add(f"⏳ الفرصة الجاية بعد: {t}")
            if node.get("isPlayable") is True:
                add("✅ العجلة متاحة للعب الآن!")
            elif node.get("isPlayable") is False and "remainingTime" in node:
                add("🎡 لعبت اليوم — الفرصة الجاية باجر")
            # شرائح الجوائز
            slides = node.get("slides")
            if isinstance(slides, list) and slides and isinstance(slides[0], dict) and "grandPrize" in slides[0]:
                prizes = [str(s.get("title")) for s in slides if s.get("title")]
                if prizes:
                    add("🎁 جوائز العجلة: " + " • ".join(prizes))
            # أزرار فعلية فيها عناوين مفيدة
            btn = node.get("actionButton")
            if isinstance(btn, dict) and btn.get("title"):
                add(f"🔘 {btn['title']}")
            # حجم متبقٍّ (باقات البيانات)
            if "remainingVolume" in node:
                def _n(x):
                    return f"{int(x):,}" if isinstance(x, (int, float)) and float(x) == int(x) else str(x)
                rv, tv = node.get("remainingVolume"), node.get("totalVolume")
                unit = ""
                for _uk in ("type", "unit"):
                    if node.get(_uk) in ("MB", "GB", "KB"):
                        unit = node[_uk]
                if tv:
                    add(f"المتبقي: {_n(rv)} من {_n(tv)} {unit}".strip())
                else:
                    add(f"المتبقي: {_n(rv)} {unit}".strip())
            # حقول بسيطة بمسميات عربية
            for key, v in node.items():
                if key in SKIP_KEYS or key in ("title", "subTitle", "desc", "description",
                                               "gpTitle", "message", "nextAction", "data",
                                               "headers", "bodies", "items", "slides",
                                               "remainingTime", "isPlayable", "actionButton"):
                    continue
                if isinstance(v, (str, int, float)) and str(v).strip() not in ("", "None", "null"):
                    if key in ("type", "unit") and v in ("MB", "GB", "KB"):
                        continue  # وحدة حجم — تُعرض مع سطر المتبقي
                    if key == "type" and isinstance(v, str) and not any("\u0600" <= ch <= "\u06FF" for ch in v):
                        continue  # أنواع داخلية بأسماء إنكليزية — ضجيج
                    label = LABELS.get(key, key)
                    val = v
                    if isinstance(val, str):
                        val = val.replace(" null", "").strip()
                        if key in ("expiry", "expireDate") and "T" in val:
                            val = val.split("T")[0]
                    if isinstance(v, float) and v == int(v):
                        val = f"{int(v):,}"
                    elif isinstance(v, int) and v >= 1000:
                        val = f"{v:,}"
                    add(f"{label}: {val}")
            for key in ("headers", "data", "bodies", "items"):
                if key in node:
                    walk(node[key], depth + 1)
            # أي قوائم/كائنات أخرى ذات معنى
            for key, v in node.items():
                if isinstance(v, (list, dict)) and key not in ("headers", "data", "bodies", "items",
                                                                "slides", "remainingTime", "actionButton") \
                        and key not in SKIP_KEYS:
                    walk(v, depth + 1)
        elif isinstance(node, list):
            if not node:
                return
            for item in node:
                walk(item, depth + 1)

    if isinstance(resp, dict):
        data = resp.get("data", resp)
        if isinstance(data, list) and not data:
            add("لا يوجد شيء هنا حاليًا")
        walk(resp)
    else:
        walk(resp)
    return "\n".join(lines) if lines else "تم ✅ (لا تفاصيل إضافية من الخادم)"
