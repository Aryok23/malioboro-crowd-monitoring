const CLASS_META = {
  orang:   { icon: '🚶', label: 'Pejalan Kaki' },
  sepeda:  { icon: '🚲', label: 'Sepeda' },
  motor:   { icon: '🛵', label: 'Motor' },
  mobil:   { icon: '🚗', label: 'Mobil' },
  bus:     { icon: '🚌', label: 'Bus' },
  truk:    { icon: '🚛', label: 'Truk' },
  bajaj:   { icon: '🛺', label: 'Bajaj' },
  becak:   { icon: '🚡', label: 'Becak' },
  andong:  { icon: '🐴', label: 'Andong' },
}

function cardColor(count) {
  if (count > 30) return 'bg-red-900/60 border-red-700'
  if (count >= 10) return 'bg-yellow-900/60 border-yellow-700'
  return 'bg-gray-800 border-gray-700'
}

function countColor(count) {
  if (count > 30) return 'text-red-300'
  if (count >= 10) return 'text-yellow-300'
  return 'text-green-400'
}

export default function DetectionOverlay({ counts }) {
  return (
    <div>
      <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">
        Deteksi Real-Time
      </h3>
      <div className="grid grid-cols-3 sm:grid-cols-5 md:grid-cols-9 gap-1.5 sm:gap-2">
        {Object.entries(CLASS_META).map(([key, meta]) => {
          const count = counts[key] ?? 0
          return (
            <div
              key={key}
              className={`border rounded-xl p-2 sm:p-3 flex flex-col items-center gap-0.5 sm:gap-1 transition-colors ${cardColor(count)}`}
            >
              <span className="text-lg sm:text-2xl">{meta.icon}</span>
              <span className="text-[10px] sm:text-xs text-gray-400 text-center leading-tight">{meta.label}</span>
              <span className={`text-base sm:text-xl font-bold ${countColor(count)}`}>{count}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}
