'use client';

import React, { memo } from 'react';
import { Handle, Position, NodeProps, Node } from '@xyflow/react';
import { motion, AnimatePresence } from 'framer-motion';
import {
  CheckCircle2,
  Circle,
  Clock,
  AlertCircle,
  RefreshCw,
  ShieldAlert,
  Ban,
  Hourglass,
  Zap,
} from 'lucide-react';
import { cn } from '@/lib/utils';

// ── Types ──────────────────────────────────────────────────────────────────

export type ARDONodeStatus =
  | 'pending'
  | 'waiting'
  | 'active'
  | 'retrying'
  | 'completed'
  | 'failed'
  | 'blocked'
  | 'approval_needed';

export type TaskNodeData = {
  label: string;
  status: ARDONodeStatus;
  reasoning?: string;
  eta?: string;
  // ARDO fields
  priorityScore?: number;
  riskScore?: number;
  retryCount?: number;
  maxRetries?: number;
  executionType?: string;
  parallelGroup?: number | null;
  approvalNeeded?: boolean;
  taskId?: string;
  factorBreakdown?: Record<string, number>;
  blockedReason?: string;
};

// ── Status Configuration ───────────────────────────────────────────────────

const STATUS_CONFIG: Record<
  ARDONodeStatus,
  {
    label: string;
    dotClass: string;
    ringClass: string;
    bgClass: string;
    textClass: string;
    icon: React.ComponentType<{ className?: string }>;
    animate: boolean;
  }
> = {
  pending: {
    label: 'Queued',
    dotClass: 'bg-zinc-300',
    ringClass: 'border-zinc-200',
    bgClass: 'bg-white',
    textClass: 'text-zinc-400',
    icon: Circle,
    animate: false,
  },
  waiting: {
    label: 'Waiting',
    dotClass: 'bg-sky-400',
    ringClass: 'border-sky-200',
    bgClass: 'bg-sky-50/40',
    textClass: 'text-sky-500',
    icon: Hourglass,
    animate: false,
  },
  active: {
    label: 'Running',
    dotClass: 'bg-[#30E3CA] animate-pulse',
    ringClass: 'border-[#30E3CA]/60 shadow-[0_0_20px_rgba(48,227,202,0.15)]',
    bgClass: 'bg-white',
    textClass: 'text-[#11999E]',
    icon: Zap,
    animate: true,
  },
  retrying: {
    label: 'Retrying',
    dotClass: 'bg-amber-400',
    ringClass: 'border-amber-300/60 shadow-[0_0_16px_rgba(251,191,36,0.12)]',
    bgClass: 'bg-amber-50/30',
    textClass: 'text-amber-600',
    icon: RefreshCw,
    animate: true,
  },
  completed: {
    label: 'Done',
    dotClass: 'bg-emerald-500',
    ringClass: 'border-emerald-200/60',
    bgClass: 'bg-emerald-50/20',
    textClass: 'text-emerald-600',
    icon: CheckCircle2,
    animate: false,
  },
  failed: {
    label: 'Failed',
    dotClass: 'bg-rose-500',
    ringClass: 'border-rose-200/60',
    bgClass: 'bg-rose-50/20',
    textClass: 'text-rose-600',
    icon: AlertCircle,
    animate: false,
  },
  blocked: {
    label: 'Blocked',
    dotClass: 'bg-zinc-400',
    ringClass: 'border-zinc-200/60',
    bgClass: 'bg-zinc-50/40',
    textClass: 'text-zinc-500',
    icon: Ban,
    animate: false,
  },
  approval_needed: {
    label: 'Approval',
    dotClass: 'bg-violet-500 animate-pulse',
    ringClass: 'border-violet-400/60 shadow-[0_0_20px_rgba(139,92,246,0.15)]',
    bgClass: 'bg-violet-50/30',
    textClass: 'text-violet-600',
    icon: ShieldAlert,
    animate: true,
  },
};

// ── Priority Badge ─────────────────────────────────────────────────────────

function PriorityBadge({ score }: { score: number }) {
  const level = score >= 0.7 ? 'HIGH' : score >= 0.4 ? 'MED' : 'LOW';
  const color =
    level === 'HIGH'
      ? 'text-rose-500 bg-rose-50 border-rose-100'
      : level === 'MED'
      ? 'text-amber-500 bg-amber-50 border-amber-100'
      : 'text-emerald-600 bg-emerald-50 border-emerald-100';
  return (
    <span
      className={cn(
        'inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-md border text-[9px] font-black uppercase tracking-widest leading-none',
        color
      )}
    >
      P:{level}
    </span>
  );
}

// ── Risk Dot ───────────────────────────────────────────────────────────────

function RiskDot({ score }: { score: number }) {
  const color =
    score >= 0.7
      ? 'bg-rose-500'
      : score >= 0.4
      ? 'bg-amber-400'
      : 'bg-emerald-400';
  return (
    <span
      title={`Risk: ${(score * 100).toFixed(0)}%`}
      className={cn('inline-block h-2 w-2 rounded-full flex-shrink-0', color)}
    />
  );
}

// ── Main Component ─────────────────────────────────────────────────────────

const TaskNodeCard = ({ data, selected }: NodeProps<Node<TaskNodeData>>) => {
  const status = (data.status ?? 'pending') as ARDONodeStatus;
  const cfg = STATUS_CONFIG[status] ?? STATUS_CONFIG.pending;
  const StatusIcon = cfg.icon;

  const isActive = status === 'active';
  const isRetrying = status === 'retrying';
  const isApproval = status === 'approval_needed';

  return (
    <motion.div
      initial={{ scale: 0.94, opacity: 0 }}
      animate={{ scale: 1, opacity: 1 }}
      transition={{ type: 'spring', stiffness: 260, damping: 22 }}
      className={cn(
        'relative min-w-[260px] max-w-[300px] rounded-2xl border-2 transition-all duration-300',
        selected ? 'border-[#30E3CA] shadow-xl shadow-[#30E3CA]/10' : cfg.ringClass,
        cfg.bgClass
      )}
    >
      {/* Active glow overlay */}
      <AnimatePresence>
        {(isActive || isApproval) && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className={cn(
              'absolute inset-0 rounded-[14px] pointer-events-none',
              isApproval
                ? 'bg-gradient-to-br from-violet-100/30 to-transparent'
                : 'bg-gradient-to-br from-[#30E3CA]/8 to-transparent'
            )}
          />
        )}
      </AnimatePresence>

      <div className="relative rounded-[14px] p-4 flex flex-col gap-3">
        {/* ── Header ────────────────────────────────────────────────── */}
        <div className="flex items-center justify-between gap-2">
          {/* Status badge */}
          <div className="flex items-center gap-1.5">
            <span className={cn('h-1.5 w-1.5 rounded-full flex-shrink-0', cfg.dotClass)} />
            <span
              className={cn(
                'text-[10px] font-black uppercase tracking-widest leading-none',
                cfg.textClass
              )}
            >
              {cfg.label}
            </span>
          </div>

          {/* Right cluster: priority + risk + eta */}
          <div className="flex items-center gap-1.5">
            {data.priorityScore != null && (
              <PriorityBadge score={data.priorityScore} />
            )}
            {data.riskScore != null && <RiskDot score={data.riskScore} />}
            {data.eta && (
              <div className="flex items-center gap-1 text-[9px] font-bold text-zinc-400 uppercase tracking-tight">
                <Clock className="h-2.5 w-2.5" />
                {data.eta}
              </div>
            )}
          </div>
        </div>

        {/* ── Task Title ─────────────────────────────────────────────── */}
        <div className="flex items-start gap-2">
          <div
            className={cn(
              'mt-0.5 h-7 w-7 rounded-lg flex items-center justify-center flex-shrink-0 border',
              status === 'active'
                ? 'bg-[#E4F9F5] border-[#30E3CA]/30 text-[#11999E]'
                : status === 'completed'
                ? 'bg-emerald-50 border-emerald-100 text-emerald-500'
                : status === 'failed'
                ? 'bg-rose-50 border-rose-100 text-rose-500'
                : status === 'approval_needed'
                ? 'bg-violet-50 border-violet-100 text-violet-500'
                : status === 'retrying'
                ? 'bg-amber-50 border-amber-100 text-amber-500'
                : 'bg-zinc-50 border-zinc-100 text-zinc-400'
            )}
          >
            <motion.div
              animate={
                isActive || isApproval
                  ? { rotate: 360 }
                  : isRetrying
                  ? { rotate: [-10, 10, -10] }
                  : {}
              }
              transition={
                isActive
                  ? { repeat: Infinity, duration: 2.5, ease: 'linear' }
                  : isRetrying
                  ? { repeat: Infinity, duration: 0.8 }
                  : {}
              }
            >
              <StatusIcon className="h-3.5 w-3.5" />
            </motion.div>
          </div>

          <h3 className="text-sm font-bold text-zinc-800 leading-snug tracking-tight text-left">
            {data.label}
          </h3>
        </div>

        {/* ── ARDO Metadata Row ──────────────────────────────────────── */}
        {(data.retryCount != null || data.executionType || data.parallelGroup != null) && (
          <div className="flex items-center gap-2 flex-wrap">
            {data.retryCount != null && data.retryCount > 0 && (
              <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-md bg-amber-50 border border-amber-100 text-[9px] font-black text-amber-600 uppercase tracking-widest">
                <RefreshCw className="h-2.5 w-2.5" />
                {data.retryCount}/{data.maxRetries ?? 3}
              </span>
            )}
            {data.executionType === 'browser' && (
              <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-md bg-teal-50 border border-teal-200 text-[9px] font-black text-teal-700 uppercase tracking-widest">
                🌐 Browser
              </span>
            )}
            {data.executionType === 'parallel' && (
              <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-md bg-sky-50 border border-sky-100 text-[9px] font-black text-sky-500 uppercase tracking-widest">
                ∥ Parallel
              </span>
            )}
            {data.parallelGroup != null && (
              <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-md bg-zinc-50 border border-zinc-100 text-[9px] font-bold text-zinc-400 uppercase tracking-widest">
                G{data.parallelGroup}
              </span>
            )}
          </div>
        )}

        {/* ── Reasoning & Explainability ─────────────────────────────── */}
        {data.reasoning && (
          <div className="bg-zinc-50/80 border border-zinc-100 rounded-xl p-3">
            <p className="text-[11px] font-medium text-zinc-500 leading-relaxed">
              {data.reasoning}
            </p>
          </div>
        )}
        
        {status === 'blocked' && data.blockedReason && (
          <div className="bg-rose-50/50 border border-rose-100 rounded-xl p-3">
            <div className="flex items-center gap-1.5 mb-1 text-[9px] font-bold text-rose-500 uppercase tracking-widest">
              <Ban className="h-3 w-3" /> Blocked Reason
            </div>
            <p className="text-[11px] font-medium text-rose-600/80 leading-relaxed">
              {data.blockedReason}
            </p>
          </div>
        )}

        {data.factorBreakdown && Object.keys(data.factorBreakdown).length > 0 && (
          <div className="grid grid-cols-2 gap-2 mt-1">
            {Object.entries(data.factorBreakdown).map(([factor, score]) => (
              <div key={factor} className="flex items-center justify-between bg-zinc-50 border border-zinc-100 px-2 py-1 rounded-md">
                <span className="text-[9px] font-bold text-zinc-400 uppercase tracking-wider">{factor}</span>
                <span className="text-[10px] font-black text-zinc-600">{(score * 100).toFixed(0)}</span>
              </div>
            ))}
          </div>
        )}

        {/* ── Footer progress bar ────────────────────────────────────── */}
        <div className="pt-1 flex items-center gap-1.5">
          {[1, 2, 3, 4].map((i) => (
            <motion.div
              key={i}
              className={cn(
                'h-0.5 flex-1 rounded-full',
                status === 'completed'
                  ? 'bg-emerald-400'
                  : status === 'active' && i === 1
                  ? 'bg-[#30E3CA]'
                  : status === 'retrying' && i <= 2
                  ? 'bg-amber-400'
                  : status === 'failed'
                  ? 'bg-rose-400/30'
                  : 'bg-zinc-100'
              )}
              animate={isActive && i === 1 ? { scaleX: [0.3, 1, 0.3] } : {}}
              transition={
                isActive && i === 1
                  ? { repeat: Infinity, duration: 1.5 }
                  : {}
              }
            />
          ))}
        </div>
      </div>

      {/* ── Handles ────────────────────────────────────────────────────── */}
      <Handle
        type="target"
        position={Position.Top}
        className="!w-3 !h-3 !bg-white !border-2 !border-zinc-200 hover:!border-[#30E3CA] !transition-colors"
      />
      <Handle
        type="source"
        position={Position.Bottom}
        className="!w-3 !h-3 !bg-white !border-2 !border-zinc-200 hover:!border-[#30E3CA] !transition-colors"
      />
    </motion.div>
  );
};

export default memo(TaskNodeCard);
