import React, { useState } from 'react'
import { api, readHref, toast, tr } from '../api'
import { Icon } from './Icons'
import StoryImage from './StoryImage'

export default function StoryCard({ item, view = 'grid', onOpen, onImage, compact = false }) {
  const [saved, setSaved] = useState(!!item.saved)
  const [busy, setBusy] = useState(false)

  async function toggleSave(event) {
    event.preventDefault()
    event.stopPropagation()
    if (busy) return
    setBusy(true)
    try {
      const result = await api('/api/save', { method: 'POST', body: { link: item.link } })
      setSaved(result.saved)
      toast(result.saved ? tr('toast_saved', 'Saved to your stories') : tr('toast_unsaved', 'Removed from saved'))
    } catch (err) {
      toast(err.message)
    } finally {
      setBusy(false)
    }
  }

  function open(event) {
    if (onOpen) {
      event.preventDefault()
      onOpen(item)
    }
  }

  return (
    <article className={`story-card ${view === 'list' ? 'as-list' : ''} ${compact ? 'compact' : ''}`}
      style={{ '--topic': item.color || '#6366f1' }}>
      <a className="story-media" href={readHref(item.link)} onClick={open} tabIndex={-1} aria-hidden="true">
        <StoryImage item={item} onClick={onImage ? () => onImage(item) : undefined} />
      </a>
      <div className="story-body">
        <div className="story-top">
          <span className="topic-pill">{item.topic}</span>
          {item.just_in && <span className="just-in">Just in</span>}
          <span className="story-time">{item.ago}</span>
        </div>
        <h3 className="story-title">
          <a href={readHref(item.link)} onClick={open}>{item.title}</a>
        </h3>
        {!compact && item.summary && <p className="story-summary">{item.summary}</p>}
        <div className="story-foot">
          <span className="story-source"><i style={{ background: item.color }} />{item.source}</span>
          <div className="story-actions">
            <button className={'icon-btn' + (saved ? ' saved' : '')} onClick={toggleSave}
              title={saved ? tr('unsave', 'Remove from saved') : tr('save', 'Save')}
              aria-label={saved ? tr('unsave', 'Remove from saved') : tr('save', 'Save')}>
              <Icon.Bookmark width={17} height={17} />
            </button>
            <a className="read-link" href={readHref(item.link)} onClick={open}>
              {tr('read', 'Read')} <Icon.Arrow width={15} height={15} />
            </a>
          </div>
        </div>
      </div>
    </article>
  )
}
