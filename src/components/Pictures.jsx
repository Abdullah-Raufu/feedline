import React, { useEffect, useState } from 'react'
import { api, tr } from '../api'
import { Icon } from './Icons'
import StoryImage from './StoryImage'
import Lightbox from './Lightbox'
import { readHref } from '../api'

export default function Pictures() {
  const [items, setItems] = useState(null)
  const [error, setError] = useState('')
  const [lightbox, setLightbox] = useState(null)

  useEffect(() => {
    api('/api/gallery?range=14d')
      .then((data) => setItems(data.items))
      .catch((err) => setError(err.message))
  }, [])

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <span className="eyebrow accent">The picture desk</span>
          <h1>{tr('pictures_title', 'News in pictures')}</h1>
          <p className="muted">Every frame, carefully shown. Tap a picture for the full story.</p>
        </div>
        <Icon.Camera width={28} height={28} />
      </header>
      {error && <div className="notice error">{error}</div>}
      {!items && !error && <div className="loading-grid">{Array.from({ length: 8 }, (_, i) => <div className="skeleton" key={i} />)}</div>}
      {items && !items.length && <div className="empty-state"><Icon.Camera width={32} height={32} /><h2>No pictures yet</h2><p>Refresh your newsroom and check back.</p></div>}
      {items && items.length > 0 && (
        <div className="picture-wall">
          {items.map((item) => (
            <figure className="picture-item" key={item.link}>
              <StoryImage item={item} className="frame-tall" onClick={() => setLightbox(item)} />
              <figcaption>
                <span className="topic-pill">{item.topic}</span>
                <a href={readHref(item.link)}>{item.title}</a>
                <small>{item.source} · {item.ago}</small>
              </figcaption>
            </figure>
          ))}
        </div>
      )}
      <Lightbox item={lightbox} onClose={() => setLightbox(null)} />
    </div>
  )
}
