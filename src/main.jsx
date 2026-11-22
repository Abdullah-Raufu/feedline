import React, { useCallback, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { BOOT } from './api'
import App from './App'
import AuthPage from './components/AuthPage'
import ForgotPassword from './components/ForgotPassword'
import Welcome from './components/Welcome'
import './styles.css'

function useLocation() {
  const [url, setUrl] = useState(() => window.location.pathname + window.location.search)
  useEffect(() => {
    const onPop = () => setUrl(window.location.pathname + window.location.search)
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])
  const navigate = useCallback((to, { replace = false } = {}) => {
    if (replace) window.history.replaceState({}, '', to)
    else window.history.pushState({}, '', to)
    setUrl(to)
    window.scrollTo(0, 0)
  }, [])
  return [url, navigate]
}

function Root() {
  const [url, navigate] = useLocation()
  const user = BOOT.user
  const path = url.split('?')[0]
  let page
  if (!user) {
    page = path === '/forgot'
      ? <ForgotPassword navigate={navigate} />
      : <AuthPage path={path} navigate={navigate} />
  }
  else if (!user.welcome_done) page = <Welcome navigate={navigate} />
  else page = <App url={url} navigate={navigate} path={path} />

  return (
    <>
      <div id="toast" className="toast" role="status" aria-live="polite" />
      {page}
    </>
  )
}

createRoot(document.getElementById('root')).render(<Root />)
