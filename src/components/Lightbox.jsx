import React, { useEffect } from 'react'
import { Icon } from './Icons'
import { imageSrc, readHref } from '../api'

export default function Lightbox({ item, onClose, onRead }) {
  useEffect(() => {
    if (!item) return undefined
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    document.body.classList.add('no-scroll')
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.classList.remove('no-scroll')
    }
  }, [item, onClose])

  if (!item) return null
  return (
    <div className="lightbox" onClick={onClose} role="dialog" aria-modal="true">
      <button className="lightbox-close" onClick={onClose} aria-label="Close"><Icon.Close /></button>
      <figure className="lightbox-figure" onClick={(e) => e.stopPropagation()}>
        <div className="frame lightbox-frame">
          {item.image ? <img src={imageSrc(item.image)} alt="" /> :
            <span className="frame-tile" style={{ '--tile': item.color || '#6366f1' }}>{item.initial}</span>}
          <span className="frame-gloss" />
        </div>
        <figcaption>
          <span className="topic-pill">{item.topic}</span>
          <h3>{item.title}</h3>
          <p>{item.source} · {item.ago}</p>
          <a className="btn primary" href={readHref(item.link)} onClick={onRead}>Read this story <Icon.Arrow width={16} height={16} /></a>
        </figcaption>
      </figure>
    </div>
  )
}
