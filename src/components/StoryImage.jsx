import React, { useEffect, useRef, useState } from 'react'
import { api, imageSrc } from '../api'

/**
 * A picture frame that always resolves: proxy first, then the publisher's own
 * copy, then a server-side replacement, and finally a typographic tile.
 */
export default function StoryImage({ item, eager = false, className = '', onClick }) {
  const original = item.image || ''
  const [src, setSrc] = useState(imageSrc(original))
  const [failed, setFailed] = useState(!original)
  const [repairing, setRepairing] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const triedDirect = useRef(false)
  const triedRepair = useRef(false)
  const imgRef = useRef(null)
  const lastImage = useRef(item.image)

  useEffect(() => {
    // A cached picture can fire onLoad before this effect runs. Keep that
    // loaded state when the image has not changed; only reset for a new story.
    const changed = lastImage.current !== item.image
    lastImage.current = item.image
    const img = imgRef.current
    setSrc(imageSrc(item.image || ''))
    setFailed(!item.image)
    setRepairing(false)
    setLoaded(!changed && Boolean(img && img.complete && img.naturalWidth > 0))
    triedDirect.current = false
    triedRepair.current = false
  }, [item.image])

  async function onError() {
    if (failed) return
    if (!triedDirect.current && original) {
      triedDirect.current = true
      setSrc(original)
      return
    }
    if (!triedRepair.current && item.link) {
      triedRepair.current = true
      setRepairing(true)
      try {
        const result = await api('/api/image', { method: 'POST', body: { link: item.link } })
        if (result.image) setSrc(imageSrc(result.image))
        else setFailed(true)
      } catch (_) {
        setFailed(true)
      } finally {
        setRepairing(false)
      }
      return
    }
    setFailed(true)
  }

  return (
    <div
      className={'frame ' + className + (failed ? ' frame-empty' : '') + (loaded || failed ? ' is-loaded' : '')}
      onClick={onClick}
    >
      {!failed ? (
        <img ref={imgRef} src={src} alt="" loading={eager ? 'eager' : 'lazy'} decoding="async"
          referrerPolicy="no-referrer" onLoad={() => setLoaded(true)} onError={onError} />
      ) : (
        <span className="frame-tile" style={{ '--tile': item.color || '#6366f1' }}>
          {item.initial || (item.source || 'N').slice(0, 1)}
        </span>
      )}
      <span className="frame-gloss" aria-hidden="true" />
      {item.local && <span className="frame-badge">Local</span>}
      {onClick && <button className="frame-zoom" aria-label="View image" onClick={(e) => { e.stopPropagation(); onClick() }}>⤢</button>}
    </div>
  )
}
