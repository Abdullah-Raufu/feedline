import React, { useState } from 'react'
import { api, BOOT, toast, tr } from '../api'
import { Brand, Icon } from './Icons'

const FLAGS = { en: '🇬🇧', ar: '🇸🇦', fr: '🇫🇷', es: '🇪🇸', pt: '🇧🇷', sw: '🇰🇪', ur: '🇵🇰', hi: '🇮🇳' }

export default function Welcome({ navigate }) {
  const [language, setLanguage] = useState(BOOT.user?.language || 'en')
  const [busy, setBusy] = useState(false)

  async function save() {
    setBusy(true)
    try {
      await api('/api/auth/welcome', { method: 'POST', body: { language } })
      window.location.href = '/'
    } catch (err) {
      toast(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="welcome-shell">
      <div className="welcome-card">
        <Brand />
        <span className="eyebrow accent">One last thing</span>
        <h1>{tr('welcome_title', 'Choose your language')}</h1>
        <p className="muted">{tr('welcome_sub', 'We will show the app and your greetings in this language. You can change it any time in Settings.')}</p>
        <div className="language-grid">
          {BOOT.languages.map((item) => (
            <button key={item.code}
              className={'language-tile' + (language === item.code ? ' selected' : '')}
              onClick={() => setLanguage(item.code)}>
              <span className="flag">{FLAGS[item.code] || '🌐'}</span>
              <span>{item.name}</span>
              {language === item.code && <Icon.Check width={16} height={16} />}
            </button>
          ))}
        </div>
        <button className="btn primary wide" onClick={save} disabled={busy}>
          {busy ? 'Saving…' : tr('continue', 'Continue to my newsroom')} <Icon.Arrow width={17} height={17} />
        </button>
      </div>
    </div>
  )
}
