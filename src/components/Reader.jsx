import React, { useEffect, useMemo, useRef, useState } from 'react'
import { api, BOOT, imageSrc, readHref, toast, tr } from '../api'
import { Icon } from './Icons'
import StoryImage from './StoryImage'

function Block({ block }) {
  switch (block.kind) {
    case 'deck':
      return <p className="r-deck">{block.text}</p>
    case 'lead':
      return <p className="r-lead">{block.text}</p>
    case 'h2':
      return <h2 className="r-h2"><span>{block.text}</span></h2>
    case 'h3':
      return <h3 className="r-h3">{block.text}</h3>
    case 'quote':
      return <blockquote className="r-quote"><span>“</span>{block.text}</blockquote>
    default:
      return <p className="r-p">{block.text}</p>
  }
}

export default function Reader({ url, navigate }) {
  const storyUrl = useMemo(() => new URLSearchParams(url.split('?')[1] || '').get('u') || '', [url])
  const readerFont = useMemo(() => {
    const code = BOOT.user?.reader_font || 'serif'
    return (BOOT.reader_fonts || []).find((font) => font.code === code)?.stack
      || 'Georgia, "Times New Roman", "Noto Serif", serif'
  }, [])
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [progress, setProgress] = useState(0)
  const [saved, setSaved] = useState(false)
  const [fontSize, setFontSize] = useState(() => Number(localStorage.getItem('readerSize')) || 19)

  useEffect(() => {
    const controller = new AbortController()
    setData(null)
    setError('')
    api('/api/read?u=' + encodeURIComponent(storyUrl), { signal: controller.signal })
      .then((result) => { setData(result); setSaved(result.saved) })
      .catch((err) => { if (err.name !== 'AbortError') setError(err.message) })
    return () => controller.abort()
  }, [storyUrl])

  useEffect(() => {
    const tick = () => {
      const doc = document.documentElement
      const max = doc.scrollHeight - doc.clientHeight
      setProgress(max > 0 ? Math.min(100, Math.round((doc.scrollTop / max) * 100)) : 0)
    }
    window.addEventListener('scroll', tick, { passive: true })
    window.addEventListener('resize', tick)
    tick()
    return () => { window.removeEventListener('scroll', tick); window.removeEventListener('resize', tick) }
  }, [data])

  function changeFont(delta) {
    const next = Math.min(30, Math.max(15, fontSize + delta))
    setFontSize(next)
    localStorage.setItem('readerSize', String(next))
  }

  async function toggleSave() {
    try {
      const result = await api('/api/save', { method: 'POST', body: { link: data.article.link } })
      setSaved(result.saved)
      toast(result.saved ? tr('toast_saved', 'Saved to your stories') : tr('toast_unsaved', 'Removed from saved'))
    } catch (err) {
      toast(err.message)
    }
  }

  function back(event) {
    event.preventDefault()
    if (window.history.length > 1) window.history.back()
    else navigate('/')
  }

  if (error) return <div className="page"><div className="notice error">{error}</div></div>
  if (!data) return <div className="page reader-loading"><div className="skeleton article-skeleton" /></div>

  const { article, reader } = data
  const heroItem = { image: reader.image || article.image, source: article.source, color: '#6366f1', link: article.link }
  const toc = reader.headings || []
  const blocks = reader.blocks || []
  const fallbackBlocks = blocks.length ? blocks : ([{
    kind: 'lead', text: article.summary || 'This publisher does not share its full text with readers outside its site.',
  }])

  return (
    <div className="reader-page" dir={data.rtl ? 'rtl' : 'ltr'}>
      <div className="read-progress"><i style={{ width: progress + '%' }} /></div>
      <article className="reader" style={{ '--read-size': fontSize + 'px', '--read-font': readerFont }}>
        <div className="reader-toolbar">
          <a href="/" onClick={back} className="back-link"><Icon.Back width={17} height={17} /> {tr('reader_back', 'Back')}</a>
          <div className="reader-tools">
            <button onClick={() => changeFont(-1)} title="Smaller text" aria-label="Smaller text"><Icon.Font width={16} height={16} />−</button>
            <span className="size-label">{fontSize}</span>
            <button onClick={() => changeFont(1)} title="Larger text" aria-label="Larger text"><Icon.Font width={17} height={17} />+</button>
            <button className={saved ? 'active' : ''} onClick={toggleSave} title="Save story">
              <Icon.Bookmark width={16} height={16} /> <span>{saved ? tr('unsave', 'Saved') : tr('save', 'Save')}</span>
            </button>
          </div>
        </div>

        <header className="article-head">
          <div className="article-kicker">
            <span className="topic-pill">{article.source}</span>
            <span>{article.ago}</span>
            <span className="dot">·</span>
            <span>{tr('reading_time', '{n} min read').replace('{n}', reader.minutes)}</span>
          </div>
          <h1>{reader.title || article.title}</h1>
          {reader.deck && <p className="article-deck">{reader.deck}</p>}
          {reader.byline && <p className="article-byline">{reader.byline}</p>}
        </header>

        {heroItem.image && <div className="article-hero"><StoryImage item={heroItem} eager /></div>}

        {toc.length >= 2 && (
          <nav className="article-toc" aria-label="In this story">
            <span className="eyebrow accent">In this story</span>
            <ol>{toc.map((heading, index) => (
              <li key={index} className={heading.kind}>{heading.text}</li>
            ))}</ol>
          </nav>
        )}

        <div className="article-body">
          {fallbackBlocks.map((block, index) => <Block key={index} block={block} />)}
          {reader.status !== 'full' && (
            <div className="article-note">
              <Icon.Spark width={18} height={18} />
              <span>{reader.status === 'blocked'
                ? tr('read_blocked', 'This site only shares a preview. You can read the rest on the original page.')
                : tr('read_partial', 'Part of this story is shown. Open the original for the full article.')}</span>
            </div>
          )}
        </div>

        <footer className="article-foot">
          <div className="foot-actions">
            <a className="btn primary" href={reader.original || article.link} target="_blank" rel="noopener">
              {tr('original', 'Open original')} <Icon.External width={16} height={16} />
            </a>
            <a className="btn ghost" href="/" onClick={back}><Icon.Back width={16} height={16} /> {tr('reader_back', 'Back to news')}</a>
          </div>
          <p>Read inside <b>Feedline</b> — no pop-ups, no detours.</p>
        </footer>
      </article>
    </div>
  )
}
