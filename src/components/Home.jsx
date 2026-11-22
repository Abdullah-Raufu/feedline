import React, { useEffect, useMemo, useRef, useState } from 'react'
import { api, readHref, toast, tr } from '../api'
import { Icon } from './Icons'
import StoryCard from './StoryCard'
import Lightbox from './Lightbox'

function mergeGroups(previous = [], incoming = []) {
  const groups = new Map(previous.map((group) => [group.label, [...group.items]]))
  incoming.forEach((group) => {
    groups.set(group.label, [...(groups.get(group.label) || []), ...group.items])
  })
  return [...groups.values()]
}

export default function Home({ url, navigate }) {
  const params = useMemo(() => new URLSearchParams(url.split('?')[1] || ''), [url])
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [query, setQuery] = useState(params.get('q') || '')
  const [lightbox, setLightbox] = useState(null)
  const searchRef = useRef(null)
  const nextOffset = useRef(0)

  const range = params.get('range') || '24h'
  const category = params.get('category') || 'All'
  const view = params.get('view') || 'grid'
  const q = params.get('q') || ''

  function setParam(key, value) {
    const next = new URLSearchParams(params)
    if (!value || value === 'All' || (key === 'view' && value === 'grid')) next.delete(key)
    else next.set(key, value)
    navigate(next.toString() ? `/?${next}` : '/')
  }

  useEffect(() => { setQuery(q) }, [q])

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError('')
    api(`/api/news?${params.toString()}`, { signal: controller.signal })
      .then((next) => {
        nextOffset.current = next.next_offset ?? next.items.length
        setData(next)
      })
      .catch((err) => { if (err.name !== 'AbortError') setError(err.message) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [url])

  useEffect(() => {
    const onKey = (event) => {
      if (event.key === '/' && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName || '')) {
        event.preventDefault()
        searchRef.current?.focus()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])

  async function refresh() {
    setRefreshing(true)
    setError('')
    try {
      await api('/api/refresh', { method: 'POST', body: {} })
      // The server starts the work immediately. Poll its real status rather
      // than adding an artificial delay to the click.
      const deadline = Date.now() + 45000
      let status
      do {
        status = await api('/api/refresh/status')
        if (!status.running) break
        await new Promise((resolve) => window.setTimeout(resolve, 500))
      } while (Date.now() < deadline)
      const freshFlag = params.toString() ? '&fresh=1' : 'fresh=1'
      const next = await api(`/api/news?${params.toString()}${freshFlag}`)
      nextOffset.current = next.next_offset ?? next.items.length
      setData(next)
      toast(tr('updated_message', 'Your newsroom is up to date'))
    } catch (err) {
      toast(err.message)
    } finally {
      setRefreshing(false)
    }
  }

  async function loadMore() {
    if (loadingMore || !data) return
    setLoadingMore(true)
    setError('')
    try {
      const join = params.toString() ? '&' : ''
      const next = await api(`/api/news?${params.toString()}${join}offset=${nextOffset.current}`)
      setData({
        ...next,
        items: [...data.items, ...next.items],
        groups: mergeGroups(data.groups, next.groups),
      })
      nextOffset.current = next.next_offset ?? nextOffset.current + next.items.length
    } catch (err) {
      setError(err.message)
    } finally {
      setLoadingMore(false)
    }
  }

  function submitSearch(event) {
    event.preventDefault()
    setParam('q', query.trim())
  }

  const cards = (items) => items.map((item) => (
    <StoryCard key={item.link} item={item} view={view} onImage={setLightbox} />
  ))

  return (
    <div className="page home-page">
      <header className="page-head">
        <div>
          <span className="eyebrow accent">{new Date().toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' })}</span>
          <h1>{data ? `${data.greeting}, ${window.__NEWS_BOOTSTRAP__.user.username}` : tr('app_name', 'Feedline')}</h1>
          <p className="muted">{q ? `Results for “${q}”` : tr('home_sub', 'A clearer way to keep up with the world.')}</p>
        </div>
        <button className="btn ghost refresh-btn" onClick={refresh} disabled={refreshing}>
          <Icon.Refresh className={refreshing ? 'spin' : ''} width={17} height={17} />
          <span>{refreshing ? tr('refreshing', 'Refreshing…') : tr('refresh', 'Refresh')}</span>
        </button>
      </header>

      <form className="searchbar" onSubmit={submitSearch}>
        <Icon.Search width={19} height={19} />
        <input ref={searchRef} value={query} onChange={(e) => setQuery(e.target.value)}
          placeholder={tr('search_placeholder', 'Search stories, sources, topics…')} aria-label={tr('search', 'Search')} />
        {query && <button type="button" className="clear-search" onClick={() => { setQuery(''); setParam('q', '') }}><Icon.Close width={16} height={16} /></button>}
        <kbd>/</kbd>
      </form>

      <div className="range-row">
        <div className="segmented">
          {(data?.ranges || ['24h', '48h', '7d', '14d']).map((key) => (
            <button key={key} className={range === key ? 'active' : ''} onClick={() => setParam('range', key)}>
              {tr('range_' + key, { '24h': '24 hours', '48h': '48 hours', '7d': '7 days', '14d': '14 days' }[key])}
            </button>
          ))}
        </div>
        <span className="date-pill"><Icon.Clock width={14} height={14} /> {data?.window || ''}</span>
      </div>

      <div className="topic-row">
        <div className="chips">
          {(data?.counts || []).map((entry) => (
            <button key={entry.name}
              className={'chip' + (category === entry.name ? ' active' : '')}
              onClick={() => setParam('category', entry.name)}>
              {entry.name === 'All' ? tr('all_topics', 'All topics') : entry.name}
              <span>{entry.count}</span>
            </button>
          ))}
        </div>
        <div className="view-switch" aria-label="Layout">
          <button className={view === 'grid' ? 'active' : ''} onClick={() => setParam('view', 'grid')} title="Grid view"><Icon.Grid width={16} height={16} /></button>
          <button className={view === 'list' ? 'active' : ''} onClick={() => setParam('view', 'list')} title="List view"><Icon.List width={16} height={16} /></button>
        </div>
      </div>

      {loading && data && (
        <div className="inline-loading" role="status" aria-live="polite">
          <span className="mini-spinner" />
          <span>{tr('loading_stories', 'Loading stories…')}</span>
        </div>
      )}

      {data && (data.widened || data.web || data.search_failed) && (
        <div className="notice">
          <Icon.Spark width={17} height={17} />
          <span>{data.web ? tr('notice_web', 'No local match — here is what we found on the web.')
            : data.widened ? tr('notice_wide', 'Nothing in this window — widened the search across the archive.')
              : tr('notice_fail', 'We could not reach the web search right now. Try different words.')}</span>
        </div>
      )}

      {error && <div className="notice error">{error}</div>}
      {loading && !data && (
        <div className="loading-grid">{Array.from({ length: 6 }, (_, i) => <div className="skeleton" key={i} />)}</div>
      )}

      {data && !loading && !data.items.length && (
        <div className="empty-state">
          <Icon.Search width={32} height={32} />
          <h2>{tr('empty_title', 'Nothing here yet')}</h2>
          <p>{tr('empty_sub', 'Try another time window, a different topic, or refresh your newsroom.')}</p>
        </div>
      )}

      {data && data.query ? (
        <div className={'story-grid ' + (view === 'list' ? 'list' : '') + (loading ? ' is-loading' : '')}>{cards(data.items)}</div>
      ) : data && data.groups.map((group) => (
        <section className="day-group" key={group.label}>
          <h2 className="day-title"><span>{group.label}</span></h2>
          <div className={'story-grid ' + (view === 'list' ? 'list' : '') + (loading ? ' is-loading' : '')}>{cards(group.items)}</div>
        </section>
      ))}

      {data?.has_more && (
        <div className="load-more-row">
          <button className="btn ghost load-more" onClick={loadMore} disabled={loadingMore}>
            {loadingMore ? tr('loading_more', 'Loading more…') : tr('load_more', 'Load more stories')}
          </button>
        </div>
      )}

      {data?.updated && <p className="updated-line">{tr('updated', 'Updated at {time}', 'Updated at {time}').replace('{time}', data.updated)}</p>}
      {lightbox && <Lightbox item={lightbox} onClose={() => setLightbox(null)}
        onRead={(e) => { e.preventDefault(); setLightbox(null) }} />}
    </div>
  )
}
