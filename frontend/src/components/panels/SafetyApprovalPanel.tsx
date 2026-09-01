'use client';

import React from 'react';
import { Shield, ShieldBan, ShieldAlert, ShieldCheck, Lock, CheckCircle2, XCircle, AlertTriangle, Info } from 'lucide-react';
import { cn } from '@/lib/utils';
import { motion } from 'framer-motion';

// ─────────────────────────────────────────────────────────────────────────────
// Types — must match backend safety_schemas.py
// ─────────────────────────────────────────────────────────────────────────────

export type RiskLevel = 'low' | 'medium' | 'high' | 'critical' | 'unknown';
export type ExecutionDecision = 'allow' | 'allow_with_logging' | 'require_approval' | 'block';

export interface SafetyMetadata {
  risk_level:            RiskLevel;
  execution_decision:    ExecutionDecision;
  triggered_policies:    string[];
  explanation:           string;
  semantic_confidence:   number;
  safety_record_id?:     string;
}

interface SafetyApprovalPanelProps {
  taskId:          string;
  taskName:        string;
  safetyMeta:      SafetyMetadata;
  sessionId:       string;
  onApprove?:      (taskId: string) => void;
  onDeny?:         (taskId: string) => void;
  isApproved?:     boolean;
  isDenied?:       boolean;
  className?:      string;
}

// ─────────────────────────────────────────────────────────────────────────────
// Risk level display configuration
// ─────────────────────────────────────────────────────────────────────────────

const RISK_CONFIG: Record<RiskLevel, { label: string; color: string; bg: string; border: string; Icon: React.FC<{ className?: string }> }> = {
  low:      { label: 'LOW',      color: 'text-emerald-400', bg: 'bg-emerald-500/10', border: 'border-emerald-500/30', Icon: ShieldCheck },
  medium:   { label: 'MEDIUM',   color: 'text-amber-400',   bg: 'bg-amber-500/10',   border: 'border-amber-500/30',   Icon: ShieldAlert },
  high:     { label: 'HIGH',     color: 'text-orange-400',  bg: 'bg-orange-500/10',  border: 'border-orange-500/30',  Icon: ShieldAlert },
  critical: { label: 'CRITICAL', color: 'text-red-400',     bg: 'bg-red-500/10',     border: 'border-red-500/30',     Icon: ShieldBan },
  unknown:  { label: 'UNKNOWN',  color: 'text-zinc-400',    bg: 'bg-zinc-500/10',    border: 'border-zinc-500/30',    Icon: Shield },
};

const DECISION_CONFIG: Record<ExecutionDecision, { label: string; color: string; bg: string; border: string }> = {
  allow:              { label: 'ALLOW',              color: 'text-emerald-400', bg: 'bg-emerald-500/10', border: 'border-emerald-500/30' },
  allow_with_logging: { label: 'ALLOW (LOGGED)',     color: 'text-sky-400',     bg: 'bg-sky-500/10',     border: 'border-sky-500/30' },
  require_approval:   { label: 'REQUIRE APPROVAL',  color: 'text-orange-400',  bg: 'bg-orange-500/10',  border: 'border-orange-500/30' },
  block:              { label: 'BLOCKED BY POLICY',  color: 'text-red-400',     bg: 'bg-red-500/10',     border: 'border-red-500/30' },
};

// ─────────────────────────────────────────────────────────────────────────────
// SafetyApprovalPanel Component
// ─────────────────────────────────────────────────────────────────────────────

export default function SafetyApprovalPanel({
  taskId,
  taskName,
  safetyMeta,
  sessionId,
  onApprove,
  onDeny,
  isApproved = false,
  isDenied = false,
  className,
}: SafetyApprovalPanelProps) {
  const risk    = RISK_CONFIG[safetyMeta.risk_level] ?? RISK_CONFIG.unknown;
  const decision = DECISION_CONFIG[safetyMeta.execution_decision] ?? DECISION_CONFIG.block;
  const RiskIcon = risk.Icon;

  const isBlocked        = safetyMeta.execution_decision === 'block';
  const needsApproval    = safetyMeta.execution_decision === 'require_approval';
  const confidencePct    = Math.round((safetyMeta.semantic_confidence ?? 1) * 100);

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className={cn(
        'rounded-xl border backdrop-blur-sm overflow-hidden',
        'bg-[#0f1117]/90',
        isBlocked ? 'border-red-500/40' : needsApproval ? 'border-orange-500/30' : 'border-zinc-700/50',
        className
      )}
    >
      {/* ── Header: Risk + Decision badges ── */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-white/5 bg-white/[0.02]">
        <div className="flex items-center gap-2">
          <RiskIcon className={cn('h-4 w-4', risk.color)} />
          <span className="text-[10px] font-bold text-zinc-400 uppercase tracking-[0.12em] font-mono">
            Safety Evaluation
          </span>
        </div>
        <div className="flex items-center gap-2">
          {/* Risk Level badge */}
          <span className={cn(
            'inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[10px] font-bold tracking-wider border',
            risk.bg, risk.color, risk.border
          )}>
            <RiskIcon className="h-2.5 w-2.5" />
            {risk.label}
          </span>
          {/* Decision badge */}
          <span className={cn(
            'inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[10px] font-bold tracking-wider border',
            decision.bg, decision.color, decision.border
          )}>
            {decision.label}
          </span>
        </div>
      </div>

      <div className="p-4 space-y-4">
        {/* ── Task name ── */}
        <div>
          <p className="text-[10px] font-semibold text-zinc-500 uppercase tracking-widest mb-1">Task</p>
          <p className="text-sm font-medium text-zinc-200 leading-snug truncate">{taskName}</p>
        </div>

        {/* ── Explanation ── */}
        {safetyMeta.explanation && (
          <div className="flex gap-2.5 p-3 rounded-lg bg-white/[0.03] border border-white/[0.05]">
            <Info className="h-3.5 w-3.5 text-zinc-500 mt-0.5 shrink-0" />
            <p className="text-[11px] text-zinc-400 leading-relaxed">{safetyMeta.explanation}</p>
          </div>
        )}

        {/* ── Triggered Policies ── */}
        {safetyMeta.triggered_policies?.length > 0 && (
          <div>
            <p className="text-[10px] font-semibold text-zinc-500 uppercase tracking-widest mb-2 flex items-center gap-1.5">
              <AlertTriangle className="h-3 w-3" />
              Triggered Policies
            </p>
            <div className="flex flex-wrap gap-1.5">
              {safetyMeta.triggered_policies.map(policy => (
                <span
                  key={policy}
                  className="inline-flex items-center px-2 py-0.5 rounded-md bg-zinc-800/80 border border-zinc-700/60 text-[9px] font-mono font-semibold text-zinc-400 tracking-wide"
                >
                  {policy}
                </span>
              ))}
            </div>
          </div>
        )}

        {/* ── Semantic Confidence ── */}
        <div className="flex items-center justify-between text-[10px] text-zinc-600">
          <span className="font-mono">Semantic Confidence</span>
          <span className={cn(
            'font-mono font-bold',
            confidencePct >= 80 ? 'text-emerald-500' :
            confidencePct >= 50 ? 'text-amber-500' : 'text-red-500'
          )}>
            {confidencePct}%
          </span>
        </div>

        {/* ── BLOCK state: locked indicator ── */}
        {isBlocked && (
          <motion.div
            initial={{ opacity: 0, scale: 0.95 }}
            animate={{ opacity: 1, scale: 1 }}
            className="flex items-start gap-3 p-3 rounded-lg bg-red-950/30 border border-red-500/30"
          >
            <Lock className="h-4 w-4 text-red-400 mt-0.5 shrink-0" />
            <div>
              <p className="text-[11px] font-bold text-red-400 mb-0.5">Execution Blocked by Policy</p>
              <p className="text-[10px] text-zinc-500 leading-relaxed">
                Operator approval cannot override this decision. The deterministic safety policy has classified this task as CRITICAL risk. Contact your system administrator if this is incorrect.
              </p>
            </div>
          </motion.div>
        )}

        {/* ── REQUIRE_APPROVAL: Approve/Deny buttons ── */}
        {needsApproval && !isApproved && !isDenied && (
          <div className="flex gap-2 pt-1">
            <button
              onClick={() => onApprove?.(taskId)}
              className={cn(
                'flex-1 flex items-center justify-center gap-2 px-3 py-2.5 rounded-lg text-[11px] font-bold uppercase tracking-wider transition-all',
                'bg-emerald-600/20 border border-emerald-500/40 text-emerald-400',
                'hover:bg-emerald-600/30 hover:border-emerald-400/60 hover:scale-[1.02] active:scale-95'
              )}
            >
              <CheckCircle2 className="h-3.5 w-3.5" />
              Approve Execution
            </button>
            <button
              onClick={() => onDeny?.(taskId)}
              className={cn(
                'flex-1 flex items-center justify-center gap-2 px-3 py-2.5 rounded-lg text-[11px] font-bold uppercase tracking-wider transition-all',
                'bg-red-600/20 border border-red-500/40 text-red-400',
                'hover:bg-red-600/30 hover:border-red-400/60 hover:scale-[1.02] active:scale-95'
              )}
            >
              <XCircle className="h-3.5 w-3.5" />
              Deny Execution
            </button>
          </div>
        )}

        {/* ── Approval resolved state ── */}
        {isApproved && (
          <div className="flex items-center gap-2 p-2.5 rounded-lg bg-emerald-900/20 border border-emerald-600/30">
            <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400 shrink-0" />
            <span className="text-[10px] font-semibold text-emerald-400 tracking-wide">Operator approved — pending dependency resolution</span>
          </div>
        )}
        {isDenied && (
          <div className="flex items-center gap-2 p-2.5 rounded-lg bg-red-900/20 border border-red-600/30">
            <XCircle className="h-3.5 w-3.5 text-red-400 shrink-0" />
            <span className="text-[10px] font-semibold text-red-400 tracking-wide">Operator denied — task will not execute</span>
          </div>
        )}

        {/* ── Approval binding indicator ── */}
        {(needsApproval || isApproved) && (
          <div className="pt-1 border-t border-white/5">
            <p className="text-[9px] font-mono text-zinc-600 mb-1.5 uppercase tracking-widest">Approval Binding</p>
            <div className="space-y-0.5 font-mono text-[9px] text-zinc-700">
              <div className="flex gap-2"><span className="text-zinc-600 w-20 shrink-0">session</span><span className="truncate text-zinc-500">{sessionId}</span></div>
              <div className="flex gap-2"><span className="text-zinc-600 w-20 shrink-0">task_id</span><span className="truncate text-zinc-500">{taskId}</span></div>
              {safetyMeta.safety_record_id && (
                <div className="flex gap-2"><span className="text-zinc-600 w-20 shrink-0">record_id</span><span className="truncate text-zinc-500">{safetyMeta.safety_record_id}</span></div>
              )}
            </div>
            <p className="mt-2 text-[9px] text-zinc-600 leading-relaxed">
              Approval binds to the current task version. Any modification to this task will invalidate this approval.
            </p>
          </div>
        )}
      </div>
    </motion.div>
  );
}
