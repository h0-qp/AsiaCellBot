"""
بوت Asiacell — تحكم كامل بحساب آسياسيل من تلغرام (Pyrogram)
============================================================
- تسجيل دخول برقم آسياسيل + OTP (نفس تدفق التطبيق الرسمي بالضبط)
- الجلسة (accessToken/refreshToken + DeviceID ثابت) تنحفظ محليًا بملف 600
- القائمة: الحساب/الرصيد، الاشتراكات، العروض، العجلة اليومية + تدوير تلقائي،
  سجل الجوائز، تصفح الباقات والاشتراك بها، شحن بكارت، تحويل رصيد، التجوال، خروج
- البوت مفتوح للجميع: كل مستخدم يدخل برقمه وجلسته مربوطة بحسابه التلغرام فقط.
التوكن والبيانات بـ .env بنفس المجلد.
"""

import asyncio
import json
import os
import re
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

# --- تحميل .env ---
for line in (BASE_DIR / ".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

API_ID = int(os.environ.get("API_ID") or 0)
API_HASH = os.environ.get("API_HASH") or ""
BOT_TOKEN = os.environ.get("BOT_TOKEN") or ""
ADMIN_ID = int(os.environ.get("ADMIN_ID") or 0)

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
SESSIONS_FILE = DATA_DIR / "sessions.json"

from pyrogram import Client, filters
from pyrogram.types import (CallbackQuery, InlineKeyboardButton,
                            InlineKeyboardMarkup, Message)

from asiacell_api import AsiacellAPI, render_dynamic, unwrap

# --- بروكسي الخروج لـ MTProto (نفس نمط باقي البوتات) ---
proxy = None
if os.environ.get("BOT_USE_PROXY", "1") != "0":
    purl = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if purl:
        from urllib.parse import urlparse
        u = urlparse(purl)
        proxy = {"scheme": "http", "hostname": u.hostname, "port": u.port or 80}
        if u.username:
            proxy["username"] = u.username
        if u.password:
            proxy["password"] = u.password

app = Client("asiacell_bot", api_id=API_ID, api_hash=API_HASH,
             bot_token=BOT_TOKEN, proxy=proxy, workdir=str(BASE_DIR))


# ---------------- الجلسات (لكل مستخدم جلسة مستقلة برقمه) ----------------
_SESSIONS_LOCK = threading.Lock()


def load_sessions() -> dict:
    if SESSIONS_FILE.exists():
        try:
            return json.loads(SESSIONS_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_sessions(s: dict):
    tmp = SESSIONS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1))
    os.replace(tmp, SESSIONS_FILE)
    os.chmod(SESSIONS_FILE, 0o600)


def get_api(user_id: int) -> AsiacellAPI | None:
    s = load_sessions().get(str(user_id))
    if not s:
        return None
    return AsiacellAPI(s.get("accessToken"), s.get("refreshToken"), s.get("deviceId"))


_ACCT_KEYS = ("accessToken", "refreshToken", "deviceId", "username", "fullName", "userId")


def record_accounts(rec: dict) -> dict:
    """حسابات السجل — مع ترقية السجلات القديمة (حساب واحد مسطّح) تلقائيًا"""
    accounts = rec.get("accounts")
    if isinstance(accounts, dict) and accounts:
        return accounts
    if rec.get("username") and rec.get("accessToken"):
        return {rec["username"]: {k: rec.get(k) for k in _ACCT_KEYS}}
    return {}


def _mirror(rec: dict, acc: dict):
    for k in _ACCT_KEYS:
        rec[k] = acc.get(k)
    rec["active"] = acc.get("username")


def store_session(user_id: int, data: dict):
    """يحفظ/يضيف حسابًا ويجعله الفعّال — يدعم تعدد الحسابات لنفس المستخدم"""
    with _SESSIONS_LOCK:
        sessions = load_sessions()
        rec = sessions.get(str(user_id)) or {}
        accounts = record_accounts(rec)
        accounts[data["username"]] = {k: data.get(k) for k in _ACCT_KEYS}
        rec["accounts"] = accounts
        _mirror(rec, accounts[data["username"]])
        sessions[str(user_id)] = rec
        save_sessions(sessions)


def switch_account(user_id: int, number: str) -> bool:
    with _SESSIONS_LOCK:
        sessions = load_sessions()
        rec = sessions.get(str(user_id))
        if not rec:
            return False
        accounts = record_accounts(rec)
        acc = accounts.get(number)
        if not acc:
            return False
        rec["accounts"] = accounts
        _mirror(rec, acc)
        sessions[str(user_id)] = rec
        save_sessions(sessions)
        return True


def remove_active_account(user_id: int) -> str | None:
    """يحذف الحساب الفعّال؛ يرجع رقم الحساب التالي إن وُجد، وإلا None (سجل فارغ)"""
    with _SESSIONS_LOCK:
        sessions = load_sessions()
        rec = sessions.get(str(user_id))
        if not rec:
            return None
        accounts = record_accounts(rec)
        accounts.pop(rec.get("active") or rec.get("username") or "", None)
        if accounts:
            rec["accounts"] = accounts
            _mirror(rec, next(iter(accounts.values())))
            sessions[str(user_id)] = rec
            save_sessions(sessions)
            return rec["active"]
        sessions.pop(str(user_id), None)
        save_sessions(sessions)
        return None


def drop_session(user_id: int):
    with _SESSIONS_LOCK:
        sessions = load_sessions()
        sessions.pop(str(user_id), None)
        save_sessions(sessions)


def update_session(user_id: int, **fields):
    with _SESSIONS_LOCK:
        sessions = load_sessions()
        rec = sessions.get(str(user_id))
        if rec is not None:
            rec.update(fields)
            save_sessions(sessions)


# حالة المحادثة المؤقتة (خطوات الدخول/الشحن/التحويل)
STATE: dict[int, dict] = {}

# البوت مفتوح للجميع — كل مستخدم يدخل برقمه وتُحفظ جلسته مربوطة بحسابه التلغرام فقط
only_admin = filters.user(ADMIN_ID) if ADMIN_ID else filters.user([])


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 حسابي ورصيدي", callback_data="m:profile"),
         InlineKeyboardButton("📦 اشتراكاتي", callback_data="m:subs")],
        [InlineKeyboardButton("🎁 عروضي النشطة", callback_data="m:offers"),
         InlineKeyboardButton("📶 الباقات والعروض", callback_data="m:addons")],
        [InlineKeyboardButton("🎡 العجلة اليومية", callback_data="m:spin"),
         InlineKeyboardButton("📜 سجل جوائز العجلة", callback_data="m:spinhistory")],
        [InlineKeyboardButton("🙏 خدمة شكراً", callback_data="m:shukran"),
         InlineKeyboardButton("🔔 الإشعارات", callback_data="m:notifs")],
        [InlineKeyboardButton("🧾 سجل الاستخدام", callback_data="m:cdr"),
         InlineKeyboardButton("📱 خطوطي", callback_data="m:lines")],
        [InlineKeyboardButton("🆘 الدعم والتذاكر", callback_data="m:tickets"),
         InlineKeyboardButton("🌍 التجوال", callback_data="m:roaming")],
        [InlineKeyboardButton("🔋 شحن خطي بكارت", callback_data="m:topup"),
         InlineKeyboardButton("🔁 تحويل رصيد", callback_data="m:transfer")],
        [InlineKeyboardButton("🔀 حساباتي", callback_data="m:accounts"),
         InlineKeyboardButton("🖼 تغيير صورتي", callback_data="m:photo")],
        [InlineKeyboardButton("📥 تصدير بياناتي", callback_data="m:export"),
         InlineKeyboardButton("🚪 تسجيل الخروج", callback_data="m:logout")],
    ])


BACK_KB = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ القائمة", callback_data="m:menu")]])


async def run_api(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


async def show_menu(msg_or_cb, edit=False):
    text = "📱 **بوت Asiacell** — اختر خدمة:"
    if edit and isinstance(msg_or_cb, CallbackQuery):
        await msg_or_cb.edit_message_text(text, reply_markup=main_menu_kb())
    elif isinstance(msg_or_cb, CallbackQuery):
        await msg_or_cb.message.reply(text, reply_markup=main_menu_kb())
    else:
        await msg_or_cb.reply(text, reply_markup=main_menu_kb())


# ---------------- البداية والدخول ----------------
@app.on_message(filters.command("start") & filters.private)
async def start(_, m: Message):
    sess = load_sessions().get(str(m.from_user.id))
    if sess and get_api(m.from_user.id):
        num = sess.get("username") or ""
        await m.reply(f"✅ أنت مسجّل دخول برقم **{num}**")
        await show_menu(m)
    else:
        STATE[m.from_user.id] = {"step": "ask_number"}
        await m.reply("👋 أهلًا! سجّل دخولك لحساب Asiacell أولًا.\n\n"
                      "📞 أرسل رقم هاتفك (مثال: 0770XXXXXXX):")


@app.on_message(filters.command("cancel") & filters.private)
async def cancel(_, m: Message):
    STATE.pop(m.from_user.id, None)
    if get_api(m.from_user.id):
        await show_menu(m)
    else:
        await m.reply("انلغت العملية. /start للبدء من جديد.")


@app.on_message(filters.command("admin") & only_admin & filters.private)
async def admin_stats(_, m: Message):
    sessions = load_sessions()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📣 بث رسالة لكل المسجّلين", callback_data="adm:bc")],
    ])
    await m.reply(f"👥 مستخدمون سجّلوا دخولهم: {len(sessions)}", reply_markup=kb)


@app.on_message(filters.command("balance") & filters.private)
async def cmd_balance(_, m: Message):
    api = get_api(m.from_user.id)
    if not api:
        await m.reply("لازم تسجّل دخول أولًا — /start")
        return
    status, prof = await run_api(api.profile)
    if status == 403:
        await m.reply("⏰ انتهت الجلسة. سجّل دخولك من جديد بـ /start")
        return
    await m.reply(f"👤 **حسابي**\n\n{render_dynamic(prof)}"[:4000], reply_markup=BACK_KB)


@app.on_message(filters.command("spin") & filters.private)
async def cmd_spin(_, m: Message):
    api = get_api(m.from_user.id)
    if not api:
        await m.reply("لازم تسجّل دخول أولًا — /start")
        return
    msg = await m.reply("🎡 جارٍ فحص العجلة...")
    text = await spin_execute(api)
    if text == "SESSION_EXPIRED":
        await msg.edit_text("⏰ انتهت الجلسة. سجّل دخولك من جديد بـ /start")
        return
    await msg.edit_text(f"🎡 **العجلة اليومية**\n\n{text}"[:4000], reply_markup=spin_kb(m.from_user.id))


@app.on_message(filters.text & filters.private & ~filters.command(["start", "cancel", "admin", "balance", "spin"]))
async def on_text(_, m: Message):
    uid = m.from_user.id
    st = STATE.get(uid)
    if not st:
        return
    step = st.get("step")
    text = m.text.strip()

    # --- بث الأدمن ---
    if step == "ask_broadcast":
        if uid != ADMIN_ID:
            STATE.pop(uid, None)
            return
        STATE.pop(uid, None)
        sent, failed = 0, 0
        for target in list(load_sessions().keys()):
            if int(target) == uid:
                continue
            try:
                await app.send_message(int(target), f"📣 **إعلان من إدارة البوت**\n\n{text}")
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.3)
        await m.reply(f"📣 تم البث: وصل {sent}، فشل {failed}.")
        return

    # --- خطوة الرقم ---
    if step == "ask_number":
        number = text.replace(" ", "").replace("+964", "0").replace("964", "0", 1) if text.startswith("964") else text.replace(" ", "")
        api = AsiacellAPI(device_id=str(uuid.uuid4()))
        _, cap = await run_api(api.captcha)
        if isinstance(cap, dict) and (cap.get("requirecaptcha") or cap.get("requireCaptcha")):
            st.update({"step": "ask_captcha", "number": number, "device": api.device_id,
                       "captcha_url": (cap.get("captcha") or {}).get("resourceUrl", "")})
            await m.reply("🧩 الخادم طلب كابتشا. افتح الرابط، واكتب الرمز اللي يطلع لك هنا:\n" +
                          (st["captcha_url"] or "(لا يوجد رابط)"))
            return
        await do_login_request(m, api, number, "")

    # --- خطوة الكابتشا ---
    elif step == "ask_captcha":
        api = AsiacellAPI(device_id=st["device"])
        await do_login_request(m, api, st["number"], text)

    # --- خطوة OTP ---
    elif step == "ask_otp":
        api = AsiacellAPI(device_id=st["device"])
        status, resp = await run_api(api.confirm_otp, st["pid"], text)
        inner = unwrap(resp)
        access = inner.get("access_token") or inner.get("accessToken")
        if status == 200 and isinstance(inner, dict) and access:
            store_session(uid, {
                "accessToken": access,
                "refreshToken": inner.get("refresh_token") or inner.get("refreshToken"),
                "deviceId": st["device"],
                "username": inner.get("username") or st["number"],
                "fullName": inner.get("full_name") or inner.get("fullName") or "",
                "userId": inner.get("user_id") or inner.get("userId"),
            })
            STATE.pop(uid, None)
            name = inner.get("full_name") or inner.get("fullName") or inner.get("username") or ""
            await m.reply(f"✅ تم تسجيل الدخول! أهلًا {name}")
            # ملخص فوري: رصيد وباقات من شاشة الحساب
            try:
                api2 = AsiacellAPI(access, inner.get("refresh_token") or inner.get("refreshToken"),
                                   st["device"])
                _, prof = await run_api(api2.profile)
                summary = render_dynamic(prof)
                if summary:
                    head = "\n".join(summary.split("\n")[:8])
                    await m.reply(f"📊 **ملخص حسابك:**\n\n{head}")
            except Exception:
                pass
            await show_menu(m)
        else:
            msg = (resp or {}).get("message") if isinstance(resp, dict) else None
            print(f"[confirm fail] status={status} resp={str(resp)[:500]}", flush=True)
            await m.reply(f"❌ فشل التأكيد: {msg or 'رد غير متوقع من الخادم'}\n"
                          "ملاحظة: الرمز صالح لدقيقتين فقط — إذا انتهى، /cancel وابدأ من جديد.\n"
                          "أعد إرسال الرمز الصحيح، أو /cancel للإلغاء.")

    # --- شحن بكارت ---
    elif step == "ask_voucher":
        api = get_api(uid)
        s = load_sessions().get(str(uid), {})
        status, resp = await run_api(api.top_up_voucher, s.get("username", ""), text)
        STATE.pop(uid, None)
        await m.reply(f"🔋 نتيجة الشحن (HTTP {status}):\n\n{render_dynamic(resp)}", reply_markup=BACK_KB)

    # --- سجل الاستخدام: رمز CDR ---
    elif step == "ask_cdr_otp":
        api = get_api(uid)
        if not api:
            STATE.pop(uid, None)
            await m.reply("لازم تسجّل دخول أولًا — /start")
            return
        status, resp = await run_api(api.cdr_confirm, st["pid"], text)
        if status == 200 and isinstance(resp, dict) and resp.get("success"):
            STATE.pop(uid, None)
            _, summ = await run_api(api.cdr_summary)
            out = f"🧾 **ملخص الاستخدام**\n\n{render_dynamic(summ)}"
            for t in ("all", "calls", "data"):
                _, det = await run_api(api.cdr_detail, t)
                if isinstance(det, dict) and det.get("success") and det.get("data"):
                    out += f"\n\n**آخر العمليات:**\n{render_dynamic(det, max_lines=25)}"
                    break
            await m.reply(out[:4000], reply_markup=BACK_KB)
        else:
            msg = (resp or {}).get("message") if isinstance(resp, dict) else None
            await m.reply(f"❌ رمز غير صحيح: {msg or 'حاول مجددًا'}\nأعد إرسال الرمز، أو /cancel للإلغاء.")

    # --- إهداء باقة: رقم المستلم ---
    elif step == "ask_gift_number":
        api = get_api(uid)
        if not api:
            STATE.pop(uid, None)
            await m.reply("لازم تسجّل دخول أولًا — /start")
            return
        receiver = text.strip()
        if receiver.startswith("+964"):
            receiver = "0" + receiver[4:]
        elif receiver.startswith("964"):
            receiver = "0" + receiver[3:]
        if not (receiver.isdigit() and len(receiver) == 11 and receiver.startswith("077")):
            await m.reply("⚠️ الرقم غير صحيح — أرسل رقم آسياسيل كامل (077xxxxxxxx) أو /cancel.")
            return
        pkg_id = st["pkg_id"]
        _, summ = await run_api(api.addon_summary_v2, pkg_id)
        data = (summ or {}).get("data") if isinstance(summ, dict) else {}
        opt = ((data or {}).get("options") or [{}])[0]
        title = opt.get("title") or "الباقة"
        price = opt.get("price") or ""
        st.update({"gift_receiver": receiver})
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ تأكيد الإهداء", callback_data=f"pk:gy:{pkg_id}")],
            [InlineKeyboardButton("⬅️ القائمة", callback_data="m:menu")],
        ])
        await m.reply(f"🎁 **تأكيد الإهداء**\n\n📦 الباقة: {title}\n💰 السعر: {price} "
                      f"(يُخصم من رصيدك)\n📱 المستلم: {receiver}", reply_markup=kb)

    # --- تحويل رصيد: الرقم ---
    elif step == "ask_transfer_to":
        st.update({"step": "ask_transfer_amount", "to": text})
        await m.reply("💰 كم المبلغ (دينار)؟")

    # --- تحويل رصيد: المبلغ ---
    elif step == "ask_transfer_amount":
        api = get_api(uid)
        try:
            amount = float(text)
        except ValueError:
            await m.reply("اكتب مبلغًا رقميًا صحيحًا.")
            return
        payload = {"receiverMsisdn": st["to"], "amount": amount}
        status, resp = await run_api(api.credit_transfer_start, payload)
        st.update({"step": "ask_transfer_otp", "payload": payload})
        msg = (resp or {}).get("message") if isinstance(resp, dict) else ""
        await m.reply(f"📩 {msg or 'إذا وصلك رمز تأكيد على خطك، أرسله هنا لإتمام التحويل.'}\n"
                      "(إذا ما طلب رمز، أرسل كلمة: تم)")

    # --- تحويل رصيد: تأكيد ---
    elif step == "ask_transfer_otp":
        api = get_api(uid)
        payload = dict(st["payload"])
        if text != "تم":
            payload["passcode"] = text
        status, resp = await run_api(api.credit_transfer_do, payload)
        STATE.pop(uid, None)
        await m.reply(f"🔁 نتيجة التحويل (HTTP {status}):\n\n{render_dynamic(resp)}", reply_markup=BACK_KB)


def normalize_pid(raw) -> str:
    """nextUrl يجي كرابط تنقّل داخلي: #/native/open/smsval/1?lang=ar&PID=<uuid>
    الـ PID الحقيقي هو قيمة PID بداخله (36 حرف) — إرسال الرابط كاملًا (71 حرف) يحظره جدار الخادم"""
    s = str(raw or "").strip()
    if not s:
        return ""
    import re
    m = re.search(r"[?&]PID=([^&\s]+)", s, flags=re.I)
    if m:
        return m.group(1)
    return s


@app.on_message(filters.photo & filters.private)
async def on_photo(_, m: Message):
    uid = m.from_user.id
    st = STATE.get(uid)
    if not st or st.get("step") != "ask_photo":
        return
    api = get_api(uid)
    if not api:
        STATE.pop(uid, None)
        await m.reply("لازم تسجّل دخول أولًا — /start")
        return
    STATE.pop(uid, None)
    wait = await m.reply("🖼 جارٍ رفع الصورة...")
    try:
        path = await app.download_media(m.photo.file_id, in_memory=True)
        img_bytes = path.getbuffer().tobytes() if hasattr(path, "getbuffer") else bytes(path)
        status, resp = await run_api(api.upload_profile_image, img_bytes)
        ok = isinstance(resp, dict) and resp.get("success")
        if ok:
            await wait.edit_text("✅ تم تغيير صورة حسابك بنجاح!", reply_markup=BACK_KB)
        else:
            await wait.edit_text(f"🖼 نتيجة الرفع:\n\n{render_dynamic(resp)}"[:4000], reply_markup=BACK_KB)
    except Exception as e:
        await wait.edit_text(f"❌ فشل الرفع: {e}", reply_markup=BACK_KB)


async def do_login_request(m: Message, api: AsiacellAPI, number: str, captcha_code: str):
    uid = m.from_user.id
    status, resp = await run_api(api.login, number, captcha_code)
    inner = unwrap(resp)
    ok = isinstance(resp, dict) and (resp.get("success") or inner.get("success"))
    raw_pid = inner.get("nextUrl") or inner.get("PID") or inner.get("pid") or ""
    pid = normalize_pid(raw_pid)
    print(f"[login] status={status} ok={ok} raw_pid_len={len(str(raw_pid))} "
          f"raw_pid_preview={str(raw_pid)[:60]!r} pid_len={len(pid)}", flush=True)
    if ok and pid:
        STATE[uid] = {"step": "ask_otp", "pid": pid, "device": api.device_id, "number": number}
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📩 إعادة إرسال الرمز", callback_data="login:resend")]])
        await m.reply("✅ تم إرسال رمز التأكيد SMS إلى رقمك.\n🔢 أرسل الرمز هنا (صالح لدقيقتين):", reply_markup=kb)
    else:
        msg = (resp or {}).get("message") if isinstance(resp, dict) else str(resp)
        print(f"[login fail] status={status} pid_found={bool(pid)} resp={str(resp)[:500]}", flush=True)
        STATE.pop(uid, None)
        await m.reply(f"❌ فشل طلب الدخول: {msg}\n/start للمحاولة من جديد.")


# ---------------- أزرار القائمة ----------------
@app.on_callback_query()
async def on_cb(_, cb: CallbackQuery):
    uid = cb.from_user.id
    data = cb.data or ""

    if data == "login:resend":
        st = STATE.get(uid)
        if st and st.get("number"):
            api = AsiacellAPI(device_id=st["device"])
            _, resp = await run_api(api.resend_otp, st["number"])
            msg = (resp or {}).get("message") if isinstance(resp, dict) else ""
            await cb.answer(f"أُعيد الإرسال: {msg}", show_alert=True)
        else:
            await cb.answer("انتهت الجلسة المؤقتة — /start من جديد", show_alert=True)
        return

    if data == "m:menu":
        await show_menu(cb, edit=True)
        return

    if data == "adm:bc":
        if uid != ADMIN_ID:
            await cb.answer("للأدمن فقط", show_alert=True)
            return
        STATE[uid] = {"step": "ask_broadcast"}
        await cb.edit_message_text("📣 أرسل نص البث — سيصل لكل المستخدمين المسجّلين:", reply_markup=BACK_KB)
        return

    api = get_api(uid)
    if not api:
        await cb.answer("لازم تسجّل دخول أولًا — /start", show_alert=True)
        return

    async def render(title, coro_fn, *a, **kw):
        status, resp = await run_api(coro_fn, *a, **kw)
        if status == 403:
            await cb.edit_message_text("⏰ انتهت الجلسة. سجّل دخولك من جديد بـ /start", reply_markup=BACK_KB)
            return
        body = render_dynamic(resp)
        await cb.edit_message_text(f"{title}\n\n{body}"[:4000], reply_markup=BACK_KB)

    if data == "m:profile":
        await render("👤 **حسابي**", api.profile)
    elif data == "m:subs":
        await render("📦 **اشتراكاتي**", api.subscriptions)
    elif data == "m:offers":
        await render("🎁 **عروضي النشطة**", api.active_offers)
    elif data == "m:roaming":
        status, resp = await run_api(api.get, "/api/v2/roaming")
        if status == 403:
            await cb.edit_message_text("⏰ انتهت الجلسة. سجّل دخولك من جديد بـ /start", reply_markup=BACK_KB)
            return
        pkgs = extract_packages(resp)
        if pkgs:
            rows = [[InlineKeyboardButton(f"🛒 {p['title']} — {p['price']}"[:60],
                                          callback_data=f"pk:d:{p['id']}")] for p in pkgs]
            rows.append([InlineKeyboardButton("⬅️ القائمة", callback_data="m:menu")])
            await cb.edit_message_text("🌍 **باقات التجوال**\n\nاختر باقة تجوال للاشتراك:",
                                       reply_markup=InlineKeyboardMarkup(rows))
        else:
            await cb.edit_message_text(f"🌍 **التجوال**\n\n{render_dynamic(resp)}"[:4000],
                                       reply_markup=BACK_KB)
    elif data == "m:notifs":
        await render("🔔 **الإشعارات**", api.notifications)
    elif data == "m:lines":
        await render("📱 **خطوطي**", api.my_lines)
    elif data == "m:tickets":
        await tickets_screen(cb, api)
    elif data == "m:cdr":
        status, resp = await run_api(api.cdr_send_otp)
        if status == 403:
            await cb.edit_message_text("⏰ انتهت الجلسة. سجّل دخولك من جديد بـ /start", reply_markup=BACK_KB)
            return
        pid = normalize_pid((resp or {}).get("nextUrl") or "")
        if pid:
            STATE[uid] = {"step": "ask_cdr_otp", "pid": pid}
            await cb.edit_message_text("🧾 **سجل الاستخدام**\n\nلحمايتك، آسياسيل تطلب رمز تأكيد خاص لهذه الشاشة — "
                                       "أرسلت رمز SMS لرقمك. أدخل الرمز هنا:", reply_markup=BACK_KB)
        else:
            await cb.edit_message_text(f"🧾 **سجل الاستخدام**\n\n{render_dynamic(resp)}"[:4000],
                                       reply_markup=BACK_KB)
    elif data == "m:shukran":
        await shukran_screen(cb, api)
    elif data in ("sh:req:credit", "sh:req:internet"):
        kind = "credit" if data.endswith("credit") else "internet"
        label = "رصيد مقدم (500 / 750 / 1000 د.ع، برسوم 150 د.ع تُخصم لاحقًا)" if kind == "credit" \
            else "حصة إنترنت مقدمة من خدمة شكراً"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ نعم، نفّذ الطلب", callback_data=f"sh:do:{kind}")],
            [InlineKeyboardButton("⬅️ رجوع", callback_data="m:shukran")],
        ])
        await cb.edit_message_text(f"🙏 **خدمة شكراً**\n\nهل أنت متأكد أنك تريد طلب {label}؟",
                                   reply_markup=kb)
    elif data in ("sh:do:credit", "sh:do:internet"):
        action_name = "requestCredit" if data.endswith("credit") else "requestInternet"
        status, resp = await run_api(api.shukran_action, action_name)
        if status == 403:
            await cb.edit_message_text("⏰ انتهت الجلسة. سجّل دخولك من جديد بـ /start", reply_markup=BACK_KB)
        else:
            await cb.edit_message_text(f"🙏 **خدمة شكراً — نتيجة الطلب**\n\n{render_dynamic(resp)}"[:4000],
                                       reply_markup=BACK_KB)
    elif data == "m:spinhistory":
        await render("📜 **سجل جوائز العجلة**", api.spin_history_v3)
    elif data == "m:spin":
        await spin_flow(cb, api)
    elif data == "spin:auto":
        sess = load_sessions().get(str(uid), {})
        new_val = not bool(sess.get("auto_spin"))
        update_session(uid, auto_spin=new_val)
        await cb.answer(("✅ فعّلت التدوير التلقائي — أدور لك العجلة كل يوم بعد 9 الصبح" if new_val
                         else "🔕 أوقفت التدوير التلقائي"), show_alert=True)
        await spin_flow(cb, api)
    elif data == "m:addons":
        await addons_home(cb, api)
    elif data == "pk:adv":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💾 حتى 5 كيكا", callback_data="pk:adv:5"),
             InlineKeyboardButton("💾 15 كيكا وفوق", callback_data="pk:adv:15")],
            [InlineKeyboardButton("💾 50 كيكا وفوق", callback_data="pk:adv:50"),
             InlineKeyboardButton("♾ بلا حدود", callback_data="pk:adv:0")],
            [InlineKeyboardButton("⬅️ القائمة", callback_data="m:menu")],
        ])
        await cb.edit_message_text("🧠 **مستشار الباقات الذكي**\n\nكم إنترنت تحتاج؟ أمسح الكتالوج كله "
                                   "وأرتّب لك الباقات حسب أفضل قيمة (السعر لكل كيكا):",
                                   reply_markup=kb)
    elif data.startswith("pk:adv:"):
        await advisor_screen(cb, float(data.split(":")[2]))
    elif data.startswith("pk:c:"):
        await addons_category(cb, api, data.split(":")[2])
    elif data.startswith("pk:d:"):
        await addons_detail(cb, api, data.split(":")[2])
    elif data.startswith("pk:y:"):
        await addons_confirm(cb, api, data.split(":")[2])
    elif data.startswith("pk:g:"):
        pkg_id = data.split(":")[2]
        STATE[uid] = {"step": "ask_gift_number", "pkg_id": pkg_id}
        await cb.edit_message_text("🎁 **إهداء باقة**\n\nأرسل رقم آسياسيل اللي تريد تهديه الباقة "
                                   "(يُخصم سعرها من رصيدك أنت):", reply_markup=BACK_KB)
    elif data.startswith("pk:gy:"):
        st = STATE.get(uid) or {}
        pkg_id = data.split(":")[2]
        receiver = st.get("gift_receiver")
        if not receiver or st.get("pkg_id") != pkg_id:
            await cb.answer("انتهت خطوة الإهداء — أعد المحاولة من الباقات", show_alert=True)
            return
        status, resp = await run_api(api.addon_send_gift, int(pkg_id), receiver)
        STATE.pop(uid, None)
        if status == 403:
            await cb.edit_message_text("⏰ انتهت الجلسة. سجّل دخولك من جديد بـ /start", reply_markup=BACK_KB)
            return
        ok = isinstance(resp, dict) and resp.get("success")
        head = f"🎉 **تم إهداء الباقة إلى {receiver}!**" if ok else "🎁 **نتيجة الإهداء:**"
        await cb.edit_message_text(f"{head}\n\n{render_dynamic(resp)}"[:4000], reply_markup=BACK_KB)
    elif data == "m:topup":
        STATE[uid] = {"step": "ask_voucher"}
        await cb.edit_message_text("🔋 أرسل رقم كارت الشحن (الفاوتشر):", reply_markup=BACK_KB)
    elif data == "m:transfer":
        STATE[uid] = {"step": "ask_transfer_to"}
        await cb.edit_message_text("🔁 أرسل رقم المستلم:", reply_markup=BACK_KB)
    elif data == "m:logout":
        await run_api(api.logout)
        STATE.pop(uid, None)
        nxt = remove_active_account(uid)
        if nxt:
            await cb.edit_message_text(f"🚪 تم الخروج من هذا الحساب — انتقلت تلقائيًا إلى حسابك {nxt}.",
                                       reply_markup=BACK_KB)
        else:
            await cb.edit_message_text("🚪 تم تسجيل الخروج. /start للدخول مجددًا.")
    elif data == "m:accounts":
        await accounts_screen(cb)
    elif data == "m:photo":
        STATE[uid] = {"step": "ask_photo"}
        await cb.edit_message_text("🖼 **تغيير صورة الحساب**\n\nأرسل الصورة الجديدة هنا وسأرفعها لحسابك بآسياسيل:",
                                   reply_markup=BACK_KB)
    elif data == "m:export":
        await export_data(cb, api)
    elif data == "acc:add":
        STATE[uid] = {"step": "ask_number"}
        await cb.edit_message_text("➕ **إضافة حساب**\n\n📞 أرسل رقم آسياسيل الجديد (سيصلك رمز تأكيد):",
                                   reply_markup=BACK_KB)
    elif data.startswith("acc:sw:"):
        num = data.split(":", 2)[2]
        if switch_account(uid, num):
            await cb.answer(f"🔀 انتقلت إلى {num}", show_alert=True)
            await show_menu(cb, edit=True)
        else:
            await cb.answer("الحساب غير موجود", show_alert=True)


# ---------------- العجلة اليومية ----------------
async def spin_execute(api: AsiacellAPI) -> str:
    """ينفّذ/يفحص العجلة ويرجع نص النتيجة (يُستخدم من القائمة ومن أمر /spin)"""
    # --- v3 ---
    status, home = await run_api(api.spin_home_v3)
    if status == 200 and isinstance(home, dict) and home.get("success", True):
        ticket = find_first(home, ("ticketId", "ticket_id", "ticket"))
        if ticket:
            _, game = await run_api(api.spin_game_v3, ticket)
            _, reward = await run_api(api.spin_reward_v3, ticket)
            return render_dynamic(reward if reward else game)
        txt = render_dynamic(home)
        if "spin" in txt.lower() or "عجلة" in txt:
            return txt

    # --- v2 ---
    status, ui = await run_api(api.spin_ui_v2)
    if status == 403:
        return "SESSION_EXPIRED"
    data_ui = ui.get("data") if isinstance(ui, dict) else None
    if isinstance(data_ui, dict) and data_ui.get("isPlayable"):
        await run_api(api.spin_confirm_v2)
        reward_key = data_ui.get("luckyId") or data_ui.get("selectedIndex")
        await run_api(api.spin_reward_v2, reward_key)
        _, me = await run_api(api.spin_me_v2)
        prize = latest_spin_prize(me)
        if prize:
            return f"🎉 **دورت العجلة وربحت!**\n\n🎁 جائزتك: {prize}\n(تنضاف لحسابك كمكافأة — شوفها من 👤 حسابي)"
        return "🎉 **دورت العجلة!**\n\nإذا انضافت جائزة، تلگاها بـ 📜 سجل جوائز العجلة وبداخل حسابك."
    return render_dynamic(ui)


async def spin_flow(cb: CallbackQuery, api: AsiacellAPI):
    uid = cb.from_user.id
    await cb.edit_message_text("🎡 جارٍ فحص العجلة اليومية...", reply_markup=BACK_KB)
    text = await spin_execute(api)
    if text == "SESSION_EXPIRED":
        await cb.edit_message_text("⏰ انتهت الجلسة. سجّل دخولك من جديد بـ /start", reply_markup=BACK_KB)
        return
    await cb.edit_message_text(f"🎡 **العجلة اليومية**\n\n{text}"[:4000], reply_markup=spin_kb(uid))


def latest_spin_prize(me_resp) -> str:
    """يستخرج أحدث جائزة من ردّ سجل العجلة /api/v2/spinwheel/me"""
    try:
        items = (me_resp or {}).get("data", {}).get("items") or []
        if items:
            first = items[0]
            title = first.get("title") or ""
            unit = first.get("unit") or ""
            return f"{title} {unit}".strip()
    except Exception:
        pass
    return ""


def spin_kb(uid: int) -> InlineKeyboardMarkup:
    sess = load_sessions().get(str(uid), {})
    on = bool(sess.get("auto_spin"))
    label = "🔔 التدوير التلقائي: يعمل ✅" if on else "🔕 التدوير التلقائي: متوقف — فعّله"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data="spin:auto")],
        [InlineKeyboardButton("⬅️ القائمة", callback_data="m:menu")],
    ])


# ---------------- تصدير البيانات ----------------
async def export_data(cb: CallbackQuery, api: AsiacellAPI):
    uid = cb.from_user.id
    sess = load_sessions().get(str(uid), {})
    num = sess.get("username") or ""
    await cb.edit_message_text("📥 جارٍ تجميع بياناتك...", reply_markup=BACK_KB)
    parts = [f"بيانات حساب Asiacell — {num}",
             f"تاريخ التصدير: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
             "=" * 40]
    for title, fn in [("حسابي", api.profile), ("اشتراكاتي", api.subscriptions),
                      ("عروضي النشطة", api.active_offers), ("خطوطي", api.my_lines),
                      ("سجل جوائز العجلة (7 أيام)", api.spin_me_v2),
                      ("الإشعارات", api.notifications)]:
        try:
            _, resp = await run_api(fn)
            parts.append(f"\n## {title}\n{render_dynamic(resp, max_lines=120)}")
        except Exception as e:
            parts.append(f"\n## {title}\n(تعذّر الجلب: {e})")
    import io
    bio = io.BytesIO(("\n".join(parts)).encode("utf-8"))
    bio.name = f"asiacell-{num}.txt"
    await cb.message.reply_document(bio, caption="📥 بيانات حسابك كاملة بملف واحد")
    await cb.edit_message_text("📥 تم إرسال ملف بياناتك ☝️", reply_markup=BACK_KB)


# ---------------- حسابات متعددة ----------------
async def accounts_screen(cb: CallbackQuery):
    uid = cb.from_user.id
    rec = load_sessions().get(str(uid), {})
    accounts = record_accounts(rec)
    active = rec.get("active") or rec.get("username")
    rows = []
    for num, acc in accounts.items():
        mark = "✅ " if num == active else "🔀 "
        name = acc.get("fullName") or ""
        rows.append([InlineKeyboardButton(f"{mark}{num} {name}".strip(),
                                          callback_data=f"acc:sw:{num}")])
    rows.append([InlineKeyboardButton("➕ إضافة حساب جديد", callback_data="acc:add")])
    rows.append([InlineKeyboardButton("⬅️ القائمة", callback_data="m:menu")])
    await cb.edit_message_text(
        "🔀 **حساباتي**\n\nدوس على أي حساب للتبديل إليه فورًا — كل حساب بجلسته ورقمه، "
        "والعجلة والتنبيهات تشتغل على الحساب الفعّال:",
        reply_markup=InlineKeyboardMarkup(rows))


# ---------------- الدعم والتذاكر (عرض) ----------------
async def tickets_screen(cb: CallbackQuery, api: AsiacellAPI):
    status, resp = await run_api(api.tickets)
    if status == 403:
        await cb.edit_message_text("⏰ انتهت الجلسة. سجّل دخولك من جديد بـ /start", reply_markup=BACK_KB)
        return
    _, cats = await run_api(api.ticket_categories)
    cat_titles = []
    if isinstance(cats, dict):
        for c in ((cats.get("data") or {}).get("categories") or []):
            if c.get("title"):
                cat_titles.append("• " + str(c["title"]).strip())
    text = f"🆘 **الدعم والتذاكر**\n\n{render_dynamic(resp)}"
    if cat_titles:
        text += "\n\n**فئات الدعم المتاحة بالتطبيق الرسمي:**\n" + "\n".join(cat_titles)
    await cb.edit_message_text(text[:4000], reply_markup=BACK_KB)


# ---------------- خدمة شكراً (رصيد/إنترنت مقدم) ----------------
async def shukran_screen(cb: CallbackQuery, api: AsiacellAPI):
    status, resp = await run_api(api.shukran)
    if status == 403:
        await cb.edit_message_text("⏰ انتهت الجلسة. سجّل دخولك من جديد بـ /start", reply_markup=BACK_KB)
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💵 اطلب رصيد مقدم", callback_data="sh:req:credit"),
         InlineKeyboardButton("🌐 اطلب إنترنت مقدم", callback_data="sh:req:internet")],
        [InlineKeyboardButton("⬅️ القائمة", callback_data="m:menu")],
    ])
    await cb.edit_message_text(f"🙏 **خدمة شكراً**\n\n{render_dynamic(resp)}"[:4000], reply_markup=kb)


# ---------------- الباقات والعروض (تصفح واشتراك) ----------------
def _walk_dicts(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk_dicts(v)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_dicts(item)


def _action_item_id(action) -> str | None:
    """يستخرج itemId من أكشن مثل navigate/addonDetailV5?itemId=1618 أو confirmSubscription?itemId=1618"""
    if isinstance(action, dict):
        action = action.get("action") or ""
    if not isinstance(action, str):
        return None
    m = re.search(r"itemId=(\d+)", action)
    return m.group(1) if m else None


def extract_categories(resp) -> list[tuple[str, str]]:
    """فئات الباقات من شاشة الإضافات (عناصر الأيقونات: id + title)"""
    out, seen = [], set()
    for d in _walk_dicts(resp):
        cid = d.get("id")
        title = d.get("title")
        if isinstance(cid, int) and isinstance(title, str) and title.strip() and "icon" in d \
                and not d.get("price") and cid not in seen:
            seen.add(cid)
            out.append((str(cid), title.strip()))
    return out[:14]


def extract_packages(resp) -> list[dict]:
    """باقات قابلة للشراء: عنوان + سعر + itemId من الأكشن"""
    out, seen = [], set()
    for d in _walk_dicts(resp):
        title = d.get("title")
        price = d.get("price")
        if not (isinstance(title, str) and title.strip()):
            continue
        pid = _action_item_id(d.get("action")) or _action_item_id(d.get("actionButton"))
        if pid is None and isinstance(d.get("id"), int) and price:
            pid = str(d["id"])
        if pid and (price or d.get("priceValue")) and pid not in seen:
            seen.add(pid)
            out.append({"id": pid, "title": title.strip(),
                        "price": price if isinstance(price, str) else str(d.get("priceValue", "")),
                        "priceValue": d.get("priceValue"),
                        "validity": d.get("validity") or "",
                        "blurb": d.get("description") or d.get("data") or d.get("desc") or ""})
    return out[:40]


# ---------------- مستشار الباقات الذكي ----------------
def _num_from(text) -> float | None:
    if text is None:
        return None
    t = str(text).translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")).replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)", t)
    return float(m.group(1)) if m else None


def parse_volume_gb(pkg: dict) -> float | None:
    """يستخرج حجم الباقة بالكيكابايت من العنوان/الوصف — None إن لم يُعرف، و99999 للا محدود"""
    text = f"{pkg.get('title', '')} {pkg.get('blurb', '')}"
    if "بلا حدود" in text or "بلاحدود" in text or "unlimited" in text.lower():
        return 99999.0
    t = text.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:كيكا|جيجا|غیغابایت|غيغابايت|GB|Gbps)", t, re.I)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:MB|ميكا|ميغابايت)", t, re.I)
    if m:
        return float(m.group(1)) / 1000.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:TB|تيرا)", t, re.I)
    if m:
        return float(m.group(1)) * 1000.0
    return None


def parse_validity_days(pkg: dict) -> int | None:
    text = f"{pkg.get('validity', '')} {pkg.get('title', '')}".translate(
        str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    m = re.search(r"(\d+)\s*(?:أسبوع|أسابيع|اسبوع)", text)
    if m:
        return int(m.group(1)) * 7
    m = re.search(r"(\d+)\s*(?:يوم|أيام|ايام)", text)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*(?:شهر|أشهر|اشهر)", text)
    if m:
        return int(m.group(1)) * 30
    if "أسبوعي" in text or "اسبوعي" in text:
        return 7
    if "شهري" in text:
        return 30
    return None


def package_price(pkg: dict) -> float | None:
    pv = pkg.get("priceValue")
    if isinstance(pv, (int, float)) and pv:
        return float(pv)
    return _num_from(pkg.get("price"))


_CATALOG_CACHE: dict[int, tuple[float, list]] = {}


async def scan_catalog(api: AsiacellAPI, uid: int = 0) -> list[dict]:
    """يمسح الرئيسية + كل الفئات (بالتوازي) + YOOZ ويجمع كل الباقات بلا تكرار — كاش 10 دقائق"""
    import time as _time
    if uid and uid in _CATALOG_CACHE and _time.time() - _CATALOG_CACHE[uid][0] < 600:
        return _CATALOG_CACHE[uid][1]
    found: dict[str, dict] = {}
    status, home = await run_api(api.addon_home_v1)
    cats = []
    if status == 200 and isinstance(home, dict):
        for p in extract_packages(home):
            found[p["id"]] = p
        cats = extract_categories(home)

    async def fetch_cat(cid):
        try:
            st, resp = await run_api(api.addon_screen, cid)
            if st == 200 and isinstance(resp, dict) and "raw" not in resp:
                return extract_packages(resp)
        except Exception:
            pass
        return []

    if cats:
        results = await asyncio.gather(*[fetch_cat(cid) for cid, _t in cats])
        for pkgs in results:
            for p in pkgs:
                found.setdefault(p["id"], p)
    try:
        _, avo = await run_api(api.avocado_boarding)
        for b in (((avo or {}).get("data") or {}).get("bundles") or []):
            if b.get("id") and str(b["id"]) not in found:
                found[str(b["id"])] = {"id": str(b["id"]), "title": b.get("title", ""),
                                       "price": b.get("price", ""), "priceValue": None,
                                       "validity": "", "blurb": b.get("volume", "")}
    except Exception:
        pass
    catalog = list(found.values())
    if uid:
        _CATALOG_CACHE[uid] = (_time.time(), catalog)
    return catalog


async def advisor_screen(cb: CallbackQuery, min_gb: float):
    uid = cb.from_user.id
    api = get_api(uid)
    if not api:
        await cb.answer("لازم تسجّل دخول أولًا — /start", show_alert=True)
        return
    await cb.edit_message_text("🧠 **مستشار الباقات**\n\nجارٍ مسح كتالوج آسياسيل كاملًا (كل الفئات) "
                               "وترتيب الباقات حسب القيمة... لحظات ⏳", reply_markup=BACK_KB)
    catalog = await scan_catalog(api, uid)
    finite, unlimited = [], []
    for p in catalog:
        vol = parse_volume_gb(p)
        price = package_price(p)
        if vol is None or price is None:
            continue
        if vol >= 9000:
            unlimited.append((price, -(parse_validity_days(p) or 0), p, vol, price))
        elif vol >= min_gb:
            finite.append((price / vol, -(parse_validity_days(p) or 0), p, vol, price))
    if min_gb == 0:
        unlimited.sort(key=lambda x: (x[0], x[1]))
        ranked = unlimited
    else:
        finite.sort(key=lambda x: (x[0], x[1]))
        unlimited.sort(key=lambda x: (x[0], x[1]))
        ranked = finite + unlimited
    top = ranked[:5]
    if not top:
        await cb.edit_message_text("🧠 **مستشار الباقات**\n\nما لگيت باقات مطابقة بهذا المسح — "
                                   "جرّب من قائمة الباقات مباشرة.", reply_markup=BACK_KB)
        return
    lines = [f"🧠 **مستشار الباقات** — فحصت {len(catalog)} باقة ورتّبت لك الأفضل قيمة:\n"]
    rows = []
    for i, (per_gb, _nv, p, vol, price) in enumerate(top, 1):
        vol_t = "بلا حدود" if vol >= 9000 else f"{vol:g} كيكا"
        days = parse_validity_days(p)
        val_t = f" • {days} يوم" if days else ""
        per_t = " • الأقوى قيمة" if vol >= 9000 else f" • {per_gb:,.0f} د.ع/كيكا"
        lines.append(f"{i}. **{p['title']}**\n   💾 {vol_t}{val_t} • 💰 {price:,.0f} د.ع{per_t}")
        rows.append([InlineKeyboardButton(f"🛒 {i}. {p['title']}"[:55], callback_data=f"pk:d:{p['id']}")])
    rows.append([InlineKeyboardButton("⬅️ كل الباقات", callback_data="m:addons"),
                 InlineKeyboardButton("⬅️ القائمة", callback_data="m:menu")])
    await cb.edit_message_text("\n".join(lines)[:4000], reply_markup=InlineKeyboardMarkup(rows))


async def addons_home(cb: CallbackQuery, api: AsiacellAPI):
    status, resp = await run_api(api.addon_home_v1)
    if status == 403:
        await cb.edit_message_text("⏰ انتهت الجلسة. سجّل دخولك من جديد بـ /start", reply_markup=BACK_KB)
        return
    cats = extract_categories(resp)
    pkgs = extract_packages(resp)
    rows = []
    # باقات YOOZ (أفوكادو) أولًا — قابلة للشراء من نفس تدفق الباقات
    try:
        _, avo = await run_api(api.avocado_boarding)
        bundles = ((avo or {}).get("data") or {}).get("bundles") or []
        for b in bundles[:4]:
            if b.get("id") and b.get("title"):
                rows.append([InlineKeyboardButton(
                    f"⭐ YOOZ: {b['title']} — {b.get('price', '')}"[:60],
                    callback_data=f"pk:d:{b['id']}")])
    except Exception:
        pass
    red_pkgs = [p for p in pkgs if "RED" in p["title"].upper() or "ريد" in p["title"]]
    red_ids = {p["id"] for p in red_pkgs}
    shown = 0
    for p in pkgs:
        if p["id"] in red_ids or shown >= 6:
            continue
        shown += 1
        rows.append([InlineKeyboardButton(f"⭐ {p['title']} — {p['price']}".strip()[:60],
                                          callback_data=f"pk:d:{p['id']}")])
    # قسم خاص: باقات RED (من مميزة الرئيسية — شاشات فئاتها تُحظر أحيانًا من جدار الخادم)
    for p in red_pkgs:
        rows.append([InlineKeyboardButton(f"🔴 {p['title']} — {p['price']}".strip()[:60],
                                          callback_data=f"pk:d:{p['id']}")])
    for cid, title in cats:
        rows.append([InlineKeyboardButton(f"📂 {title}", callback_data=f"pk:c:{cid}")])
    rows.append([InlineKeyboardButton("⬅️ القائمة", callback_data="m:menu")])
    text = "📶 **الباقات والعروض**\n\nاختر باقة مميزة أو تصفّح حسب الفئة:"
    if not cats and not pkgs:
        text = "📶 **الباقات والعروض**\n\n" + render_dynamic(resp)
    rows.insert(0, [InlineKeyboardButton("🧠 مستشار الباقات — رتّب لي الأفضل قيمة",
                                         callback_data="pk:adv")])
    await cb.edit_message_text(text[:4000], reply_markup=InlineKeyboardMarkup(rows))


async def addons_category(cb: CallbackQuery, api: AsiacellAPI, cat_id: str):
    status, resp = await run_api(api.addon_screen, cat_id)
    if status == 403:
        await cb.edit_message_text("⏰ انتهت الجلسة. سجّل دخولك من جديد بـ /start", reply_markup=BACK_KB)
        return
    pkgs = extract_packages(resp)
    cats = extract_categories(resp)
    if not pkgs:
        # بعض "الفئات" هي نفسها شاشة تفاصيل باقة واحدة (priceBox + زر تأكيد اشتراك)
        for d in _walk_dicts(resp):
            pid = _action_item_id(d.get("action")) if d.get("price") else None
            if pid:
                await addons_detail(cb, api, pid)
                return
    rows = []
    for p in pkgs:
        label = f"{p['title']} — {p['price']}".strip()
        if p.get("validity"):
            label += f" ({p['validity']})"
        rows.append([InlineKeyboardButton("🛒 " + label[:60], callback_data=f"pk:d:{p['id']}")])
    # فئات فرعية إن وُجدت (غير الفئة الحالية)
    for cid, title in cats:
        if cid != cat_id:
            rows.append([InlineKeyboardButton(f"📂 {title}", callback_data=f"pk:c:{cid}")])
    rows.append([InlineKeyboardButton("⬅️ كل الباقات", callback_data="m:addons"),
                 InlineKeyboardButton("⬅️ القائمة", callback_data="m:menu")])
    if pkgs:
        text = "📶 **باقات الفئة**\n\nاختر باقة لعرض تفاصيلها والاشتراك:"
    else:
        text = "📶 **باقات الفئة**\n\n" + render_dynamic(resp)
    await cb.edit_message_text(text[:4000], reply_markup=InlineKeyboardMarkup(rows))


async def addons_detail(cb: CallbackQuery, api: AsiacellAPI, pkg_id: str):
    status, summ = await run_api(api.addon_summary_v2, pkg_id)
    if status == 403:
        await cb.edit_message_text("⏰ انتهت الجلسة. سجّل دخولك من جديد بـ /start", reply_markup=BACK_KB)
        return
    data = (summ or {}).get("data") if isinstance(summ, dict) else None
    lines = ["📶 **تفاصيل الباقة**\n"]
    balance_note = ""
    if isinstance(data, dict):
        opt = (data.get("options") or [{}])[0]
        if opt.get("title"):
            lines.append(f"📦 {opt['title']}")
        if opt.get("volumed"):
            lines.append(f"💾 الحجم: {opt['volumed']}")
        if opt.get("validity"):
            lines.append(f"🗓 الصلاحية: {opt['validity']}")
        if opt.get("price"):
            lines.append(f"💰 السعر: {opt['price']}")
        bal = (opt.get("onlinePayment") or {}).get("balance") or {}
        if bal.get("desc"):
            balance_note = f"\n\n💳 {bal['desc']}"
        total = opt.get("total")
        lines.append("\nالاشتراك يُخصم من رصيدك مباشرة.")
    else:
        lines.append(render_dynamic(summ))
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تأكيد الاشتراك", callback_data=f"pk:y:{pkg_id}")],
        [InlineKeyboardButton("🎁 إهداء هذه الباقة لرقم آخر", callback_data=f"pk:g:{pkg_id}")],
        [InlineKeyboardButton("⬅️ كل الباقات", callback_data="m:addons"),
         InlineKeyboardButton("⬅️ القائمة", callback_data="m:menu")],
    ])
    await cb.edit_message_text(("\n".join(lines) + balance_note)[:4000], reply_markup=kb)


async def addons_confirm(cb: CallbackQuery, api: AsiacellAPI, pkg_id: str):
    status, resp = await run_api(api.addon_subscribe, pkg_id)
    if status == 403:
        await cb.edit_message_text("⏰ انتهت الجلسة. سجّل دخولك من جديد بـ /start", reply_markup=BACK_KB)
        return
    body = render_dynamic(resp)
    ok = isinstance(resp, dict) and resp.get("success")
    head = "🎉 **تم الاشتراك بالباقة بنجاح!**" if ok else "📶 **نتيجة الاشتراك:**"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 حسابي", callback_data="m:profile"),
         InlineKeyboardButton("⬅️ القائمة", callback_data="m:menu")],
    ])
    await cb.edit_message_text(f"{head}\n\n{body}"[:4000], reply_markup=kb)


# ---------------- مهام الخلفية: تدوير تلقائي + مراقبة انتهاء الرصيد/البيانات ----------------
_AR_MONTHS = {
    "يناير": 1, "كانون الثاني": 1, "فبراير": 2, "شباط": 2, "مارس": 3, "آذار": 3,
    "أبريل": 4, "نيسان": 4, "مايو": 5, "أيار": 5, "يونيو": 6, "حزيران": 6,
    "يوليو": 7, "تموز": 7, "أغسطس": 8, "آب": 8, "سبتمبر": 9, "أيلول": 9,
    "أكتوبر": 10, "تشرين الأول": 10, "نوفمبر": 11, "تشرين الثاني": 11,
    "ديسمبر": 12, "كانون الأول": 12,
}


def _parse_ar_date(text: str):
    """يحوّل تاريخًا عربيًا مثل «١٢ أكتوبر ٢٠٢٦» إلى date أو None"""
    if not text:
        return None
    t = text.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    m = re.search(r"(\d{1,2})\s+([^\d\s]+(?:\s+ال[^\d\s]+)?)\s+(\d{4})", t)
    if not m:
        return None
    day, mon_name, year = int(m.group(1)), m.group(2).strip(), int(m.group(3))
    month = _AR_MONTHS.get(mon_name)
    if not month:
        for k, v in _AR_MONTHS.items():
            if k in mon_name or mon_name in k:
                month = v
                break
    if not month:
        return None
    try:
        from datetime import date
        return date(year, month, day)
    except ValueError:
        return None


async def _watch_user(uid: int, sess: dict, today_str: str):
    """فحص يومي: تنبيه قبل انتهاء صلاحية الرصيد (يومان) أو نفاد باقة إنترنت (<20MB)"""
    api_u = AsiacellAPI(sess.get("accessToken"), sess.get("refreshToken"), sess.get("deviceId"))
    status, prof = await run_api(api_u.profile)
    if status == 403 or not isinstance(prof, dict):
        return
    alerts = []
    flags = dict(sess.get("watch_flags") or {})
    # صلاحية الرصيد + انخفاضه
    for d in _walk_dicts(prof):
        val = d.get("validity")
        bal = d.get("value")
        if isinstance(bal, (int, float)) and bal < 1000 and flags.get("lowbal") != str(bal):
            alerts.append(f"💰 **تنبيه رصيد منخفض:** رصيدك الحالي {bal:,.0f} د.ع بس — "
                          "اشحن حتى ما تنقطع خدمتك.")
            flags["lowbal"] = str(bal)
        if isinstance(val, str) and ("لغاية" in val or "لغایة" in val):
            exp = _parse_ar_date(val)
            if exp:
                from datetime import date
                days = (exp - date.today()).days
                if 0 <= days <= 2 and flags.get("bal_exp") != val:
                    alerts.append(f"⏰ **تنبيه رصيد:** صلاحية رصيدك تنتهي {val.strip()} "
                                  f"({'اليوم!' if days == 0 else 'باجر!' if days == 1 else 'بعد يومين'}) — "
                                  "اشحن خطك حتى ما ينقطع.")
                    flags["bal_exp"] = val
            break
    # باقات إنترنت قاربت النفاد
    for d in _walk_dicts(prof):
        rv, tv = d.get("remainingVolume"), d.get("totalVolume")
        if isinstance(rv, (int, float)) and isinstance(tv, (int, float)) and tv >= 50 and rv <= 20:
            key = f"{d.get('title')}|{d.get('subscribeDate')}"
            if flags.get("lowdata") != key:
                alerts.append(f"📉 **تنبيه إنترنت:** باقتك «{str(d.get('title') or '').strip()}» قاربت تخلص — "
                              f"المتبقي {int(rv) if float(rv) == int(rv) else rv} من {int(tv) if float(tv) == int(tv) else tv} MB.")
                flags["lowdata"] = key
    if alerts:
        update_session(uid, watch_flags=flags, last_watch=today_str)
        for a in alerts:
            try:
                await app.send_message(uid, a)
            except Exception:
                pass
    else:
        update_session(uid, last_watch=today_str)


async def auto_spin_loop():
    """كل 20 دقيقة: للمستخدمين المفعّلين، يدور العجلة مرة باليوم بعد 9 الصبح (بغداد) ويُعلم الفائز"""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Baghdad")
    await asyncio.sleep(60)
    while True:
        try:
            now = datetime.now(tz)
            today = now.date().isoformat()
            if now.hour >= 9:
                for uid_str, sess in list(load_sessions().items()):
                    if not sess.get("auto_spin") or sess.get("last_auto_spin") == today:
                        continue
                    uid = int(uid_str)
                    api_u = AsiacellAPI(sess.get("accessToken"), sess.get("refreshToken"),
                                        sess.get("deviceId"))
                    status, ui = await run_api(api_u.spin_ui_v2)
                    if status == 403:
                        update_session(uid, last_auto_spin=today)
                        try:
                            await app.send_message(
                                uid, "⚠️ **التدوير التلقائي:** جلستك انتهت — سجّل دخولك من جديد بـ /start "
                                     "حتى أرجع أدوّر لك العجلة كل يوم.")
                        except Exception:
                            pass
                        continue
                    data_ui = ui.get("data") if isinstance(ui, dict) else None
                    prize = ""
                    if isinstance(data_ui, dict) and data_ui.get("isPlayable"):
                        await run_api(api_u.spin_confirm_v2)
                        rk = data_ui.get("luckyId") or data_ui.get("selectedIndex")
                        await run_api(api_u.spin_reward_v2, rk)
                        _, me = await run_api(api_u.spin_me_v2)
                        prize = latest_spin_prize(me)
                    update_session(uid, last_auto_spin=today)
                    if prize:
                        try:
                            await app.send_message(
                                uid, f"🎡 **التدوير التلقائي**\n\nدوّرت لك العجلة اليوم وربحت: 🎁 **{prize}**\n"
                                     "انضافت لحسابك كمكافأة إنترنت.")
                        except Exception:
                            pass
            # مراقبة انتهاء الرصيد/البيانات — مرة باليوم لكل مستخدم مسجّل
            if now.hour >= 8:
                for uid_str, sess in list(load_sessions().items()):
                    if sess.get("last_watch") == today:
                        continue
                    try:
                        await _watch_user(int(uid_str), sess, today)
                    except Exception as e:
                        print(f"[watch] user {uid_str}: {e}", flush=True)
            # التقرير الأسبوعي — الأحد 10 صباحًا بتوقيت بغداد
            if now.weekday() == 6 and now.hour >= 10:
                for uid_str, sess in list(load_sessions().items()):
                    if sess.get("last_report") == today:
                        continue
                    uid = int(uid_str)
                    update_session(uid, last_report=today)
                    try:
                        api_u = AsiacellAPI(sess.get("accessToken"), sess.get("refreshToken"),
                                            sess.get("deviceId"))
                        status, wh = await run_api(api_u.watch_home)
                        if status != 200 or not isinstance(wh, dict):
                            continue
                        info = ((wh.get("data") or {}).get("information") or {})
                        usage = ((wh.get("data") or {}).get("dataUsage") or [])
                        data_line = ""
                        for u in usage:
                            if u.get("type") == "DATA":
                                data_line = f"📶 إنترنت متبقي: {u.get('remaining')} من {u.get('total')} {u.get('unit')}"
                        _, ui = await run_api(api_u.spin_ui_v2)
                        dui = ui.get("data") if isinstance(ui, dict) else {}
                        spin_line = "🎡 عجلة اليوم: جاهزة للتدوير!" if (dui or {}).get("isPlayable") \
                            else "🎡 عجلة اليوم: لعبتها"
                        await app.send_message(
                            uid, "📊 **تقريرك الأسبوعي من بوت Asiacell**\n\n"
                                 f"👤 {info.get('fullname') or ''} ({info.get('username') or ''})\n"
                                 f"💰 الرصيد: {info.get('mainBalance') or '—'}\n"
                                 f"{data_line}\n{spin_line}\n\n"
                                 "أسبوعك سعيد! 🌹")
                    except Exception as e:
                        print(f"[report] user {uid_str}: {e}", flush=True)
        except Exception as e:
            print(f"[autospin] error: {e}", flush=True)
        await asyncio.sleep(1200)


def find_first(node, keys):
    if isinstance(node, dict):
        for k in keys:
            v = node.get(k)
            if v not in (None, "", 0):
                return v
        for v in node.values():
            r = find_first(v, keys)
            if r:
                return r
    elif isinstance(node, list):
        for item in node:
            r = find_first(item, keys)
            if r:
                return r
    return None


if __name__ == "__main__":
    if not BOT_TOKEN:
        print("⚠️ BOT_TOKEN فارغ بـ .env — ضع توكن البوت من BotFather أولًا.")
        sys.exit(1)
    print("Asiacell bot starting...")
    from pyrogram import idle

    async def _main():
        await app.start()
        asyncio.create_task(auto_spin_loop())
        await idle()
        await app.stop()

    app.run(_main())
