# Email setup for password recovery

Password recovery sends a 6-digit OTP by email. The app reads SMTP settings from environment variables, so no password is stored in the code.

## Gmail (recommended)

1. Turn on 2-factor authentication on the Gmail account.
2. Create a **App password** at <https://myaccount.google.com/apppasswords>.
3. Use that 16-character app password, not the normal Gmail password.

Open PowerShell in the folder that contains `feedline.py`, then run:

```powershell
$env:NEWS_SMTP_HOST = "smtp.gmail.com"
$env:NEWS_SMTP_PORT = "587"
$env:NEWS_SMTP_USER = "your-address@gmail.com"
$env:NEWS_SMTP_PASSWORD = "your-16-character-app-password"
$env:NEWS_SMTP_FROM = "Feedline <your-address@gmail.com>"
python feedline.py
```

Those variables only last for that PowerShell window. To keep them permanently, set them from **System Properties → Environment Variables → User variables**, then restart PowerShell and the app.

## Other email providers

| Variable | Meaning | Default |
| --- | --- | --- |
| `NEWS_SMTP_HOST` | SMTP server host. Recovery is disabled when this is blank. | _blank_ |
| `NEWS_SMTP_PORT` | SMTP port. | `587` |
| `NEWS_SMTP_USER` | Login username. Can be blank for open relays. | _blank_ |
| `NEWS_SMTP_PASSWORD` | Login password or app password. | _blank_ |
| `NEWS_SMTP_FROM` | Sender shown on the email. | `NEWS_SMTP_USER` |
| `NEWS_SMTP_TLS` | Use STARTTLS on port 587. Set to `0` to disable. | `1` |
| `NEWS_SMTP_SSL` | Use direct SSL on port 465. Set to `1` to enable. | `0` |
| `NEWS_SMTP_TIMEOUT` | Connection timeout in seconds. | `10` |

For port 465, set both `NEWS_SMTP_PORT=465` and `NEWS_SMTP_SSL=1`.

## How the flow works

1. User clicks **Forgot password?** on the login page.
2. They enter the email saved on their account. Email is optional at sign-up; it can be added later in **Profile → Settings**.
3. The app emails a 6-digit code, valid for 10 minutes. Five wrong attempts cancel the request.
4. After the code is verified, the user chooses a new username and password, then is signed in automatically.

If `NEWS_SMTP_HOST` is not configured, the app returns a clear error instead of pretending to send the code.
