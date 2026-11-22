import React, { useEffect, useState } from 'react'
import { api, BOOT, tr, toast } from '../api'
import { Brand, Icon } from './Icons'

export default function Profile() {
  const [info, setInfo] = useState(null)
  const [error, setError] = useState('')
  const [username, setUsername] = useState(BOOT.user.username)
  const [email, setEmail] = useState(BOOT.user.email || '')
  const [country, setCountry] = useState(BOOT.user.country)
  const [language, setLanguage] = useState(BOOT.user.language)
  const [theme, setTheme] = useState(BOOT.user.theme)
  const [viewMode, setViewMode] = useState(BOOT.user.view_mode)
  const [readerFont, setReaderFont] = useState(BOOT.user.reader_font || 'serif')
  const [currentPw, setCurrentPw] = useState('')
  const [newPw, setNewPw] = useState('')
  const [confirmPw, setConfirmPw] = useState('')
  const [showPw, setShowPw] = useState(false)
  const [busy, setBusy] = useState('')

  useEffect(() => {
    api('/api/profile').then(setInfo).catch((err) => setError(err.message))
  }, [])

  async function save(endpoint, body, key) {
    setBusy(key)
    try {
      await api(endpoint, { method: 'POST', body })
      toast(tr('prefs_changed', 'Preferences saved'))
      if (key !== 'username') window.location.href = '/'
      else setInfo(await api('/api/profile'))
    } catch (err) {
      toast(err.message)
    } finally {
      setBusy('')
    }
  }

  async function logout() {
    try { await api('/api/auth/logout', { method: 'POST', body: {} }); window.location.href = '/login' }
    catch (err) { toast(err.message) }
  }

  return (
    <div className="page profile-page">
      <header className="page-head">
        <div><span className="eyebrow accent">Your account</span><h1>{tr('profile', 'Profile & settings')}</h1>
          <p className="muted">Make the newsroom feel like yours.</p></div>
      </header>
      {error && <div className="notice error">{error}</div>}

      <section className="profile-hero panel">
        <div className="avatar">{BOOT.user.username.slice(0, 1).toUpperCase()}</div>
        <div className="profile-identity">
          <h2>{info?.user?.username || BOOT.user.username}</h2>
          <p>{info?.country_name || BOOT.countries.find((c) => c.code === country)?.name} · {tr('member_since', 'Member since')} {info?.member_since || '—'}</p>
        </div>
        <div className="profile-stat"><b>{info?.saved_count ?? 0}</b><span>{tr('saved', 'Saved')}</span></div>
      </section>

      <div className="settings-grid">
        <section className="panel" id="account">
          <div className="panel-head"><h2>Account</h2><span className="panel-icon"><Icon.User width={17} height={17} /></span></div>
          <form onSubmit={(e) => { e.preventDefault(); save('/api/profile/username', { username }, 'username') }}>
            <label className="field"><span>{tr('username', 'Username')}</span>
              <input value={username} onChange={(e) => setUsername(e.target.value)} /></label>
            <button className="btn ghost" disabled={busy === 'username'}>{busy === 'username' ? 'Saving…' : tr('save_changes', 'Save username')}</button>
          </form>
          <button className="btn ghost danger" onClick={logout}><Icon.Logout width={16} height={16} /> {tr('logout', 'Sign out')}</button>
        </section>

        <section className="panel" id="settings">
          <div className="panel-head"><h2>{tr('settings', 'Preferences')}</h2><span className="panel-icon"><Icon.Spark width={17} height={17} /></span></div>
          <div className="field"><span>{tr('email', 'Email (optional)')}</span>
            <input type="email" value={email} onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com" autoComplete="email" />
            <small className="hint">{tr('email_hint', 'Only needed if you forget your password.')}</small>
          </div>
          <div className="field"><span>{tr('country', 'Country')}</span>
            <select value={country} onChange={(e) => setCountry(e.target.value)}>
              {BOOT.countries.map((c) => <option key={c.code} value={c.code}>{c.name}</option>)}
            </select></div>
          <div className="field"><span>{tr('language', 'Language')}</span>
            <select value={language} onChange={(e) => setLanguage(e.target.value)}>
              {BOOT.languages.map((l) => <option key={l.code} value={l.code}>{l.name}</option>)}
            </select></div>
          <div className="field"><span>{tr('reader_font', 'Reading room font')}</span>
            <select value={readerFont} onChange={(e) => setReaderFont(e.target.value)}>
              {(BOOT.reader_fonts || []).map((font) => (
                <option key={font.code} value={font.code}>{font.name}</option>
              ))}
            </select>
            <small className="hint font-preview" style={{ fontFamily: (BOOT.reader_fonts || []).find((f) => f.code === readerFont)?.stack }}>
              Every good story begins with a clear sentence.
            </small>
          </div>
          <div className="field"><span>{tr('theme', 'Card colour')}</span>
            <div className="theme-swatches">
              {BOOT.themes.map((name) => (
                <button key={name} className={'swatch ' + name + (theme === name ? ' selected' : '')}
                  onClick={() => setTheme(name)} title={tr('theme_' + name, name)}>
                  {theme === name && <Icon.Check width={15} height={15} />}
                </button>
              ))}
            </div>
            <small className="hint">{tr('theme_' + theme, theme)}</small></div>
          <div className="field"><span>{tr('layout', 'Layout')}</span>
            <div className="segmented small">
              <button className={viewMode === 'grid' ? 'active' : ''} onClick={() => setViewMode('grid')}><Icon.Grid width={15} height={15} /> {tr('grid', 'Grid')}</button>
              <button className={viewMode === 'list' ? 'active' : ''} onClick={() => setViewMode('list')}><Icon.List width={15} height={15} /> {tr('list', 'List')}</button>
            </div></div>
          <button className="btn primary" onClick={() => save('/api/profile/prefs', { email, country, language, theme, view_mode: viewMode, reader_font: readerFont }, 'prefs')}>
            {busy === 'prefs' ? 'Saving…' : tr('save_preferences', 'Save preferences')}
          </button>
        </section>

        <section className="panel" id="password">
          <div className="panel-head"><h2>{tr('change_password', 'Change password')}</h2><span className="panel-icon">🔒</span></div>
          <form onSubmit={(e) => { e.preventDefault(); save('/api/profile/password', { current_password: currentPw, new_password: newPw, confirm: confirmPw }, 'password') }}>
            <label className="field"><span>{tr('current_password', 'Current password')}</span>
              <input type={showPw ? 'text' : 'password'} value={currentPw} onChange={(e) => setCurrentPw(e.target.value)} /></label>
            <label className="field"><span>{tr('new_password', 'New password')}</span>
              <input type={showPw ? 'text' : 'password'} value={newPw} onChange={(e) => setNewPw(e.target.value)} /></label>
            <label className="field"><span>{tr('confirm_password', 'Confirm password')}</span>
              <input type={showPw ? 'text' : 'password'} value={confirmPw} onChange={(e) => setConfirmPw(e.target.value)} /></label>
            <label className="checkline"><input type="checkbox" checked={showPw} onChange={(e) => setShowPw(e.target.checked)} /><span>{tr('show', 'Show passwords')}</span></label>
            <button className="btn ghost" disabled={busy === 'password'}>{busy === 'password' ? 'Saving…' : tr('update_password', 'Update password')}</button>
          </form>
        </section>
      </div>
    </div>
  )
}
