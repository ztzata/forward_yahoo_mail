# Forward Email
Yahoo stopped its auto-forward for free account. If you have imap access, but don't have smtp access (like render.com), this is an alternative.
Vistit https://[yourserver].render.com/?uid=12345, it will forward the email with specific uid from Yahoo email to another email address. You can modify the IMAP_SERVER to fit any email server other than Yahoo. You will need to get a Brevo API Key to send email.


Environment:

YAHOO_EMAIL

YAHOO_APP_PASSWORD

FORWARD_TO

IMAP_SERVER: default to imap.mail.yahoo.com

BREVO_API_KEY:	Brevo API Key

SENDER:	Email Sender, must verified by Brevo first.


Note: You will need your own domain to send email from Brevo server! Public email address will be rejected!
