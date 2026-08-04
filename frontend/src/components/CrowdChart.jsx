import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

// Live rolling-window trend only — there is no backend history in this
// stateless demo deployment. `series` is a 15-minute, 1-minute-bucket window
// computed client-side from WebSocket messages (see Dashboard.jsx).
export default function CrowdChart({ activeCamera, series = [] }) {
  const hasData = series.some((d) => d.avg !== null || d.peak !== null)

  return (
    <div className="bg-gray-900 rounded-xl border border-gray-700 p-3 sm:p-4">
      <div className="flex items-center justify-between mb-3 sm:mb-4 flex-wrap gap-2">
        <h3 className="text-sm font-semibold text-white">
          Tren Kepadatan Pejalan Kaki <span className="text-gray-500 font-normal">(15 menit terakhir)</span>
        </h3>
        {activeCamera && (
          <span className="text-xs text-gray-400 bg-gray-800 px-2 py-1 rounded-lg">
            {activeCamera.name}
          </span>
        )}
      </div>

      {!hasData ? (
        <div className="h-48 sm:h-52 flex items-center justify-center text-gray-600 text-sm text-center px-4">
          Menunggu data live... grafik akan terisi seiring waktu.
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={220}>
          <LineChart data={series} margin={{ top: 5, right: 5, left: -20, bottom: 5 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis
              dataKey="time"
              tick={{ fill: '#9ca3af', fontSize: 11 }}
              axisLine={{ stroke: '#4b5563' }}
              tickLine={false}
              interval="preserveStartEnd"
              minTickGap={20}
            />
            <YAxis
              tick={{ fill: '#9ca3af', fontSize: 11 }}
              axisLine={false}
              tickLine={false}
              width={30}
            />
            <Tooltip
              contentStyle={{
                backgroundColor: '#1f2937',
                border: '1px solid #374151',
                borderRadius: 8,
                color: '#f9fafb',
                fontSize: 12,
              }}
            />
            <Legend wrapperStyle={{ fontSize: 12, color: '#9ca3af' }} />
            <Line
              type="monotone"
              dataKey="avg"
              name="Rata-rata Orang"
              stroke="#3b82f6"
              strokeWidth={2}
              dot={false}
              connectNulls={false}
              activeDot={{ r: 4, fill: '#3b82f6' }}
              isAnimationActive={false}
            />
            <Line
              type="monotone"
              dataKey="peak"
              name="Puncak Orang"
              stroke="#f97316"
              strokeWidth={2}
              dot={false}
              connectNulls={false}
              activeDot={{ r: 4, fill: '#f97316' }}
              isAnimationActive={false}
            />
          </LineChart>
        </ResponsiveContainer>
      )}
    </div>
  )
}
