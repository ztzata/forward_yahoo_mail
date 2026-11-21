import os
import re
import imaplib
import email
import requests
from datetime import datetime, timezone
from html import escape as html_escape
from email.header import decode_header
from flask import Flask, jsonify, request

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

app = Flask(__name__)

# ---------- 环境变量读取 ----------
YAHOO_EMAIL = os.environ.get("YAHOO_EMAIL")
YAHOO_APP_PASSWORD = os.environ.get("YAHOO_APP_PASSWORD")
FORWARD_TO = os.environ.get("FORWARD_TO")
BREVO_API_KEY = os.environ.get("BREVO_API_KEY")
SENDER = os.environ.get("SENDER")  # Brevo 发件人地址
IMAP_SERVER = os.environ.get("IMAP_SERVER", "imap.mail.yahoo.com")

IMAP_PORT = 993
BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"


# ---------- 头部编解码 ----------
def decode_mime_words(s):
    if not s:
        return ""
    result = ""
    for content, charset in decode_header(s):
        if isinstance(content, bytes):
            try:
                result += content.decode(charset or "utf-8", errors="replace")
            except (LookupError, UnicodeDecodeError):
                result += content.decode("utf-8", errors="replace")
        else:
            result += content
    return result


# ---------- 正文 / HTML / 日历 解析 ----------
def extract_parts(msg):
    """返回 {'plain': (bytes, charset), 'html': (bytes, charset), 'cal': (bytes, charset)}"""
    result = {"plain": (None, None), "html": (None, None), "cal": (None, None)}

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "").lower()
            # 只跳过真正的图片/文件附件，别误伤正文
            if "attachment" in disp and ctype.startswith(("image/", "application/")):
                continue
            charset = part.get_content_charset()
            payload = part.get_payload(decode=True)
            if ctype == "text/plain" and result["plain"][0] is None:
                result["plain"] = (payload, charset)
            elif ctype == "text/html" and result["html"][0] is None:
                result["html"] = (payload, charset)
            elif ctype in ("text/calendar", "application/ics") and result["cal"][0] is None:
                result["cal"] = (payload, charset)
    else:
        ctype = msg.get_content_type()
        charset = msg.get_content_charset()
        payload = msg.get_payload(decode=True)
        if ctype == "text/html":
            result["html"] = (payload, charset)
        else:
            result["plain"] = (payload, charset)

    return result


def decode_bytes_to_str(raw, charset):
    if raw is None:
        return ""
    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")


# ---------- ICS 解析 ----------
# 常见 Windows 时区名 → IANA（Teams 常发 Windows 名）
_WIN_TZ = {
    "China Standard Time": "Asia/Shanghai",
    "Taipei Standard Time": "Asia/Taipei",
    "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul",
    "India Standard Time": "Asia/Kolkata",
    "GMT Standard Time": "Europe/London",
    "W. Europe Standard Time": "Europe/Berlin",
    "Romance Standard Time": "Europe/Paris",
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "Pacific Standard Time": "America/Los_Angeles",
    "AUS Eastern Standard Time": "Australia/Sydney",
}


def _unfold_ics(text):
    """ICS 折行展开：以空格/制表符开头的续行拼接回上一行。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    out = []
    for line in lines:
        if line.startswith((" ", "\t")) and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _parse_property(line):
    """解析 NAME;PARAM=VAL:VALUE，返回 (name, params, value)；无法解析返回 (None, {}, line)。"""
    in_quote, colon = False, -1
    for i, ch in enumerate(line):
        if ch == '"':
            in_quote = not in_quote
        elif ch == ":" and not in_quote:
            colon = i
            break
    if colon == -1:
        return None, {}, line

    key_part, value = line[:colon], line[colon + 1:]
    pieces = key_part.split(";")
    name = pieces[0].upper()
    params = {}
    for p in pieces[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
                v = v[1:-1]
            params[k.upper()] = v
        else:
            params[p.upper()] = ""
    return name, params, value


def _unescape(v):
    return (v.replace("\\n", "\n").replace("\\N", "\n")
             .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def _to_zone(tzid):
    if not tzid or not ZoneInfo:
        return None
    tzid = tzid.strip('"')
    for cand in (tzid, _WIN_TZ.get(tzid)):
        if not cand:
            continue
        try:
            return ZoneInfo(cand)
        except Exception:
            pass
    return None


def _parse_dt(value, params):
    v = value.strip()
    is_utc = v.endswith("Z")
    if is_utc:
        v = v[:-1]
    dt = None
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d"):
        try:
            dt = datetime.strptime(v, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return None
    if is_utc:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        tz = _to_zone(params.get("TZID"))
        if tz:
            dt = dt.replace(tzinfo=tz)
    return dt


def _fmt_dt(prop):
    """(params, value) → 可读时间字符串。"""
    if not prop:
        return None
    params, value = prop
    dt = _parse_dt(value, params)
    if dt is None:
        return value
    if dt.tzinfo:
        local = dt.strftime("%Y-%m-%d %H:%M:%S %Z").strip()
        utc = dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return f"{local}  (UTC: {utc})"
    tzid = params.get("TZID")
    suffix = f"  (时区: {tzid})" if tzid else ""
    return dt.strftime("%Y-%m-%d %H:%M:%S") + suffix


def parse_ics(text):
    """返回 VEVENT 列表，每个是 {FIELD: (params, value), 'ATTENDEES': [...]}"""
    events, cur, in_evt = [], None, False
    for line in _unfold_ics(text):
        u = line.strip().upper()
        if u == "BEGIN:VEVENT":
            cur, in_evt = {}, True
            continue
        if u == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur, in_evt = None, False
            continue
        if not in_evt or cur is None or not line.strip():
            continue
        name, params, value = _parse_property(line)
        if name is None:
            continue
        if name == "ATTENDEE":
            cur.setdefault("ATTENDEES", []).append((params, value))
        elif name not in cur:
            cur[name] = (params, value)
    return events


def format_event_text(ev):
    out = []
    if "SUMMARY" in ev:
        out.append("主题: " + _unescape(ev["SUMMARY"][1]))
    if "DTSTART" in ev:
        s = _fmt_dt(ev["DTSTART"])
        if s:
            out.append("开始: " + s)
    if "DTEND" in ev:
        s = _fmt_dt(ev["DTEND"])
        if s:
            out.append("结束: " + s)
    if "DURATION" in ev:
        out.append("时长: " + ev["DURATION"][1])
    if "LOCATION" in ev:
        out.append("地点: " + _unescape(ev["LOCATION"][1]))
    if "ORGANIZER" in ev:
        p, v = ev["ORGANIZER"]
        cn = p.get("CN")
        out.append("组织者: " + (_unescape(cn) if cn else v))
    if "ATTENDEES" in ev:
        names = []
        for p, v in ev["ATTENDEES"]:
            cn = p.get("CN")
            names.append(_unescape(cn) if cn else v)
        out.append("参与人: " + ", ".join(names))
    if "URL" in ev:
        out.append("链接: " + ev["URL"][1])
    if "STATUS" in ev:
        out.append("状态: " + ev["STATUS"][1])
    if "RRULE" in ev:
        out.append("重复规则: " + ev["RRULE"][1])
    if "DESCRIPTION" in ev:
        out.append("描述:\n" + _unescape(ev["DESCRIPTION"][1]).strip())
    return "\n".join(out)


def format_events_plain(events):
    if not events:
        return ""
    blocks = []
    for i, ev in enumerate(events, 1):
        head = f"【日历事件 {i}】" if len(events) > 1 else "【日历事件】"
        blocks.append(head + "\n" + format_event_text(ev))
    return "\n\n".join(blocks)


def format_events_html(events):
    plain = format_events_plain(events)
    if not plain:
        return ""
    return (
        '<div style="font-family:sans-serif;font-size:13px;'
        'background:#f6f8fa;border:1px solid #ddd;border-radius:6px;'
        'padding:10px 12px;margin:10px 0;white-space:pre-wrap;">'
        + html_escape(plain) + "</div>"
    )


# ---------- HTML 注入 ----------
def insert_after_body_open(prefix_html, original_html):
    """把 prefix_html 插进 <body> 之后，避免破坏原邮件的 <!DOCTYPE>/<head>/<style>。"""
    m = re.search(r"<body[^>]*>", original_html, re.IGNORECASE)
    if m:
        return original_html[:m.end()] + prefix_html + original_html[m.end():]
    m = re.search(r"</head>", original_html, re.IGNORECASE)
    if m:
        return original_html[:m.end()] + prefix_html + original_html[m.end():]
    return prefix_html + original_html


# ---------- 构建 Brevo payload（不含附件，日历转文本） ----------
def build_email_payload(original_msg):
    original_subject = decode_mime_words(original_msg.get("Subject")) or "(无主题)"
    original_from = decode_mime_words(original_msg.get("From")) or "(未知发件人)"
    original_to = decode_mime_words(original_msg.get("To")) or "(未知收件人)"
    original_date = original_msg.get("Date", "(未知日期)")

    parts = extract_parts(original_msg)
    plain_raw, plain_charset = parts["plain"]
    html_raw, html_charset = parts["html"]
    cal_raw, cal_charset = parts["cal"]

    # 把 ICS 解析成纯文本
    ics_plain, ics_html = "", ""
    if cal_raw is not None:
        try:
            events = parse_ics(cal_raw.decode(cal_charset or "utf-8", errors="replace"))
            ics_plain = format_events_plain(events)
            ics_html = format_events_html(events)
        except Exception as e:
            print("ICS 解析失败:", e)

    note_plain = (
        "---------- 转发邮件 ----------\n"
        f"发件人: {original_from}\n"
        f"日期: {original_date}\n"
        f"主题: {original_subject}\n"
        f"收件人: {original_to}\n"
        "---------- 转发邮件 ----------\n\n"
    )
    note_html = (
        '<div style="font-family:sans-serif;font-size:13px;color:#555;'
        'border-bottom:1px solid #ccc;padding-bottom:8px;margin-bottom:12px;">'
        "---------- 转发邮件 ----------<br>"
        f"发件人: {html_escape(original_from)}<br>"
        f"日期: {html_escape(original_date)}<br>"
        f"主题: {html_escape(original_subject)}<br>"
        f"收件人: {html_escape(original_to)}<br>"
        "---------- 转发邮件 ----------</div>"
    )

    # 纯文本：转发头 + 日历 + 原邮件正文
    plain_text = note_plain
    if ics_plain:
        plain_text += ics_plain + "\n\n"
    if plain_raw is not None:
        plain_text += decode_bytes_to_str(plain_raw, plain_charset)
    elif html_raw is not None:
        plain_text += "(原邮件为 HTML 格式，请用支持 HTML 的客户端查看)"

    # HTML：转发头 + 日历框 插入 <body>，保留原 HTML 结构
    prefix_html = note_html + ics_html
    if html_raw is not None:
        original_html = decode_bytes_to_str(html_raw, html_charset)
        html_content = insert_after_body_open(prefix_html, original_html)
    else:
        html_content = (
            '<div style="font-family:monospace;font-size:13px;white-space:pre-wrap;">'
            + html_escape(plain_text) + "</div>"
        )

    payload = {
        "sender": {"name": "Yahoo Forwarder", "email": SENDER},
        "to": [{"email": FORWARD_TO}],
        "subject": f"Fwd: {original_subject}",
        "textContent": plain_text,
        "htmlContent": html_content,
    }
    return payload, original_subject


# ---------- 通过 Brevo HTTP API 发送 ----------
def send_via_brevo(payload):
    headers = {
        "accept": "application/json",
        "api-key": BREVO_API_KEY,
        "content-type": "application/json",
    }
    resp = requests.post(BREVO_API_URL, json=payload, headers=headers, timeout=30)
    if resp.status_code not in (200, 201, 202):
        raise Exception(f"Brevo API 返回错误 {resp.status_code}: {resp.text}")
    return resp.json()


# ---------- 根据 UID 抓取并转发 ----------
def forward_email_by_uid(uid):
    mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    mail.login(YAHOO_EMAIL, YAHOO_APP_PASSWORD)
    mail.select("INBOX")

    status, msg_data = mail.uid("fetch", str(uid), "(RFC822)")
    if status != "OK" or not msg_data or not msg_data[0]:
        mail.logout()
        return False, f"未找到 UID 为 {uid} 的邮件"

    raw_email = msg_data[0][1]
    if raw_email is None:
        mail.logout()
        return False, f"UID {uid} 对应邮件内容为空（可能已被删除）"

    original_msg = email.message_from_bytes(raw_email)
    mail.logout()

    payload, subject = build_email_payload(original_msg)
    send_via_brevo(payload)

    return True, f"已转发邮件 (UID={uid})：{subject}"


# ---------- Flask 路由 ----------
@app.route("/")
def trigger():
    try:
        uid = request.args.get("uid")
        if not uid:
            return jsonify({"status": "error", "message": "缺少 uid 参数，请使用 /?uid=<邮件UID>"}), 400

        success, message = forward_email_by_uid(uid)
        code = 200 if success else 500
        return jsonify({
            "status": "ok" if success else "error",
            "message": message,
            "uid": uid,
        }), code

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/health")
def health():
    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
