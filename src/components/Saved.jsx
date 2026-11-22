import React, { useEffect, useState } from 'react'
import { api, toast, tr } from '../api'
import { Icon } from './Icons'
import StoryCard from './StoryCard'
import Lightbox from './Lightbox'

export default function Saved() {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [lightbox, setLightbox] = useState(null)

  function load() {
    api('/api/saved').then(setData).catch((err) => setError(err.message))
  }
  useEffect(load, [])

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <span className="eyebrow accent">Your library</span>
          <h1>{tr('saved_title', 'Saved stories')}</h1>
          <p className="muted">Everything you kept for later, in one place.</p>
        </div>
        <span className="count-badge">{data?.shown ?? 0}</span>
      </header>
      {error && <div className="notice error">{error}</div>}
      {!data && !error && <div className="loading-grid">{Array.from({ length: 3 }, (_, i) => <div className="skeleton" key={i} />)}</div>}
      {data && !data.items.length && (
        <div className="empty-state"><Icon.Bookmark width={32} height={32} />
          <h2>Nothing saved yet</h2><p>Tap the bookmark on any story and it will wait for you here.</p>
        </div>
      )}
      {data && data.items.length > 0 && (
        <div className="story-grid">{data.items.map((item) =>
          <StoryCard key={item.link} item={item} onImage={setLightbox} />)}</div>
      )}
      <Lightbox item={lightbox} onClose={() => setLightbox(null)} />
    </div>
  )
}
