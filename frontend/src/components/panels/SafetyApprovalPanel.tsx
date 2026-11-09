'use client';

import React, { useState } from 'react';
import { 
  Shield, 
  ShieldBan, 
  ShieldAlert, 
  ShieldCheck, 
  Lock, 
  CheckCircle2, 
  XCircle, 
  AlertTriangle, 
  Info, 
  X,
  KeyRound,
  FileCode
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { motion, AnimatePresence } from 'framer-motion';

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
  planHash?:       string | null;
  onApprove?:      (taskId: string, planHash?: string, notes?: string) => Promise<void> | void;
  onDeny?:         (taskId: string) => Promise<void> | void;
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
  planHash,
  onApprove,
  onDeny,
  isApproved = false,
  isDenied = false,
  className,
}: SafetyApprovalPanelProps) {
  const [isProcessing, setIsProcessing] = useState(false);
  const [isStepUpOpen, setIsStepUpOpen] = useState(false);
  const [confirmPhrase, setConfirmPhrase] = useState('');
  const [isAcknowledged, setIsAcknowledged] = useState(false);
  const [operatorNotes, setOperatorNotes] = useState('');

  const risk = RISK_CONFIG[safetyMeta.risk_level] ?? RISK_CONFIG.unknown;
  const decision = DECISION_CONFIG[safetyMeta.execution_decision] ?? DECISION_CONFIG.block;
  const RiskIcon = risk.Icon;

  const isBlocked     = safetyMeta.execution_decision === 'block' || safetyMeta.risk_level === 'critical';
  const needsApproval = safetyMeta.execution_decision === 'require_approval' && !isBlocked;
  const isHighRisk    = safetyMeta.risk_level === 'high';
  const confidencePct = Math.round((safetyMeta.semantic_confidence ?? 1) * 100);

  const isStepUpValid = confirmPhrase.trim().toUpperCase() === 'CONFIRM' && isAcknowledged;

  const handleApproveClick = async () => {
    if (isProcessing || isApproved || isDenied) return;

    if (isHighRisk) {
      // Step-up confirmation modal required for HIGH risk
      setIsStepUpOpen(true);
      return;
    }

    // Direct approval for medium/low
    setIsProcessing(true);
    try {
      await onApprove?.(taskId, planHash ?? undefined);
    } finally {
      setIsProcessing(false);
    }
  };

  const handleConfirmStepUp = async () => {
    if (!isStepUpValid || isProcessing) return;
    setIsProcessing(true);
    try {
      await onApprove?.(taskId, planHash ?? undefined, operatorNotes || undefined);
      setIsStepUpOpen(false);
      setConfirmPhrase('');
      setIsAcknowledged(false);
      setOperatorNotes('');
    } finally {
      setIsProcessing(false);
    }
  };

  const handleDeny = async () => {
    if (isProcessing || isApproved || isDenied) return;
    setIsProcessing(true);
    try {
      await onDeny?.(taskId);
    } finally {
      setIsProcessing(false);
    }
  };

  return (
    <>
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

          {/* ── BLOCK state: permanently locked indicator ── */}
          {isBlocked && (
            <motion.div
              initial={{ opacity: 0, scale: 0.95 }}
              animate={{ opacity: 1, scale: 1 }}
              className="flex items-start gap-3 p-3.5 rounded-lg bg-red-950/40 border border-red-500/40 shadow-sm"
            >
              <Lock className="h-4 w-4 text-red-400 mt-0.5 shrink-0" />
              <div>
                <p className="text-[11px] font-bold text-red-400 mb-0.5">Execution Permanently Blocked</p>
                <p className="text-[10px] text-zinc-400 leading-relaxed">
                  Operator approval cannot override this policy decision. Tasks classified as CRITICAL risk or hard policy violations are permanently blocked to safeguard infrastructure and private network resources.
                </p>
              </div>
            </motion.div>
          )}

          {/* ── REQUIRE_APPROVAL: Approve/Deny buttons ── */}
          {needsApproval && !isApproved && !isDenied && (
            <div className="flex gap-2 pt-1">
              <button
                onClick={handleApproveClick}
                disabled={isProcessing}
                className={cn(
                  'flex-1 flex items-center justify-center gap-2 px-3 py-2.5 rounded-lg text-[11px] font-bold uppercase tracking-wider transition-all',
                  isHighRisk
                    ? 'bg-amber-600/20 border border-amber-500/40 text-amber-400 hover:bg-amber-600/30 hover:border-amber-400/60'
                    : 'bg-emerald-600/20 border border-emerald-500/40 text-emerald-400 hover:bg-emerald-600/30 hover:border-emerald-400/60',
                  isProcessing ? 'opacity-50 cursor-not-allowed' : 'hover:scale-[1.02] active:scale-95'
                )}
              >
                {isHighRisk ? (
                  <>
                    <KeyRound className="h-3.5 w-3.5 text-amber-400" />
                    {isProcessing ? 'Verifying...' : 'Step-Up Authorize'}
                  </>
                ) : (
                  <>
                    <CheckCircle2 className="h-3.5 w-3.5" />
                    {isProcessing ? 'Processing...' : 'Approve Execution'}
                  </>
                )}
              </button>
              <button
                onClick={handleDeny}
                disabled={isProcessing}
                className={cn(
                  'flex-1 flex items-center justify-center gap-2 px-3 py-2.5 rounded-lg text-[11px] font-bold uppercase tracking-wider transition-all',
                  'bg-red-600/20 border border-red-500/40 text-red-400',
                  isProcessing ? 'opacity-50 cursor-not-allowed' : 'hover:bg-red-600/30 hover:border-red-400/60 hover:scale-[1.02] active:scale-95'
                )}
              >
                <XCircle className="h-3.5 w-3.5" />
                {isProcessing ? 'Processing...' : 'Deny Execution'}
              </button>
            </div>
          )}

          {/* ── Approval resolved state ── */}
          {isApproved && (
            <div className="flex items-center gap-2 p-2.5 rounded-lg bg-emerald-900/20 border border-emerald-600/30">
              <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400 shrink-0" />
              <span className="text-[10px] font-semibold text-emerald-400 tracking-wide">Operator approved — cryptographically bound to plan hash</span>
            </div>
          )}
          {isDenied && (
            <div className="flex items-center gap-2 p-2.5 rounded-lg bg-red-900/20 border border-red-600/30">
              <XCircle className="h-3.5 w-3.5 text-red-400 shrink-0" />
              <span className="text-[10px] font-semibold text-red-400 tracking-wide">Operator denied — task permanently blocked</span>
            </div>
          )}

          {/* ── Approval binding indicator ── */}
          {(needsApproval || isApproved) && (
            <div className="pt-2 border-t border-white/5 space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-[9px] font-mono text-zinc-500 uppercase tracking-widest flex items-center gap-1.5">
                  <FileCode className="h-3 w-3 text-cyan-400" /> Cryptographic Plan Binding
                </span>
                <span className="text-[9px] font-mono text-cyan-400/80 bg-cyan-950/40 px-1.5 py-0.5 rounded border border-cyan-800/40">
                  SHA-256
                </span>
              </div>
              <div className="space-y-1 font-mono text-[9px] text-zinc-400 bg-black/40 p-2 rounded-lg border border-white/5">
                <div className="flex gap-2">
                  <span className="text-zinc-500 w-16 shrink-0">Session:</span>
                  <span className="truncate text-zinc-300">{sessionId}</span>
                </div>
                <div className="flex gap-2">
                  <span className="text-zinc-500 w-16 shrink-0">Task ID:</span>
                  <span className="truncate text-zinc-300">{taskId}</span>
                </div>
                {planHash && (
                  <div className="flex gap-2">
                    <span className="text-zinc-500 w-16 shrink-0">Plan Hash:</span>
                    <span className="truncate text-cyan-300 font-bold" title={planHash}>
                      {planHash.substring(0, 16)}...{planHash.substring(planHash.length - 8)}
                    </span>
                  </div>
                )}
                {safetyMeta.safety_record_id && (
                  <div className="flex gap-2">
                    <span className="text-zinc-500 w-16 shrink-0">Record ID:</span>
                    <span className="truncate text-zinc-300">{safetyMeta.safety_record_id}</span>
                  </div>
                )}
              </div>
              <p className="text-[9px] text-zinc-500 leading-relaxed">
                Approval strictly binds to this exact DAG hash. Any modification to parameters, dependencies, or tasks automatically invalidates authorization.
              </p>
            </div>
          )}
        </div>
      </motion.div>

      {/* ── High-Risk Step-Up Confirmation Modal ── */}
      <AnimatePresence>
        {isStepUpOpen && (
          <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-md">
            <motion.div
              initial={{ opacity: 0, scale: 0.95, y: 10 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.95, y: 10 }}
              className="w-full max-w-lg bg-[#0e1117] border border-amber-500/40 rounded-2xl shadow-2xl overflow-hidden"
            >
              {/* Modal Header */}
              <div className="flex items-center justify-between px-5 py-4 border-b border-amber-500/20 bg-amber-500/5">
                <div className="flex items-center gap-2.5">
                  <AlertTriangle className="h-5 w-5 text-amber-400" />
                  <div>
                    <h3 className="text-sm font-bold text-amber-200">High-Risk Step-Up Authorization</h3>
                    <p className="text-[10px] text-amber-400/80">Deliberate operator confirmation required</p>
                  </div>
                </div>
                <button
                  onClick={() => setIsStepUpOpen(false)}
                  className="p-1 rounded-lg text-zinc-400 hover:text-white hover:bg-white/10 transition-colors"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>

              {/* Modal Body */}
              <div className="p-5 space-y-4 text-xs">
                {/* Warning Card */}
                <div className="p-3 rounded-xl bg-amber-950/20 border border-amber-500/30 text-amber-300 space-y-1">
                  <p className="font-semibold text-amber-200 flex items-center gap-1.5">
                    <ShieldAlert className="h-4 w-4 text-amber-400" /> Sensitive Operation Notice
                  </p>
                  <p className="text-[11px] text-zinc-300 leading-relaxed">
                    This step involves high-risk state-altering browser automation or external resource mutation. Confirming authorizes execution strictly for the current canonical plan hash.
                  </p>
                </div>

                {/* Details Breakdown */}
                <div className="bg-black/50 rounded-xl p-3 border border-white/5 space-y-2">
                  <div>
                    <span className="text-[10px] font-semibold text-zinc-500 uppercase tracking-widest">Task Name:</span>
                    <p className="text-zinc-200 font-medium">{taskName}</p>
                  </div>
                  <div>
                    <span className="text-[10px] font-semibold text-zinc-500 uppercase tracking-widest">Policy Explanation:</span>
                    <p className="text-zinc-400 text-[11px]">{safetyMeta.explanation || 'High risk action triggered.'}</p>
                  </div>
                  {planHash && (
                    <div>
                      <span className="text-[10px] font-semibold text-zinc-500 uppercase tracking-widest">Plan Hash Binding:</span>
                      <p className="font-mono text-[10px] text-cyan-300 break-all">{planHash}</p>
                    </div>
                  )}
                </div>

                {/* Acknowledgment Checkbox */}
                <label className="flex items-start gap-2.5 p-3 rounded-xl bg-white/[0.02] border border-white/10 cursor-pointer hover:bg-white/[0.04] transition-colors">
                  <input
                    type="checkbox"
                    checked={isAcknowledged}
                    onChange={(e) => setIsAcknowledged(e.target.checked)}
                    className="mt-0.5 h-4 w-4 rounded border-zinc-700 bg-zinc-900 text-amber-500 focus:ring-amber-500"
                  />
                  <span className="text-[11px] text-zinc-300 leading-relaxed select-none">
                    I acknowledge this action has been classified as HIGH RISK and confirm that the execution parameters and target resources have been verified.
                  </span>
                </label>

                {/* Confirmation Phrase Input */}
                <div className="space-y-1.5">
                  <label className="text-[11px] font-medium text-zinc-300">
                    Type <span className="font-mono font-bold text-amber-400">CONFIRM</span> to authorize execution:
                  </label>
                  <input
                    type="text"
                    value={confirmPhrase}
                    onChange={(e) => setConfirmPhrase(e.target.value)}
                    placeholder="CONFIRM"
                    className="w-full px-3 py-2 bg-black/60 border border-zinc-700 rounded-lg text-sm font-mono text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-amber-500"
                  />
                </div>

                {/* Operator Audit Notes */}
                <div className="space-y-1.5">
                  <label className="text-[11px] font-medium text-zinc-400">
                    Audit rationale / Operator notes (optional):
                  </label>
                  <input
                    type="text"
                    value={operatorNotes}
                    onChange={(e) => setOperatorNotes(e.target.value)}
                    placeholder="e.g. Authorized by release team for staging integration"
                    className="w-full px-3 py-2 bg-black/60 border border-zinc-700 rounded-lg text-xs text-zinc-200 placeholder:text-zinc-600 focus:outline-none focus:border-zinc-500"
                  />
                </div>
              </div>

              {/* Modal Footer */}
              <div className="flex items-center justify-end gap-2.5 px-5 py-4 border-t border-white/10 bg-black/30">
                <button
                  type="button"
                  onClick={() => setIsStepUpOpen(false)}
                  className="px-4 py-2 rounded-lg text-xs font-semibold text-zinc-400 hover:text-white hover:bg-white/10 transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  disabled={!isStepUpValid || isProcessing}
                  onClick={handleConfirmStepUp}
                  className={cn(
                    'flex items-center gap-2 px-4 py-2 rounded-lg text-xs font-bold uppercase tracking-wider transition-all',
                    'bg-amber-500 text-black shadow-lg shadow-amber-500/20',
                    (!isStepUpValid || isProcessing)
                      ? 'opacity-40 cursor-not-allowed'
                      : 'hover:bg-amber-400 hover:scale-[1.02] active:scale-95'
                  )}
                >
                  <KeyRound className="h-3.5 w-3.5" />
                  {isProcessing ? 'Authorizing...' : 'Authorize High-Risk Execution'}
                </button>
              </div>
            </motion.div>
          </div>
        )}
      </AnimatePresence>
    </>
  );
}
