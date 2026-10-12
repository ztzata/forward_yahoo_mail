import os
import imaplib
import smtplib
import email
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import Header, decode_header
from flask import Flask, jsonify

app = Flask(__name__)

# ---------- 环境变量读取 ----------
YAHOO_EMAIL = os.environ.get("YAHOO_EMAIL")
YAHOO_APP_PASSWORD = os.environ.get("YAHOO_APP_PASSWORD")
FORWARD_TO = os.environ.get("FORWARD_TO", "ztzaztza@gmail.com")

# Yahoo IMAP / SMTP 设置
IMAP_SERVER = "imap.mail.yahoo.com"
IMAP_PORT = 993
SMTP_SERVER = "smtp.mail.yahoo.com"
SMTP_PORT = 465


# ---------- 头部编解码工具 ----------
def decode_mime_words(s):
    """解码 MIME 编码的头部，例如 =?utf-8?B?...?= 还原为原文"""
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


def encode_header(s):
    """把可能含非 ASCII 的字符串安全编码为邮件头部"""
    try:
        s.encode("ascii")
        return s
    except UnicodeEncodeError:
        return Header(s, "utf-8").encode()


# ---------- 正文/附件解析 ----------
def extract_bodies(msg):
    """从邮件中提取 text/plain 和 text/html 正文，返回 (plain, html)"""
    plain, html_body = None, None

    if msg.is_multipart():
        for part in msg.walk():
            # 跳过本身就是附件容器的情况
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


def collect_attachments(msg):
    """收集原始邮件中的附件（含文件名或 Content-Disposition: attachment 的部件）"""
    attachments = []
    if not msg.is_multipart():
        return attachments

    for part in msg.walk():
        ctype = part.get_content_type()
        # 跳过容器类型与正文类型
        if ctype.startswith("multipart/"):
            continue
        if ctype in ("text/plain", "text/html"):
            # 但如果它带文件名，说明是作为附件发送的文本
            if not part.get_filename():
                continue

        disp = str(part.get("Content-Disposition") or "").lower()
        filename = part.get_filename()

        if filename or "attachment" in disp:
            attachments.append(part)

    return attachments


def decode_bytes_to_str(raw, charset):
    if raw is None:
        return ""
    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")


# ---------- 构建转发邮件 ----------
def build_forward_message(original_msg):
    """把原始邮件正文内联到转发邮件中，附件保持为附件"""
    original_subject = decode_mime_words(original_msg.get("Subject")) or "(无主题)"
    original_from = decode_mime_words(original_msg.get("From")) or "(未知发件人)"
    original_to = decode_mime_words(original_msg.get("To")) or "(未知收件人)"
    original_date = original_msg.get("Date", "(未知日期)")

    # 外层用 mixed，便于挂载附件
    forward = MIMEMultipart("mixed")
    forward["From"] = YAHOO_EMAIL
    forward["To"] = FORWARD_TO
    forward["Subject"] = encode_header(f"Fwd: {original_subject}")

    # 内层 alternative，同时提供纯文本与 HTML
    body_container = MIMEMultipart("alternative")

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

    # 纯文本版本：优先取原 plain，没有就从 html 退化为占位
    if plain_raw is not None:
        charset = None
        if original_msg.is_multipart():
            for part in original_msg.walk():
                if part.get_content_type() == "text/plain" and part.get_payload(decode=True) == plain_raw:
                    charset = part.get_content_charset()
                    break
        else:
            charset = original_msg.get_content_charset()
        plain_text = decode_bytes_to_str(plain_raw, charset)
        body_container.attach(MIMEText(note_plain + plain_text, "plain", "utf-8"))
    elif html_raw is not None:
        # 没有纯文本版，就提示读者用 HTML 版查看
        body_container.attach(MIMEText(note_plain + "(原邮件为 HTML 格式，请用支持 HTML 的客户端查看)", "plain", "utf-8"))

    # HTML 版本
    if html_raw is not None:
        charset = None
        if original_msg.is_multipart():
            for part in original_msg.walk():
                if part.get_content_type() == "text/html" and part.get_payload(decode=True) == html_raw:
                    charset = part.get_content_charset()
                    break
        else:
            charset = original_msg.get_content_charset()
        html_text = decode_bytes_to_str(html_raw, charset)
        body_container.attach(MIMEText(note_html + html_text, "html", "utf-8"))

    forward.attach(body_container)

    # 把原始附件挂到外层
    for part in collect_attachments(original_msg):
        forward.attach(part)

    return forward


# ---------- 主流程 ----------
def forward_latest_email():
    # 1. 连接 IMAP，取最新 1 封
    mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    mail.login(YAHOO_EMAIL, YAHOO_APP_PASSWORD)
    mail.select("INBOX")

    status, messages = mail.search(None, "ALL")
    if status != "OK" or not messages[0]:
        mail.logout()
        return False, "收件箱中没有邮件"

    email_ids = messages[0].split()
    latest_id = email_ids[-1]

    status, msg_data = mail.fetch(latest_id, "(RFC822)")
    if status != "OK":
        mail.logout()
        return False, "获取邮件内容失败"

    raw_email = msg_data[0][1]
    original_msg = email.message_from_bytes(raw_email)
    mail.logout()

    # 2. 构建转发邮件（正文内联，附件保留为附件）
    forward_msg = build_forward_message(original_msg)

    # 3. 发送
    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as smtp:
        smtp.login(YAHOO_EMAIL, YAHOO_APP_PASSWORD)
        smtp.sendmail(YAHOO_EMAIL, [FORWARD_TO], forward_msg.as_string())

    subject = decode_mime_words(original_msg.get("Subject")) or "(无主题)"
    return True, f"已转发邮件：{subject}"


# ---------- Flask 路由 ----------
@app.route("/")
def trigger():
    try:
        success, message = forward_latest_email()
        code = 200 if success else 500
        return jsonify({"status": "ok" if success else "error", "message": message}), code
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/health")
def health():
    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
