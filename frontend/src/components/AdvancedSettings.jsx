import { useState } from 'react'
import InfoTip from './InfoTip'
import { PARAM_SPECS, DEFAULT_PARAMS, STRICT_GROUNDING_TOOLTIP } from '../queryParams'

// Collapsible control panel for the query state space (retrieval + generation hyperparameters)
export default function AdvancedSettings({ params, onChange, disabled }) {
  const [open, setOpen] = useState(false)
  const set = (key, value) => onChange({ ...params, [key]: value })

  const summary = [
    `k=${params.k_hops}`,
    `${params.top_k_chunks} chunk${params.top_k_chunks === 1 ? '' : 's'}`,
    `T ${params.temperature.toFixed(2)}`,
    `p ${params.top_p.toFixed(2)}`,
    params.strict_grounding ? 'strict' : 'open'
  ]

  return (
    <div className={`advanced-settings${open ? ' is-open' : ''}`}>
      <button
        type="button"
        className="advanced-header"
        aria-expanded={open}
        aria-controls="advanced-settings-body"
        onClick={() => setOpen(!open)}
      >
        <span className="advanced-title">
          <span className="advanced-caret" aria-hidden="true">{open ? '▾' : '▸'}</span>
          Advanced Settings
        </span>
        <span className="advanced-summary">
          {summary.map((s) => <span key={s} className="advanced-chip">{s}</span>)}
        </span>
      </button>

      {open && (
        <div id="advanced-settings-body" className="advanced-body">
          {Object.entries(PARAM_SPECS).map(([key, spec]) => (
            <div key={key} className="param-row">
              <div className="param-label">
                <label htmlFor={`param-${key}`}>{spec.label}</label>
                <InfoTip text={spec.tooltip} />
                <span className="param-value">{spec.format(params[key])}</span>
              </div>
              <input
                id={`param-${key}`}
                type="range"
                min={spec.min}
                max={spec.max}
                step={spec.step}
                value={params[key]}
                disabled={disabled}
                onChange={(e) => set(key, Number(e.target.value))}
              />
              <div className="param-scale" aria-hidden="true">
                <span>{spec.min}</span><span>{spec.max}</span>
              </div>
            </div>
          ))}

          <div className="param-row param-row-toggle">
            <div className="param-label">
              <span id="param-strict-label">Strict Grounding</span>
              <InfoTip text={STRICT_GROUNDING_TOOLTIP} />
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={params.strict_grounding}
              aria-labelledby="param-strict-label"
              className="toggle-switch"
              disabled={disabled}
              onClick={() => set('strict_grounding', !params.strict_grounding)}
            >
              <span className="toggle-knob" />
            </button>
          </div>

          <div className="advanced-footer">
            <button type="button" className="reset-btn" disabled={disabled} onClick={() => onChange({ ...DEFAULT_PARAMS })}>
              Reset defaults
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
