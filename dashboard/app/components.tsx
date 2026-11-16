import { useEffect, useRef, useState, type ReactNode } from "react";
import { Activity, ArrowUpRight, Inbox, X } from "lucide-react";
import { bytes, date, title, type HostSample } from "./data";

export function Badge({ status }: { status: string }) {
  const group = ["running", "succeeded", "ready", "passed"].includes(status)
    ? "green"
    : ["failed", "error", "needs_attention"].includes(status)
      ? "red"
      : [
            "unknown",
            "unobserved",
            "queued",
            "pending",
            "collecting",
            "submitting",
          ].includes(status)
        ? "amber"
        : "neutral";
  return (
    <span className={`badge ${group}`}>
      <span
        className={status === "running" ? "status-dot pulse" : "status-dot"}
      />
      {title(status)}
    </span>
  );
}

export function Empty({
  title: heading,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="empty">
      <Inbox size={28} strokeWidth={1.4} />
      <h3>{heading}</h3>
      <p>{children}</p>
    </div>
  );
}

export function Panel({
  title: heading,
  eyebrow,
  action,
  children,
  className = "",
}: {
  title: string;
  eyebrow?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      <div className="panel-heading">
        <div>
          {eyebrow && <span className="eyebrow">{eyebrow}</span>}
          <h2>{heading}</h2>
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

export function Stat({
  label,
  value,
  note,
  icon,
  onClick,
}: {
  label: string;
  value: ReactNode;
  note: string;
  icon: ReactNode;
  onClick?: () => void;
}) {
  return (
    <button className="stat" onClick={onClick} disabled={!onClick}>
      <div className="stat-label">
        {label}
        {icon}
      </div>
      <div className="stat-value">
        {value}
        {onClick && <ArrowUpRight size={19} />}
      </div>
      <div className="stat-note">{note}</div>
    </button>
  );
}

export function Drawer({
  title: heading,
  subtitle,
  children,
  close,
}: {
  title: string;
  subtitle: string;
  children: ReactNode;
  close: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  return (
    <dialog
      ref={dialog}
      className="drawer"
      onCancel={(event) => {
        event.preventDefault();
        close();
      }}
      aria-label={heading}
    >
      <header className="drawer-heading">
        <div>
          <span className="eyebrow">{subtitle}</span>
          <h2>{heading}</h2>
        </div>
        <button
          className="icon-button"
          onClick={close}
          aria-label="Close details"
        >
          <X size={20} />
        </button>
      </header>
      <div className="drawer-body">{children}</div>
    </dialog>
  );
}

export function Facts({ values }: { values: [string, ReactNode][] }) {
  return (
    <dl className="facts">
      {values.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function HostChart({ samples }: { samples: HostSample[] }) {
  const [selected, setSelected] = useState<number | null>(null);
  if (samples.length < 2)
    return (
      <Empty title="Collecting host measurements">
        CPU and memory samples will appear here as they arrive.
      </Empty>
    );
  const first = Date.parse(samples[0].time);
  const span = Math.max(1, Date.parse(samples.at(-1)!.time) - first);
  const point =
    samples[Math.min(selected ?? samples.length - 1, samples.length - 1)];
  const x = (sample: HostSample) =>
    38 + ((Date.parse(sample.time) - first) / span) * 652;
  const path = (value: (sample: HostSample) => number) =>
    samples
      .map((sample, index) => {
        // Break the line when collection stopped, instead of hiding missing samples.
        const gap =
          index === 0 ||
          Date.parse(sample.time) - Date.parse(samples[index - 1].time) >
            20_000;
        return `${gap ? "M" : "L"}${x(sample)},${155 - value(sample) * 1.3}`;
      })
      .join(" ");
  return (
    <div className="host-chart">
      <div className="chart-legend">
        <span>
          <i className="legend-cpu" />
          CPU <strong>{point.cpu_percent.toFixed(1)}%</strong>
        </span>
        <span>
          <i className="legend-ram" />
          Memory <strong>{bytes(point.ram_used_bytes)}</strong>
        </span>
        <time>{date(point.time, true)}</time>
      </div>
      <svg
        viewBox="0 0 710 190"
        role="img"
        aria-label="CPU and memory usage on the agent host"
        tabIndex={0}
        onKeyDown={(event) => {
          if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
            event.preventDefault();
            setSelected(
              Math.max(
                0,
                Math.min(
                  samples.length - 1,
                  (selected ?? samples.length - 1) +
                    (event.key === "ArrowLeft" ? -1 : 1),
                ),
              ),
            );
          }
        }}
        onPointerMove={(event) => {
          const bounds = event.currentTarget.getBoundingClientRect();
          const position =
            (((event.clientX - bounds.left) / bounds.width) * 710 - 38) / 652;
          setSelected(
            Math.max(
              0,
              Math.min(
                samples.length - 1,
                Math.round(position * (samples.length - 1)),
              ),
            ),
          );
        }}
      >
        <title>
          Agent host resource history. Use arrow keys to inspect samples.
        </title>
        {[0, 25, 50, 75, 100].map((value) => (
          <g key={value}>
            <line
              x1="38"
              x2="690"
              y1={155 - value * 1.3}
              y2={155 - value * 1.3}
              className="chart-grid"
            />
            <text x="26" y={159 - value * 1.3} textAnchor="end">
              {value}
            </text>
          </g>
        ))}
        <path
          d={path(
            (sample) => (sample.ram_used_bytes / sample.ram_total_bytes) * 100,
          )}
          className="ram-line"
        />
        <path d={path((sample) => sample.cpu_percent)} className="cpu-line" />
        <line
          x1={x(point)}
          x2={x(point)}
          y1="20"
          y2="155"
          className="chart-cursor"
        />
        <circle
          cx={x(point)}
          cy={155 - point.cpu_percent * 1.3}
          r="4"
          fill="#30745b"
        />
        <text x="38" y="182">
          {date(samples[0].time, true)}
        </text>
        <text x="690" y="182" textAnchor="end">
          {date(samples.at(-1)!.time, true)}
        </text>
      </svg>
      <div className="chart-caption">
        <Activity size={13} />
        Local agent host only. Remote GPU measurements are not inferred.
      </div>
    </div>
  );
}
