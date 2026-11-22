import React, { useEffect, useState } from 'react'
import { api, BOOT, offerPasswordSave, toast, tr } from '../api'
import { Brand, Icon } from './Icons'

export default function AuthPage({ path, navigate }) {
  const [mode, setMode] = useState(path === '/signup' ? 'signup' : 'login')
  const [username, setUsername] = useState('')
  const [email, setEmail] = useState('')
  const [country, setCountry] = useState('NG')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [remember, setRemember] = useState(true)
  const [show, setShow] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    setMode(path === '/signup' ? 'signup' : 'login')
  }, [path])

  async function submit(event) {
    event.preventDefault()
    const form = event.currentTarget
    setBusy(true)
    setError('')
    let triedExistingLogin = false
    try {
      if (mode === 'signup') {
        try {
          const result = await api('/api/auth/signup', {
            method: 'POST',
            body: { username, email, country, password, confirm, remember },
          })
          offerPasswordSave({
            form,
            username,
            password,
            remember,
            next: result.next || '/',
          })
          return
        } catch (err) {
          const taken = tr('err_exists', 'That username is already taken.')
          if (err.data?.error !== taken && err.data?.error !== 'That username is already taken.') {
            throw err
          }
          // They may simply be on the wrong tab. If the password matches,
          // sign them in instead of blocking them with "username taken".
          triedExistingLogin = true
          const result = await api('/api/auth/login', {
            method: 'POST',
            body: { username, password, remember },
          })
          offerPasswordSave({
            form,
            username,
            password,
            remember,
            next: result.next || '/',
          })
          return
        }
      }

      const result = await api('/api/auth/login', {
        method: 'POST',
        body: { username, password, remember },
      })
      offerPasswordSave({
        form,
        username,
        password,
        remember,
        next: result.next || '/',
      })
    } catch (err) {
      if (triedExistingLogin) {
        setMode('login')
        navigate('/login')
      }
      setError(err.message)
      toast(err.message)
    } finally {
      setBusy(false)
    }
  }

  function switchMode(next) {
    setMode(next)
    setError('')
    navigate(next === 'signup' ? '/signup' : '/login')
  }

  return (
    <div className="auth-shell">
      <aside className="auth-art">
        <Brand />
        <div className="auth-art-inner">
          <span className="eyebrow"><Icon.Spark width={14} height={14} /> Independent. Intelligent. Yours.</span>
          <h1>The world,<br /><em>beautifully told.</em></h1>
          <p>Breaking stories from the sources you trust, gathered in one calm place and designed for the way you actually read.</p>
          <div className="auth-features">
            <div><b>01</b><span>Local and global headlines</span></div>
            <div><b>02</b><span>A clean, in-app reading room</span></div>
            <div><b>03</b><span>Save what matters, read it later</span></div>
          </div>
        </div>
      </aside>

      <iframe name="auth-save-frame" title="Save password" hidden />

      <main className="auth-panel">
        <div className="auth-card">
          <div className="mobile-brand"><Brand /></div>
          <span className="eyebrow accent">{mode === 'signup' ? tr('signup_title', 'Create your account') : tr('login_title', 'Welcome back')}</span>
          <h2>{mode === 'signup' ? tr('signup_title', 'Create your account') : tr('login_title', 'Welcome back')}</h2>
          <p className="muted">{mode === 'signup' ? tr('signup_sub', 'Headlines that matter, from home and around the world.') : tr('login_sub', 'Your newsroom is waiting.')}</p>

          {error && <div className="form-error" role="alert">{error}</div>}

          <form onSubmit={submit} action="/api/auth/login" method="post" target="auth-save-frame" autoComplete="on">
            <input type="hidden" name="_csrf" value={BOOT.csrf || ''} />
            <input type="hidden" name="remember" value="" data-credential-remember />
            <label className="field">
              <span>{tr('username', 'Username')}</span>
              <input name="username" data-credential-username value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" placeholder="Your username" required />
            </label>

            {mode === 'signup' && (
              <label className="field">
                <span>{tr('email', 'Email (optional)')}</span>
                <input name="email" value={email} onChange={(e) => setEmail(e.target.value)}
                  type="email" autoComplete="email" placeholder="you@example.com" />
                <small className="hint">{tr('email_hint', 'Only needed if you forget your password.')}</small>
              </label>
            )}

            {mode === 'signup' && (
              <label className="field">
                <span>{tr('country', 'Country')}</span>
                <select name="country" value={country} onChange={(e) => setCountry(e.target.value)}>
                  {BOOT.countries.map((c) => <option key={c.code} value={c.code}>{c.name}</option>)}
                </select>
              </label>
            )}

            <label className="field">
              <span>{tr('password', 'Password')}</span>
              <span className="password-wrap">
                <input name={mode === 'signup' ? 'new_password' : 'password'} data-credential-password
                  value={password} onChange={(e) => setPassword(e.target.value)}
                  type={show ? 'text' : 'password'} autoComplete={mode === 'signup' ? 'new-password' : 'current-password'}
                  placeholder="At least 8 characters" required />
                <button type="button" className="pw-toggle" onClick={() => setShow(!show)} aria-label={show ? tr('hide', 'Hide') : tr('show', 'Show')}>
                  {show ? <Icon.EyeOff width={17} height={17} /> : <Icon.Eye width={17} height={17} />}
                </button>
              </span>
            </label>

            {mode === 'login' && (
              <div className="forgot-line">
                <button type="button" className="linkbtn" onClick={() => navigate('/forgot')}>
                  {tr('forgot_password', 'Forgot password?')}
                </button>
              </div>
            )}

            {mode === 'signup' && (
              <label className="field">
                <span>{tr('confirm_password', 'Confirm password')}</span>
                <input name="confirm" value={confirm} onChange={(e) => setConfirm(e.target.value)} type={show ? 'text' : 'password'} autoComplete="new-password" placeholder="Repeat your password" required />
              </label>
            )}

            <label className="checkline">
              <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
              <span>{tr('remember', 'Keep me signed in on this device')}</span>
            </label>
            <small className="hint">{tr('remember_hint', 'You will not need to log in again on this device for six months.')}</small>

            <button className="btn primary wide" disabled={busy}>
              {busy ? 'Please wait…' : mode === 'signup' ? tr('signup', 'Sign up') : tr('login', 'Log in')}
              {!busy && <Icon.Arrow width={17} height={17} />}
            </button>
          </form>

          <p className="switchline">
            {mode === 'signup' ? 'Already have an account?' : 'New to Feedline?'}{' '}
            <button className="linkbtn" onClick={() => switchMode(mode === 'signup' ? 'login' : 'signup')}>
              {mode === 'signup' ? tr('login', 'Log in') : tr('signup', 'Sign up')}
            </button>
          </p>
        </div>
      </main>
    </div>
  )
}
