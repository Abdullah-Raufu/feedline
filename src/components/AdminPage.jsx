import React, { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { Icon } from './Icons'

const FILTERS = [
  { id: 'all', label: 'All activity' },
  { id: 'login', label: 'Logins' },
  { id: 'signup', label: 'Sign-ups' },
  { id: 'logout', label: 'Sign-outs' },
  { id: 'reset', label: 'Resets' },
]

const KIND_LABELS = {
  login: 'Log in',
  signup: 'Sign up',
  logout: 'Sign out',
  reset: 'Password reset',
  forgot: 'Recovery',
  password_change: 'Password changed',
}

function kindLabel(kind) {
  return KIND_LABELS[kind] || kind
}

export default function AdminPage() {
  const [overview, setOverview] = useState(null)
  const [events, setEvents] = useState([])
  const [users, setUsers] = useState([])
  const [filter, setFilter] = useState('all')
  const [error, setError] = useState('')
  const [updated, setUpdated] = useState(null)

  const load = useCallback(async () => {
    try {
      const [summary, activity, accounts] = await Promise.all([
        api('/api/admin/overview'),
        api(`/api/admin/events?kind=${filter}&limit=100`),
        api('/api/admin/users'),
      ])
      setOverview(summary.overview)
      setEvents(activity.events)
      setUsers(accounts.users)
      setUpdated(Date.now())
      setError('')
    } catch (err) {
      setError(err.message)
    }
  }, [filter])

  useEffect(() => { load() }, [load])
  useEffect(() => {
    const timer = window.setInterval(load, 30000)
    return () => window.clearInterval(timer)
  }, [load])

  const stats = [
    { label: 'Accounts', value: overview?.users, hint: 'total' },
    { label: 'Logins today', value: overview?.logins_today, hint: 'successful' },
    { label: 'Failed logins', value: overview?.failed_today, hint: 'today' },
    { label: 'Active users', value: overview?.active_week, hint: 'last 7 days' },
    { label: 'Sign-ups', value: overview?.signups_week, hint: 'last 7 days' },
  ]

  return (
    <div className="page admin-page">
      <header className="page-head">
        <div>
          <span className="eyebrow accent">Administration</span>
          <h1>Account activity</h1>
          <p className="muted">See who signs in, when they do it, and where from.</p>
        </div>
        <div className="admin-head-actions">
          {updated && <small className="muted">Updated {new Date(updated).toLocaleTimeString()}</small>}
          <button className="btn ghost" onClick={load}><Icon.Refresh width={15} height={15} /> Refresh</button>
        </div>
      </header>

      {error && <div className="notice error">{error}</div>}

      <section className="admin-stats">
        {stats.map((item) => (
          <div className="panel admin-stat" key={item.label}>
            <span>{item.label}</span>
            <b>{item.value ?? '—'}</b>
            <small>{item.hint}</small>
          </div>
        ))}
      </section>

      <section className="panel admin-panel">
        <div className="panel-head">
          <h2>Sign-ins &amp; activity</h2>
          <span className="panel-icon"><Icon.Shield width={17} height={17} /></span>
        </div>
        <div className="admin-filters">
          {FILTERS.map((item) => (
            <button key={item.id}
              className={'admin-filter' + (filter === item.id ? ' active' : '')}
              onClick={() => setFilter(item.id)}>
              {item.label}
            </button>
          ))}
        </div>
        <div className="table-wrap">
          <table className="admin-table">
            <thead>
              <tr><th>When</th><th>Event</th><th>Account</th><th>Status</th><th>From</th></tr>
            </thead>
            <tbody>
              {events.map((event) => (
                <tr key={event.id}>
                  <td><span className="admin-when">{event.when}</span></td>
                  <td><span className={'admin-kind kind-' + event.kind}>{kindLabel(event.kind)}</span></td>
                  <td>
                    <b>{event.identifier}</b>
                    {event.detail && <small>{event.detail.replace(/_/g, ' ')}</small>}
                  </td>
                  <td>
                    <span className={'admin-status ' + (event.success ? 'ok' : 'fail')}>
                      {event.success ? 'Success' : 'Failed'}
                    </span>
                  </td>
                  <td><span>{event.ip}</span><small>{event.device}</small></td>
                </tr>
              ))}
              {!events.length && (
                <tr><td colSpan="5" className="admin-empty">No activity yet.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="panel admin-panel">
        <div className="panel-head">
          <h2>Accounts</h2>
          <span className="panel-icon"><Icon.User width={17} height={17} /></span>
        </div>
        <div className="table-wrap">
          <table className="admin-table">
            <thead>
              <tr><th>User</th><th>Country</th><th>Joined</th><th>Last login</th><th>Logins</th><th>Saved</th></tr>
            </thead>
            <tbody>
              {users.map((user) => (
                <tr key={user.id}>
                  <td>
                    <span className="admin-user">
                      <span className="avatar small">{user.username.slice(0, 1).toUpperCase()}</span>
                      <span><b>{user.username}</b>{user.is_admin && <em>Admin</em>}<small>{user.email}</small></span>
                    </span>
                  </td>
                  <td>{user.country}</td>
                  <td>{user.joined}</td>
                  <td>{user.last_login}</td>
                  <td>{user.login_count}</td>
                  <td>{user.saved_count}</td>
                </tr>
              ))}
              {!users.length && (
                <tr><td colSpan="6" className="admin-empty">No accounts yet.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <p className="admin-note">
        <Icon.Clock width={15} height={15} />
        On Vercel this history lives in the current server instance. Set <code>NEWS_DB</code>
        to keep it across restarts and devices.
      </p>
    </div>
  )
}
