import os
import imaplib
import email
import requests
from email.header import decode_header
from flask import Flask, jsonify, request

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


# ---------- 正文解析 ----------
def extract_bodies(msg):
    plain, html_body = None, None
    if msg.is_multipart():
        for part in msg.walk():
            disp = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            ctype = part.get_content_type()
            if ctype == "text/plain" and plain is None:
                plain = part.get_payload(decode=True)
            elif ctype == "text/html" and html_body is None:
                html_body = part.get_payload(decode=True)
    else:
        payload = msg.get_payload(decode=True)
        if msg.get_content_type() == "text/html":
            html_body = payload
        else:
            plain = payload
    return plain, html_body


def decode_bytes_to_str(raw, charset):
    if raw is None:
        return ""
    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")


# ---------- 构建 Brevo payload（不含附件） ----------
def build_email_payload(original_msg):
    original_subject = decode_mime_words(original_msg.get("Subject")) or "(无主题)"
    original_from = decode_mime_words(original_msg.get("From")) or "(未知发件人)"
    original_to = decode_mime_words(original_msg.get("To")) or "(未知收件人)"
    original_date = original_msg.get("Date", "(未知日期)")

    plain_raw, html_raw = extract_bodies(original_msg)

    note_plain = (
        f"---------- 转发邮件 ----------\n"
        f"发件人: {original_from}\n"
        f"日期: {original_date}\n"
        f"主题: {original_subject}\n"
        f"收件人: {original_to}\n"
        f"---------- 转发邮件 ----------\n\n"
    )
    note_html = (
        f"<div style=\"font-family:sans-serif;font-size:13px;color:#555;"
        f"border-bottom:1px solid #ccc;padding-bottom:8px;margin-bottom:12px;\">"
        f"---------- 转发邮件 ----------<br>"
        f"发件人: {original_from}<br>"
        f"日期: {original_date}<br>"
        f"主题: {original_subject}<br>"
        f"收件人: {original_to}<br>"
        f"---------- 转发邮件 ----------</div>"
    )

    plain_text = note_plain
    if plain_raw is not None:
        charset = None
        if original_msg.is_multipart():
            for part in original_msg.walk():
                if part.get_content_type() == "text/plain" and part.get_payload(decode=True) == plain_raw:
                    charset = part.get_content_charset()
                    break
        else:
            charset = original_msg.get_content_charset()
        plain_text += decode_bytes_to_str(plain_raw, charset)
    elif html_raw is not None:
        plain_text += "(原邮件为 HTML 格式，请用支持 HTML 的客户端查看)"

    if html_raw is not None:
        charset = None
        if original_msg.is_multipart():
            for part in original_msg.walk():
                if part.get_content_type() == "text/html" and part.get_payload(decode=True) == html_raw:
                    charset = part.get_content_charset()
                    break
        else:
            charset = original_msg.get_content_charset()
        html_content = note_html + decode_bytes_to_str(html_raw, charset)
    else:
        html_content = f"<pre>{note_plain}{plain_text.replace(note_plain, '')}</pre>"

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
