import React, { useState } from 'react'
import { api, BOOT, offerPasswordSave, toast, tr } from '../api'
import { Brand, Icon } from './Icons'

export default function ForgotPassword({ navigate }) {
  const [step, setStep] = useState(1)
  const [email, setEmail] = useState('')
  const [hint, setHint] = useState('')
  const [token, setToken] = useState('')
  const [otp, setOtp] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [show, setShow] = useState(false)
  const [remember, setRemember] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

  async function run(job) {
    setBusy(true)
    setError('')
    setMessage('')
    try {
      await job()
    } catch (err) {
      setError(err.message)
      toast(err.message)
    } finally {
      setBusy(false)
    }
  }

  function sendCode(event) {
    event?.preventDefault()
    return run(async () => {
      const result = await api('/api/auth/forgot', {
        method: 'POST',
        body: { email },
      })
      setToken(result.reset_token || '')
      setHint(result.email_hint || email)
      setOtp('')
      setMessage(tr('otp_sent', 'A fresh code is on its way.'))
      setStep(2)
    })
  }

  function verifyCode(event) {
    event.preventDefault()
    return run(async () => {
      const result = await api('/api/auth/verify-otp', {
        method: 'POST',
        body: { token, otp },
      })
      setUsername(result.username || '')
      setStep(3)
    })
  }

  function finish(event) {
    event.preventDefault()
    const form = event.currentTarget
    return run(async () => {
      const result = await api('/api/auth/reset', {
        method: 'POST',
        body: { token, username, password, confirm, remember },
      })
      offerPasswordSave({
        form,
        username,
        password,
        remember,
        next: result.next || '/',
      })
    })
  }

  const titles = [
    tr('forgot_title', 'Reset your account'),
    tr('otp_title', 'Check your email'),
    tr('reset_title', 'Choose new details'),
  ]
  const subtitles = [
    tr('forgot_sub', 'Enter the email on your account and we will send a 6-digit code.'),
    tr('otp_sub', 'We sent a 6-digit code to {email}. It expires in 10 minutes.')
      .replace('{email}', hint),
    tr('reset_sub', 'Pick a new username and password. You will be signed in automatically.'),
  ]

  return (
    <div className="auth-shell">
      <aside className="auth-art">
        <Brand />
        <div className="auth-art-inner">
          <span className="eyebrow"><Icon.Spark width={14} height={14} /> Independent. Intelligent. Yours.</span>
          <h1>The world,<br /><em>beautifully told.</em></h1>
          <p>Your stories are still here. Recover your account and carry on reading in a calm, familiar place.</p>
          <div className="auth-features">
            <div><b>01</b><span>A code sent to your email</span></div>
            <div><b>02</b><span>A new username and password</span></div>
            <div><b>03</b><span>Straight back to your newsroom</span></div>
          </div>
        </div>
      </aside>

      <iframe name="auth-save-frame" title="Save password" hidden />

      <main className="auth-panel">
        <div className="auth-card">
          <div className="mobile-brand"><Brand /></div>

          <div className="step-track" aria-label={`Step ${step} of 3`}>
            {[1, 2, 3].map((n) => (
              <span key={n} className={n === step ? 'active' : n < step ? 'done' : ''}>
                {n < step ? <Icon.Check width={13} height={13} /> : n}
              </span>
            ))}
          </div>

          <span className="eyebrow accent">{tr('forgot_password', 'Forgot password?')}</span>
          <h2>{titles[step - 1]}</h2>
          <p className="muted">{subtitles[step - 1]}</p>

          {error && <div className="form-error" role="alert">{error}</div>}
          {message && <div className="form-notice" role="status">{message}</div>}

          {step === 1 && (
            <form onSubmit={sendCode}>
              <label className="field">
                <span>{tr('email', 'Email (optional)')}</span>
                <input type="email" value={email} onChange={(e) => setEmail(e.target.value)}
                  autoComplete="email" placeholder="you@example.com" required />
              </label>
              <small className="hint">{tr('email_hint', 'Only needed if you forget your password.')}</small>
              <button className="btn primary wide" disabled={busy}>
                {busy ? 'Sending…' : tr('send_code', 'Send code')}
                {!busy && <Icon.Arrow width={17} height={17} />}
              </button>
              <p className="support-note">
                {tr('reset_support', 'No email on your account?')}{' '}
                <a href="tel:08035486527">08035486527</a> {tr('or', 'or')}{' '}
                <a href="mailto:raufuabdullahmail@gmail.com">raufuabdullahmail@gmail.com</a>.
              </p>
            </form>
          )}

          {step === 2 && (
            <form onSubmit={verifyCode}>
              <label className="field">
                <span>{tr('otp', '6-digit code')}</span>
                <input className="otp-input" value={otp}
                  onChange={(e) => setOtp(e.target.value.replace(/\D/g, '').slice(0, 6))}
                  inputMode="numeric" pattern="[0-9]{6}" maxLength={6}
                  autoComplete="one-time-code" placeholder="000000" required />
              </label>
              <button className="btn primary wide" disabled={busy || otp.length !== 6}>
                {busy ? 'Checking…' : tr('verify_code', 'Verify code')}
                {!busy && <Icon.Arrow width={17} height={17} />}
              </button>
              <button type="button" className="btn ghost wide" disabled={busy} onClick={sendCode}>
                {tr('resend_code', 'Resend code')}
              </button>
            </form>
          )}

          {step === 3 && (
            <form onSubmit={finish} action="/api/auth/login" method="post" target="auth-save-frame" autoComplete="on">
              <input type="hidden" name="_csrf" value={BOOT.csrf || ''} />
              <input type="hidden" name="remember" value="" data-credential-remember />
              <label className="field">
                <span>{tr('new_username', 'New username')}</span>
                <input name="username" data-credential-username value={username} onChange={(e) => setUsername(e.target.value)}
                  autoComplete="username" placeholder="Pick a username" required />
              </label>
              <label className="field">
                <span>{tr('password', 'Password')}</span>
                <span className="password-wrap">
                  <input name="new_password" data-credential-password value={password} onChange={(e) => setPassword(e.target.value)}
                    type={show ? 'text' : 'password'} autoComplete="new-password"
                    placeholder="At least 8 characters" required />
                  <button type="button" className="pw-toggle" onClick={() => setShow(!show)}
                    aria-label={show ? tr('hide', 'Hide') : tr('show', 'Show')}>
                    {show ? <Icon.EyeOff width={17} height={17} /> : <Icon.Eye width={17} height={17} />}
                  </button>
                </span>
              </label>
              <label className="field">
                <span>{tr('confirm_password', 'Confirm password')}</span>
                <input name="confirm" value={confirm} onChange={(e) => setConfirm(e.target.value)}
                  type={show ? 'text' : 'password'} autoComplete="new-password"
                  placeholder="Repeat your password" required />
              </label>
              <label className="checkline">
                <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
                <span>{tr('remember', 'Keep me signed in on this device')}</span>
              </label>
              <button className="btn primary wide" disabled={busy}>
                {busy ? 'Saving…' : tr('reset_password', 'Reset and sign in')}
                {!busy && <Icon.Arrow width={17} height={17} />}
              </button>
            </form>
          )}

          <p className="switchline">
            <button className="linkbtn" onClick={() => navigate('/login')}>
              {tr('back_login', 'Back to log in')}
            </button>
          </p>
        </div>
      </main>
    </div>
  )
}
