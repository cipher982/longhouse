/**
 * OfflineBanner component - Shows when app is offline
 */

import { useState, useEffect } from 'react'

export function OfflineBanner() {
  const [isOnline, setIsOnline] = useState(navigator.onLine)

  useEffect(() => {
    const handleOnline = () => setIsOnline(true)
    const handleOffline = () => setIsOnline(false)

    window.addEventListener('online', handleOnline)
    window.addEventListener('offline', handleOffline)

    return () => {
      window.removeEventListener('online', handleOnline)
      window.removeEventListener('offline', handleOffline)
    }
  }, [])

  if (isOnline) {
    return null
  }

  return (
    <div
      style={{
        position: 'fixed',
        top: 0,
        left: 0,
        right: 0,
        backgroundColor: '#ff9800',
        color: 'white',
        padding: '8px 16px',
        textAlign: 'center',
        zIndex: 9999,
        fontSize: '14px',
        fontWeight: 500
      }}
    >
      ⚠️ Offline - Some features may be unavailable
    </div>
  )
}
