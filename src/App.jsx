import React, { useEffect, useState } from 'react'
import { api, BOOT, subscribeActivity } from './api'
import { Brand, Icon } from './components/Icons'
import Home from './components/Home'
import Saved from './components/Saved'
import Pictures from './components/Pictures'
import Reader from './components/Reader'
import Profile from './components/Profile'

const NAV = [
  { path: '/', label: 'Home', icon: Icon.Home },
  { path: '/saved', label: 'Saved', icon: Icon.Bookmark },
  { path: '/pictures', label: 'Pictures', icon: Icon.Camera },
  { path: '/profile', label: 'Profile', icon: Icon.User },
]

export default function App({ url, navigate, path }) {
  const [search, setSearch] = useState('')
  const [menu, setMenu] = useState(false)
  const [busy, setBusy] = useState(false)

  useEffect(() => subscribeActivity(setBusy), [])

  useEffect(() => {
    document.documentElement.lang = BOOT.lang || 'en'
    document.documentElement.dir = BOOT.rtl ? 'rtl' : 'ltr'
    document.body.dataset.theme = BOOT.user?.theme || 'classic'
  }, [])

  function go(event, to) {
    event.preventDefault()
    setMenu(false)
    navigate(to)
  }

  function submitSearch(event) {
    event.preventDefault()
    navigate(search.trim() ? `/?q=${encodeURIComponent(search.trim())}` : '/')
  }

  const isReader = path === '/read'
  let content
  if (isReader) content = <Reader url={url} navigate={navigate} />
  else if (path === '/saved') content = <Saved />
  else if (path === '/pictures') content = <Pictures />
  else if (path === '/profile' || path === '/settings') content = <Profile />
  else content = <Home url={url} navigate={navigate} />

  return (
    <div className={'app-shell' + (isReader ? ' reading' : '')}>
      {busy && <div className="api-progress" role="progressbar" aria-label="Loading"><span /></div>}
      {!isReader && (
        <aside className="sidebar">
          <a href="/" onClick={(e) => go(e, '/')}><Brand /></a>
          <nav className="side-nav">
            {NAV.map((item) => {
              const active = item.path === '/' ? path === '/' : path.startsWith(item.path)
              return (
                <a key={item.path} href={item.path} onClick={(e) => go(e, item.path)}
                  className={active ? 'active' : ''}>
                  <item.icon width={19} height={19} /><span>{item.label}</span>
                </a>
              )
            })}
          </nav>
          <div className="side-card">
            <span className="eyebrow">The daily brief</span>
            <p>Local voices. Global perspective. One quiet place to read.</p>
          </div>
          <button className="side-user" onClick={() => navigate('/profile')}>
            <span className="avatar small">{BOOT.user.username.slice(0, 1).toUpperCase()}</span>
            <span><b>{BOOT.user.username}</b><small>{BOOT.countries.find((c) => c.code === BOOT.user.country)?.name}</small></span>
          </button>
        </aside>
      )}

      <div className="main-column">
        <header className="topbar">
          <a href="/" onClick={(e) => go(e, '/')} className="mobile-brand"><Brand compact /></a>
          {!isReader && (
            <form className="top-search" onSubmit={submitSearch}>
              <Icon.Search width={17} height={17} />
              <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search the newsroom…" aria-label="Search" />
            </form>
          )}
          {isReader && <span className="reader-top-label">Reading room</span>}
          <div className="top-actions">
            {!isReader && <a className="top-icon" href="/pictures" onClick={(e) => go(e, '/pictures')} title="Pictures"><Icon.Camera width={18} height={18} /></a>}
            <button className="avatar small top-avatar" onClick={() => setMenu(!menu)} aria-label="Account menu">
              {BOOT.user.username.slice(0, 1).toUpperCase()}
            </button>
            {menu && (
              <div className="account-menu">
                <div><b>{BOOT.user.username}</b><small>{BOOT.countries.find((c) => c.code === BOOT.user.country)?.name}</small></div>
                <a href="/profile" onClick={(e) => go(e, '/profile')}><Icon.User width={16} height={16} /> Profile & settings</a>
                <a href="/saved" onClick={(e) => go(e, '/saved')}><Icon.Bookmark width={16} height={16} /> Saved stories</a>
                <button onClick={async () => { await api('/api/auth/logout', { method: 'POST', body: {} }); window.location.href = '/login' }}>
                  <Icon.Logout width={16} height={16} /> Sign out
                </button>
              </div>
            )}
          </div>
        </header>

        <main className="main-content">{content}</main>

        <footer className="app-footer">
          <div className="footer-brand">
            <b>© 2026 Feedline</b>
            <span>All rights reserved.</span>
          </div>
          <div className="footer-contact">
            <span>Complaints &amp; support</span>
            <a href="tel:08035486527">08035486527</a>
            <span className="dot">·</span>
            <a href="mailto:raufuabdullahmail@gmail.com">raufuabdullahmail@gmail.com</a>
          </div>
        </footer>

        {!isReader && (
          <nav className="bottom-nav">
            {NAV.map((item) => {
              const active = item.path === '/' ? path === '/' : path.startsWith(item.path)
              return <a key={item.path} href={item.path} onClick={(e) => go(e, item.path)} className={active ? 'active' : ''}>
                <item.icon width={20} height={20} /><span>{item.label}</span>
              </a>
            })}
          </nav>
        )}
      </div>
    </div>
  )
}
