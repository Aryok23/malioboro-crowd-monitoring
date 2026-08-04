import axios from 'axios'
import { useCallback, useEffect, useRef, useState } from 'react'
import AlertsPanel from '../components/AlertsPanel'
import CameraSelector from '../components/CameraSelector'
import CrowdChart from '../components/CrowdChart'
import DetectionOverlay from '../components/DetectionOverlay'
import LiveFeed from '../components/LiveFeed'
import { formatWibTime } from '../utils/wib'

const API = import.meta.env.VITE_API_URL
const WS_URL = import.meta.env.VITE_WS_URL

// Live rolling-window trend (no backend history — this is a stateless demo
// deployment). 15 x 1-minute buckets built client-side from WS messages.
const TREND_WINDOW_MS = 15 * 60 * 1000
const TREND_BUCKET_MS = 60 * 1000
const TREND_RECOMPUTE_MS = 5000
const ALERT_LOG_CAP = 50

function computeTrendSeries(samples) {
  const now = Date.now()
  const bucketCount = TREND_WINDOW_MS / TREND_BUCKET_MS
  const windowStart = now - TREND_WINDOW_MS
  const buckets = Array.from({ length: bucketCount }, (_, i) => ({
    start: windowStart + i * TREND_BUCKET_MS,
    values: [],
  }))

  for (const s of samples) {
    if (s.t < windowStart) continue
    const idx = Math.floor((s.t - windowStart) / TREND_BUCKET_MS)
    if (idx >= 0 && idx < bucketCount) buckets[idx].values.push(s.orang)
  }

  return buckets.map((b) => ({
    time: formatWibTime(new Date(b.start)),
    avg: b.values.length
      ? Math.round((b.values.reduce((a, v) => a + v, 0) / b.values.length) * 10) / 10
      : null,
    peak: b.values.length ? Math.max(...b.values) : null,
  }))
}

export default function Dashboard() {
  const [cameras, setCameras] = useState([])
  const [detections, setDetections] = useState([])
  const [counts, setCounts] = useState({})
  const [frameSize, setFrameSize] = useState(null)
  const [activeCamera, setActiveCamera] = useState(null)
  const [wsConnected, setWsConnected] = useState(false)
  const [alerts, setAlerts] = useState([])
  const [trendSeries, setTrendSeries] = useState([])

  const wsRef = useRef(null)
  const reconnectTimer = useRef(null)
  // Keep a ref so the WS message handler always sees the latest cameras list
  const camerasRef = useRef([])
  // Raw samples for the rolling trend chart; recomputed into buckets on a timer
  const samplesRef = useRef([])
  const trendTimerRef = useRef(null)

  useEffect(() => {
    axios
      .get(`${API}/cameras`)
      .then(({ data }) => {
        setCameras(data)
        camerasRef.current = data
      })
      .catch(console.error)
  }, [])

  useEffect(() => {
    trendTimerRef.current = setInterval(() => {
      setTrendSeries(computeTrendSeries(samplesRef.current))
    }, TREND_RECOMPUTE_MS)
    return () => clearInterval(trendTimerRef.current)
  }, [])

  const connectWs = useCallback(() => {
    if (wsRef.current) {
      wsRef.current.close()
    }

    const ws = new WebSocket(WS_URL)
    wsRef.current = ws

    ws.onopen = () => {
      setWsConnected(true)
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current)
    }

    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data)
        if (msg.type === 'detection') {
          setDetections(msg.detections || [])
          setCounts(msg.counts || {})
          if (msg.frame_width && msg.frame_height) {
            setFrameSize({ width: msg.frame_width, height: msg.frame_height })
          }
          // Sync dropdown to whatever camera the backend is actually streaming
          setActiveCamera((prev) => {
            if (prev && prev.id === msg.camera_id) return prev
            const cam = camerasRef.current.find((c) => c.id === msg.camera_id)
            return cam || prev
          })

          const now = Date.now()
          samplesRef.current.push({ t: now, orang: msg.counts?.orang || 0 })
          const windowStart = now - TREND_WINDOW_MS
          while (samplesRef.current.length && samplesRef.current[0].t < windowStart) {
            samplesRef.current.shift()
          }

          if (msg.alerts && msg.alerts.length > 0) {
            const camera = camerasRef.current.find((c) => c.id === msg.camera_id)
            const newEntries = msg.alerts.map((a) => ({
              id: `${a.timestamp}-${a.alert_type}-${msg.camera_id}`,
              camera_id: msg.camera_id,
              camera_name: camera?.name || `Kamera ${msg.camera_id}`,
              alert_type: a.alert_type,
              description: a.description,
              trigger_value: a.trigger_value,
              timestamp: a.timestamp,
            }))
            setAlerts((prev) => [...newEntries, ...prev].slice(0, ALERT_LOG_CAP))
          }
        }
      } catch {
        // ignore malformed message
      }
    }

    ws.onerror = () => {
      setWsConnected(false)
    }

    ws.onclose = () => {
      setWsConnected(false)
      reconnectTimer.current = setTimeout(connectWs, 3000)
    }
  }, [])

  useEffect(() => {
    connectWs()
    return () => {
      if (wsRef.current) wsRef.current.close()
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current)
    }
  }, [connectWs])

  function handleCameraSwitch(camera) {
    setActiveCamera(camera)
    setCounts({})
    setDetections([])
    samplesRef.current = []
    setTrendSeries([])
  }

  function handleDismissAlert(id) {
    setAlerts((prev) => prev.filter((a) => a.id !== id))
  }

  function handleClearAlerts() {
    setAlerts([])
  }

  return (
    <div className="min-h-screen bg-gray-950 text-white flex flex-col">
      {/* Top bar */}
      <header className="bg-gray-900 border-b border-gray-700 px-4 sm:px-6 py-3 flex items-center justify-between flex-shrink-0">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-full bg-blue-600 flex items-center justify-center flex-shrink-0">
            <svg className="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                d="M15 10l4.553-2.069A1 1 0 0121 8.82V15a1 1 0 01-1.447.894L15 14M3 8a2 2 0 012-2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V8z" />
            </svg>
          </div>
          <span className="font-semibold text-white text-sm sm:text-base truncate">Malioboro Monitor</span>
        </div>
      </header>

      {/* Body */}
      <div className="flex flex-1 flex-col lg:flex-row overflow-hidden">
        {/* Sidebar (below main content on mobile, left column on desktop) */}
        <aside className="w-full lg:w-72 flex-shrink-0 bg-gray-900 border-b lg:border-b-0 lg:border-r border-gray-700 flex flex-col lg:overflow-y-auto order-2 lg:order-1">
          <div className="p-4 border-b border-gray-700">
            <CameraSelector cameras={cameras} onSwitch={handleCameraSwitch} activeCamera={activeCamera} />
          </div>
          <div className="flex-1 p-4 lg:overflow-y-auto">
            <AlertsPanel alerts={alerts} onDismiss={handleDismissAlert} onClear={handleClearAlerts} />
          </div>
        </aside>

        {/* Main content */}
        <main className="flex-1 flex flex-col overflow-y-auto p-3 sm:p-4 gap-3 sm:gap-4 order-1 lg:order-2">
          <LiveFeed
            camera={activeCamera}
            connected={wsConnected}
            detections={detections}
            frameSize={frameSize}
          />
          <DetectionOverlay counts={counts} />
          <CrowdChart activeCamera={activeCamera} series={trendSeries} />
        </main>
      </div>
    </div>
  )
}
