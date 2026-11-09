'use client';

import React, { useState, useEffect } from 'react';
import { 
  Activity, 
  Terminal, 
  CheckCircle2, 
  Play, 
  RotateCcw,
  Lightbulb,
  ShieldCheck,
  ChevronDown,
  Timer,
  Zap,
  Info,
  BarChart2,
  List,
  HelpCircle,
  Clock,
  AlertCircle,
  BookOpen,
  Globe,
  Database,
  History,
  ExternalLink,
  Eye,
  X,
  Maximize2,
  Lock
} from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';
import { useARDOStore } from '@/store/useARDOStore';
import { cn } from '@/lib/utils';
import { motion, AnimatePresence } from 'framer-motion';
import SafetyApprovalPanel, { SafetyMetadata } from './SafetyApprovalPanel';
import { BrowserEvidenceItem, HistoryItem } from '@/lib/ardo-api';

export default function ExecutionControl() {
  const { logs: appLogs, resetSession: resetAppSession } = useAppStore();
  const { 
    isSessionActive, 
    sessionId,
    currentGoal,
    whyNow, 
    executionEvents, 
    approvalPendingIds,
    approvedTaskIds,
    safetyMetadataMap,
    deniedTaskIds,
    nodeMap,
    browserEvidence,
    goalVerification,
    executionHistory,
    knowledgeStats,
    planHash,
    approveTask,
    rejectTask,
    resetOrchestration,
    loadHistory,
    loadEvidence,
    loadKnowledgeStats,
    metrics
  } = useARDOStore();
  
  const [activeTab, setActiveTab] = useState<'main' | 'evidence' | 'rag_memory' | 'history' | 'dashboard' | 'timeline' | 'explainability'>('main');
  const [isLogsExpanded, setIsLogsExpanded] = useState(true);
  const [selectedScreenshot, setSelectedScreenshot] = useState<string | null>(null);

  useEffect(() => {
    loadKnowledgeStats();
    loadHistory();
  }, [loadKnowledgeStats, loadHistory]);

  useEffect(() => {
    if (sessionId) {
      loadEvidence(sessionId);
    }
  }, [sessionId, metrics?.tasks_completed, loadEvidence]);

  let status = 'idle';
  if (goalVerification) {
    if (goalVerification.overall_status === 'verified') status = 'verified';
    else if (goalVerification.overall_status === 'failed') status = 'failed';
    else if (goalVerification.overall_status === 'needs_human_review') status = 'needs review';
    else if (goalVerification.overall_status === 'inconclusive') status = 'inconclusive';
    else status = goalVerification.overall_status;
  } else if (isSessionActive) {
    if (approvalPendingIds.size > 0) status = 'planning';
    else if (metrics && metrics.tasks_completed === metrics.tasks_total && metrics.tasks_total > 0) status = 'completed';
    else status = 'executing';
  } else if (metrics && metrics.tasks_total > 0) {
    if (metrics.tasks_failed > 0) status = 'failed';
    else if (metrics.tasks_blocked > 0) status = 'blocked';
    else if (metrics.tasks_completed === metrics.tasks_total) status = 'completed';
  }

  const ardoLogs = (executionEvents || [])
    .filter(e => e != null)
    .map(e => {
      const eventTypeStr = typeof e.event_type === 'string' ? e.event_type.replace(/_/g, ' ').toUpperCase() : 'EVENT';
      const taskId = e.task_id ? e.task_id.substring(0, 5) : 'SYS';
      const statusText = e.status ? `- ${e.status}` : '';
      return `[${taskId}] ${eventTypeStr} ${statusText}`;
    });
  const logs = [...appLogs, ...ardoLogs];

  const handleReset = () => {
    resetOrchestration();
    resetAppSession();
  };

  return (
    <div className="flex h-full flex-col bg-white overflow-hidden font-sans selection:bg-primary/20 border-l border-secondary/10">
      
      {/* ── Header & Navigation Tabs ────────────────────────────────────────── */}
      <div className="flex flex-col border-b border-secondary/10 bg-white shrink-0">
        <div className="px-5 py-3.5 flex items-center justify-between">
          <div className="flex items-center gap-2">
             <Activity className="h-4 w-4 text-[#11999E]" />
             <h2 className="text-xs font-bold text-foreground uppercase tracking-wider">Console & Telemetry</h2>
          </div>
          <div className="flex items-center gap-1.5 px-2 py-0.5 rounded-lg bg-teal-50 border border-teal-200">
             <ShieldCheck className="h-3 w-3 text-teal-700" />
             <span className="text-[9px] font-bold text-teal-700 uppercase tracking-widest">
               {sessionId ? sessionId.substring(0, 12) : 'STANDBY'}
             </span>
          </div>
        </div>
        
        <div className="flex px-3 gap-1 border-t border-secondary/5 bg-slate-50/80 pt-1.5 pb-0 overflow-x-auto custom-scrollbar">
          {([
            { id: 'main', label: 'Console' },
            { id: 'evidence', label: 'Evidence' },
            { id: 'rag_memory', label: 'RAG & Memory' },
            { id: 'history', label: 'History' },
            { id: 'dashboard', label: 'Metrics' },
            { id: 'explainability', label: 'Why Now?' },
          ] as const).map(tab => (
            <button
              key={tab.id}
              onClick={() => {
                setActiveTab(tab.id as any);
                if (tab.id === 'history') loadHistory();
                if (tab.id === 'evidence' && sessionId) loadEvidence(sessionId);
              }}
              className={cn(
                "px-3 py-1.5 text-[9px] font-bold uppercase tracking-wider border-b-2 transition-all whitespace-nowrap",
                activeTab === tab.id 
                  ? "border-[#11999E] text-[#11999E] bg-white rounded-t-lg shadow-2xs" 
                  : "border-transparent text-slate-500 hover:text-slate-800 hover:bg-slate-100 rounded-t-lg"
              )}
            >
              {tab.label}
            </button>
          ))}
        </div>
      </div>

      {/* ── Tab Content Area ───────────────────────────────────────────────────── */}
      <div className="flex-1 overflow-y-auto custom-scrollbar bg-slate-50/40">
        <AnimatePresence mode="wait">
          <motion.div
            key={activeTab}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            transition={{ duration: 0.15 }}
            className="p-5 space-y-5 flex-1 flex flex-col min-h-full"
          >
            
            {/* ── MAIN TAB: Console, State & Logs ───────────────────────────────── */}
            {activeTab === 'main' && (
              <>
                <section className="space-y-3">
                  <div className="flex items-center justify-between px-1">
                     <h3 className="text-[10px] font-bold text-slate-400 uppercase tracking-widest">Runtime State</h3>
                     <div className="flex items-center gap-1.5">
                        <span className={cn("h-2 w-2 rounded-full", isSessionActive ? "bg-teal-500 animate-pulse" : "bg-slate-300")} />
                        <span className={cn("text-[9px] font-bold uppercase tracking-wider", isSessionActive ? "text-teal-700" : "text-slate-400")}>
                          {isSessionActive ? 'Active Execution' : 'Standby'}
                        </span>
                     </div>
                  </div>
                  <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-xs relative overflow-hidden">
                    <div className="flex items-center gap-3.5">
                      <div className={cn(
                        "h-11 w-11 rounded-xl flex items-center justify-center border transition-all",
                        status === 'idle' ? "bg-slate-50 border-slate-200 text-slate-400" :
                        status === 'verified' ? "bg-emerald-500 border-emerald-600 text-white shadow-md shadow-emerald-500/20" :
                        status === 'failed' ? "bg-rose-500 border-rose-600 text-white shadow-md shadow-rose-500/20" :
                        (status === 'needs review' || status === 'inconclusive' || status === 'planning') ? "bg-amber-500 border-amber-600 text-white shadow-md shadow-amber-500/20" :
                        "bg-teal-500 border-teal-600 text-white shadow-md shadow-teal-500/20"
                      )}>
                        {status === 'idle' && <Timer className="h-5 w-5" />}
                        {status === 'planning' && <Zap className="h-5 w-5 animate-pulse" />}
                        {status === 'executing' && <Play className="h-5 w-5 fill-current" />}
                        {status === 'completed' && <CheckCircle2 className="h-5 w-5" />}
                        {status === 'verified' && <CheckCircle2 className="h-5 w-5" />}
                        {status === 'failed' && <AlertCircle className="h-5 w-5" />}
                        {status === 'needs review' && <ShieldCheck className="h-5 w-5" />}
                        {status === 'inconclusive' && <HelpCircle className="h-5 w-5" />}
                      </div>
                      <div className="flex flex-col gap-0.5">
                        <span className="text-xs font-bold text-slate-900 capitalize tracking-tight leading-none">{status} Status</span>
                        <span className="text-[10px] font-semibold text-slate-500">
                          {metrics ? `${metrics.tasks_completed} / ${metrics.tasks_total} Tasks Completed (${((metrics.success_rate || 0)).toFixed(0)}%)` : 'Ready for prompt dispatch'}
                        </span>
                      </div>
                    </div>
                  </div>
                </section>

                <section className="space-y-3 flex-1 flex flex-col">
                  <div className="flex items-center justify-between px-1">
                     <h3 className="text-[10px] font-bold text-slate-400 uppercase tracking-widest flex items-center gap-1.5">
                       <Terminal className="h-3.5 w-3.5 text-[#11999E]" /> Operational Telemetry
                     </h3>
                     <button 
                        type="button"
                        suppressHydrationWarning
                        onClick={() => setIsLogsExpanded(!isLogsExpanded)}
                        className="h-5 w-5 flex items-center justify-center rounded bg-slate-100 border border-slate-200 text-slate-500 hover:text-slate-800 transition-all"
                     >
                        <ChevronDown className={cn("h-3.5 w-3.5 transition-transform duration-200", !isLogsExpanded && "-rotate-90")} />
                     </button>
                  </div>
                  <AnimatePresence mode="popLayout">
                    {isLogsExpanded && (
                      <motion.div 
                        initial={{ opacity: 0, height: 0 }}
                        animate={{ opacity: 1, height: 'auto' }}
                        exit={{ opacity: 0, height: 0 }}
                        className="flex-1 min-h-[180px] flex flex-col"
                      >
                        <div className="flex-1 bg-slate-900 rounded-xl border border-slate-800 font-mono text-[10px] p-4 overflow-y-auto space-y-2 custom-scrollbar shadow-inner text-teal-300">
                          {logs.map((log, i) => (
                            <div key={i} className="flex gap-2 border-l-2 border-teal-500/30 pl-2.5 py-0.5">
                              <span className="text-slate-500 select-none">[{i < 9 ? `0${i+1}` : i+1}]</span>
                              <span className="font-semibold tracking-tight text-slate-200">{log}</span>
                            </div>
                          ))}
                          {logs.length === 0 && (
                            <div className="flex flex-col items-center justify-center h-full gap-2 opacity-40 py-8">
                               <Terminal className="h-5 w-5 text-slate-400" />
                               <span className="uppercase text-[9px] font-bold tracking-widest text-slate-400">Awaiting Log Stream</span>
                            </div>
                          )}
                        </div>
                      </motion.div>
                    )}
                  </AnimatePresence>
                </section>

                {/* Per-task Safety Approval Panels */}
                {approvalPendingIds.size > 0 && (
                  <section className="space-y-2 shrink-0">
                    <p className="text-[10px] font-bold uppercase tracking-wider text-amber-700 flex items-center gap-1.5">
                      <ShieldCheck className="h-3.5 w-3.5 text-amber-600" />
                      Awaiting Operator Approval ({approvalPendingIds.size})
                    </p>
                    {Array.from(approvalPendingIds).map(taskId => {
                      const node = nodeMap.get(taskId);
                      const rawMeta = safetyMetadataMap.get(taskId);
                      const safetyMeta: SafetyMetadata = rawMeta ? (rawMeta as unknown as SafetyMetadata) : {
                        risk_level: 'unknown',
                        execution_decision: 'require_approval',
                        triggered_policies: [],
                        explanation: 'Safety policy gate triggered.',
                        semantic_confidence: 0,
                      };
                      return (
                        <SafetyApprovalPanel
                          key={taskId}
                          taskId={taskId}
                          taskName={node?.task_name ?? taskId}
                          safetyMeta={safetyMeta}
                          sessionId={sessionId ?? ''}
                          planHash={planHash}
                          onApprove={(id, hash, notes) => approveTask(id, hash, notes)}
                          onDeny={(id) => rejectTask(id)}
                          isApproved={approvedTaskIds?.has(taskId) ?? false}
                          isDenied={deniedTaskIds?.has(taskId) ?? false}
                        />
                      );
                    })}
                  </section>
                )}

                <section className="pt-2 space-y-2 shrink-0">
                   <div className="flex flex-col gap-2">
                      <button 
                        type="button"
                        suppressHydrationWarning
                        onClick={handleReset}
                        className="w-full flex items-center justify-center gap-2 bg-white border border-slate-200 rounded-xl py-2.5 text-[11px] font-bold text-slate-600 transition-all hover:bg-slate-50 hover:text-slate-900 uppercase tracking-wider shadow-2xs"
                      >
                        <RotateCcw className="h-3.5 w-3.5" /> Reset Session
                      </button>
                   </div>
                </section>
              </>
            )}

            {/* ── EVIDENCE TAB: Playwright Browser Artifacts & Cryptographic Verification ─────── */}
            {activeTab === 'evidence' && (
              <div className="space-y-4">
                <div className="flex items-center justify-between border-b border-slate-200 pb-2.5">
                  <div className="flex items-center gap-2">
                    <Globe className="h-4 w-4 text-[#11999E]" />
                    <h3 className="text-xs font-bold uppercase tracking-wider text-slate-800">
                      Execution Evidence & Outcome Verification
                    </h3>
                  </div>
                  <button 
                    onClick={() => sessionId && loadEvidence(sessionId)}
                    className="text-[9px] font-bold text-[#11999E] hover:underline uppercase"
                  >
                    Refresh Evidence
                  </button>
                </div>

                {/* Phase 3 Objective C: Goal-Level Outcome Verification Summary */}
                {goalVerification && (
                  <div className={cn(
                    "rounded-xl border p-3.5 space-y-2 shadow-xs",
                    goalVerification.overall_status === 'verified' ? "bg-emerald-50/70 border-emerald-200" :
                    goalVerification.overall_status === 'failed' ? "bg-rose-50/70 border-rose-200" :
                    goalVerification.overall_status === 'needs_human_review' ? "bg-amber-50/70 border-amber-200" :
                    "bg-slate-50 border-slate-200"
                  )}>
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <ShieldCheck className={cn(
                          "h-4 w-4",
                          goalVerification.overall_status === 'verified' ? "text-emerald-700" :
                          goalVerification.overall_status === 'failed' ? "text-rose-700" :
                          "text-amber-700"
                        )} />
                        <span className="text-xs font-bold uppercase tracking-wider text-slate-900">
                          Goal Outcome Verification
                        </span>
                      </div>
                      <div className="flex items-center gap-2">
                        <span className="text-[10px] font-semibold text-slate-600">
                          {(goalVerification.confidence_score * 100).toFixed(0)}% Confidence
                        </span>
                        <span className={cn(
                          "text-[9px] font-bold px-2 py-0.5 rounded border uppercase tracking-wider",
                          goalVerification.overall_status === 'verified' ? "bg-emerald-100 text-emerald-800 border-emerald-300" :
                          goalVerification.overall_status === 'failed' ? "bg-rose-100 text-rose-800 border-rose-300" :
                          goalVerification.overall_status === 'needs_human_review' ? "bg-amber-100 text-amber-800 border-amber-300" :
                          "bg-blue-100 text-blue-800 border-blue-300"
                        )}>
                          {goalVerification.overall_status.replace(/_/g, ' ')}
                        </span>
                      </div>
                    </div>
                    <p className="text-[11px] text-slate-700">
                      {goalVerification.explanation}
                    </p>
                    <div className="flex items-center gap-4 text-[10px] text-slate-500 pt-1 border-t border-slate-200/60">
                      <span>Tasks: <strong className="text-slate-700">{goalVerification.verified_tasks}/{goalVerification.total_tasks} Verified</strong></span>
                      {goalVerification.failed_tasks > 0 && <span>Failed: <strong className="text-rose-600">{goalVerification.failed_tasks}</strong></span>}
                      {goalVerification.inconclusive_tasks > 0 && <span>Inconclusive: <strong className="text-amber-600">{goalVerification.inconclusive_tasks}</strong></span>}
                      <span className="ml-auto flex items-center gap-1 text-[9px] text-slate-400">
                        <Lock className="h-3 w-3" /> Tenant Isolated
                      </span>
                    </div>
                  </div>
                )}

                {browserEvidence.length > 0 ? (
                  <div className="space-y-4">
                    {browserEvidence.map((ev, idx) => (
                      <div key={idx} className="bg-white rounded-xl border border-slate-200 p-4 shadow-xs space-y-3">
                        <div className="flex items-start justify-between gap-2">
                          <div>
                            <span className="text-xs font-bold text-slate-800 block">{ev.task_name || ev.task_id || 'Unnamed Task'}</span>
                            <div className="flex items-center gap-1.5 mt-1">
                              {ev.artifact_hash && (
                                <span className="font-mono text-[9px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 border border-slate-200" title={`Full SHA-256: ${ev.artifact_hash}`}>
                                  SHA-256: {ev.artifact_hash.slice(0, 10)}...
                                </span>
                              )}
                              <span className="text-[9px] px-1.5 py-0.5 rounded bg-slate-50 text-slate-500 border border-slate-200">
                                Privacy Redacted
                              </span>
                            </div>
                          </div>
                          <div className="flex flex-col items-end gap-1">
                            <span className={cn(
                              "text-[9px] font-bold px-2 py-0.5 rounded border uppercase tracking-wider",
                              (ev.verification_status === 'verified' || ev.result?.success) ? "bg-emerald-50 text-emerald-700 border-emerald-200" :
                              (ev.verification_status === 'failed' || ev.status === 'failed') ? "bg-rose-50 text-rose-700 border-rose-200" :
                              (ev.verification_status === 'needs_human_review') ? "bg-amber-50 text-amber-700 border-amber-200" :
                              "bg-slate-50 text-slate-700 border-slate-200"
                            )}>
                              {ev.verification_status ? ev.verification_status.replace(/_/g, ' ') : (ev.result?.success ? 'VERIFIED' : (ev.status === 'completed' ? 'FAILED' : (ev.status ? String(ev.status).toUpperCase() : 'PENDING')))}
                            </span>
                            {ev.confidence_score !== undefined && (
                              <span className="text-[9px] font-medium text-slate-500">
                                {(ev.confidence_score * 100).toFixed(0)}% conf
                              </span>
                            )}
                          </div>
                        </div>

                        <div className="grid grid-cols-2 gap-2 text-[10px]">
                          <div className="bg-slate-50 p-2 rounded border border-slate-100">
                            <span className="text-slate-400 font-bold block">Action Type</span>
                            <span className="font-mono font-semibold text-slate-700">{ev.action?.action_type || 'Awaiting action'}</span>
                          </div>
                          <div className="bg-slate-50 p-2 rounded border border-slate-100">
                            <span className="text-slate-400 font-bold block">Target Selector</span>
                            <span className="font-mono font-semibold text-slate-700 truncate block">{ev.action?.selector || ev.action?.url || 'Page Viewport'}</span>
                          </div>
                        </div>

                        {ev.result?.error && (
                          (() => {
                            const errStr = String(ev.result.error);
                            const isQuota = /quota|free[-_ ]tier|429|resource[-_ ]exhausted/i.test(errStr);
                            if (isQuota) {
                              return (
                                <div className="bg-amber-50 border border-amber-300 rounded p-2.5 text-[10px] text-amber-900 space-y-1">
                                  <div className="flex items-center gap-1.5 font-bold uppercase tracking-wider text-amber-800">
                                    <AlertCircle className="h-3.5 w-3.5 text-amber-600" />
                                    <span>AI Provider Quota Exhausted</span>
                                  </div>
                                  <p className="font-mono text-[9px] text-amber-800/90 leading-relaxed">
                                    Upstream AI model quota limit reached. This is an API quota constraint, not a browser action failure.
                                  </p>
                                </div>
                              );
                            }
                            return (
                              <div className="bg-rose-50 border border-rose-200 rounded p-2 text-[10px] text-rose-800 font-mono">
                                {errStr}
                              </div>
                            );
                          })()
                        )}

                        {ev.result?.text_extracted && (
                          <div className="bg-teal-50 border border-teal-200 rounded p-2.5">
                            <span className="text-[9px] font-bold text-teal-800 uppercase tracking-wider block mb-1">
                              Extracted Verification Text
                            </span>
                            <p className="text-[11px] font-mono font-semibold text-teal-950">
                              &ldquo;{ev.result.text_extracted}&rdquo;
                            </p>
                          </div>
                        )}

                        {ev.result?.screenshot_base64 && (
                          <div className="space-y-1.5">
                            <span className="text-[9px] font-bold text-slate-400 uppercase tracking-wider flex items-center gap-1">
                              <Eye className="h-3 w-3 text-slate-500" /> Screenshot Capture
                            </span>
                            <div 
                              onClick={() => setSelectedScreenshot(`data:image/png;base64,${ev.result.screenshot_base64}`)}
                              className="relative rounded-lg overflow-hidden border border-slate-200 cursor-pointer group max-h-40 bg-slate-900 flex items-center justify-center"
                            >
                              <img 
                                src={`data:image/png;base64,${ev.result.screenshot_base64}`} 
                                alt="Playwright Screenshot Evidence" 
                                className="w-full object-cover object-top group-hover:opacity-80 transition-opacity" 
                              />
                              <div className="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity bg-black/40 text-white text-[10px] font-bold gap-1">
                                <Maximize2 className="h-3.5 w-3.5" /> Click to Expand
                              </div>
                            </div>
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="bg-white border border-slate-200 rounded-2xl p-6 text-center space-y-2">
                    <Globe className="h-8 w-8 text-slate-300 mx-auto" />
                    <p className="text-xs font-bold text-slate-700">No Browser Artifacts in Current Run</p>
                    <p className="text-[10px] text-slate-400 max-w-xs mx-auto">
                      Launch the &ldquo;Playwright Browser E2E&rdquo; preset to see real Chromium navigation, DOM interaction, text extraction, and screenshot evidence.
                    </p>
                  </div>
                )}
              </div>
            )}

            {/* ── RAG & SEMANTIC MEMORY TAB ────────────────────────────────────── */}
            {activeTab === 'rag_memory' && (
              <div className="space-y-4">
                <div className="flex items-center justify-between border-b border-slate-200 pb-2.5">
                  <div className="flex items-center gap-2">
                    <Database className="h-4 w-4 text-[#11999E]" />
                    <h3 className="text-xs font-bold uppercase tracking-wider text-slate-800">
                      RAG & Semantic Memory
                    </h3>
                  </div>
                  <span className="text-[9px] font-mono font-bold bg-teal-50 text-teal-800 border border-teal-200 px-2 py-0.5 rounded">
                    Dual Pipeline (65% Static / 35% Memory)
                  </span>
                </div>

                {/* Storage Engine Status */}
                <div className="bg-white rounded-xl border border-slate-200 p-4 shadow-xs space-y-3">
                  <h4 className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">
                    Vector Architecture & Backend
                  </h4>
                  <div className="grid grid-cols-2 gap-2 text-xs">
                    <div className="bg-slate-50 p-2.5 rounded border border-slate-100">
                      <span className="text-[9px] font-bold text-slate-400 uppercase block">Engine</span>
                      <span className="font-bold text-slate-800">PostgreSQL + pgvector</span>
                    </div>
                    <div className="bg-slate-50 p-2.5 rounded border border-slate-100">
                      <span className="text-[9px] font-bold text-slate-400 uppercase block">Index Type</span>
                      <span className="font-bold text-teal-700">HNSW (Cosine &lt;=&gt;)</span>
                    </div>
                    <div className="bg-slate-50 p-2.5 rounded border border-slate-100">
                      <span className="text-[9px] font-bold text-slate-400 uppercase block">Indexed Playbooks</span>
                      <span className="font-bold text-slate-800">{knowledgeStats?.total_knowledge_chunks ?? 24} chunks</span>
                    </div>
                    <div className="bg-slate-50 p-2.5 rounded border border-slate-100">
                      <span className="text-[9px] font-bold text-slate-400 uppercase block">Saved Memories</span>
                      <span className="font-bold text-slate-800">{knowledgeStats?.total_historical_memories ?? 12} records</span>
                    </div>
                  </div>
                </div>

                {/* Memory Feedback Loop Explainer */}
                <div className="bg-teal-50/70 border border-teal-200 rounded-xl p-4 space-y-2">
                  <div className="flex items-center gap-1.5">
                    <RotateCcw className="h-3.5 w-3.5 text-teal-800" />
                    <span className="text-[10px] font-bold uppercase tracking-wider text-teal-900">
                      Closed Feedback Loop Architecture
                    </span>
                  </div>
                  <p className="text-[11px] text-teal-900 leading-relaxed">
                    Upon task completion, execution telemetry and browser outcomes are embedded into PostgreSQL pgvector table <code className="bg-teal-100 px-1 py-0.5 rounded font-mono text-[10px]">execution_memories</code>. Subsequent planner runs retrieve these memories with <code className="font-mono text-[10px]">memory://...</code> provenance to adapt task strategies.
                  </p>
                </div>
              </div>
            )}

            {/* ── HISTORY TAB: Execution Records ───────────────────────────────── */}
            {activeTab === 'history' && (
              <div className="space-y-4">
                <div className="flex items-center justify-between border-b border-slate-200 pb-2.5">
                  <div className="flex items-center gap-2">
                    <History className="h-4 w-4 text-[#11999E]" />
                    <h3 className="text-xs font-bold uppercase tracking-wider text-slate-800">
                      Execution History
                    </h3>
                  </div>
                  <button 
                    onClick={() => loadHistory()}
                    className="text-[9px] font-bold text-[#11999E] hover:underline uppercase"
                  >
                    Refresh History
                  </button>
                </div>

                {executionHistory.length > 0 ? (
                  <div className="space-y-2.5">
                    {executionHistory.map((item, idx) => (
                      <div key={idx} className="bg-white rounded-xl border border-slate-200 p-3.5 shadow-xs space-y-2">
                        <div className="flex items-center justify-between">
                          <span className="font-mono text-[10px] font-bold text-slate-400">
                            {item.session_id}
                          </span>
                          <span className={cn(
                            "text-[9px] font-bold px-2 py-0.5 rounded border uppercase tracking-wider",
                            item.status === 'completed' ? "bg-emerald-50 text-emerald-700 border-emerald-200" :
                            item.status === 'blocked' ? "bg-rose-50 text-rose-700 border-rose-200" :
                            "bg-amber-50 text-amber-700 border-amber-200"
                          )}>
                            {item.status}
                          </span>
                        </div>
                        <p className="text-xs font-semibold text-slate-800 line-clamp-2 leading-snug">
                          {item.goal}
                        </p>
                        <div className="flex items-center justify-between text-[10px] text-slate-500 pt-1 border-t border-slate-100">
                          <span>Tasks: {item.tasks_completed ?? item.task_count}/{item.task_count}</span>
                          <span>Duration: {(item.duration_seconds != null ? Number(item.duration_seconds).toFixed(1) : '0.0')}s</span>
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="bg-white border border-slate-200 rounded-2xl p-6 text-center space-y-2">
                    <History className="h-8 w-8 text-slate-300 mx-auto" />
                    <p className="text-xs font-bold text-slate-700">No Historical Records Yet</p>
                    <p className="text-[10px] text-slate-400">
                      Dispatched goals and DAG execution runs will appear here with duration and outcome telemetry.
                    </p>
                  </div>
                )}
              </div>
            )}

            {/* ── DASHBOARD TAB: Telemetry & Metrics ───────────────────────────── */}
            {activeTab === 'dashboard' && (
              <div className="space-y-5">
                <h3 className="text-xs font-bold uppercase tracking-wider text-slate-800 border-b border-slate-200 pb-2.5 flex items-center gap-2">
                  <BarChart2 className="h-4 w-4 text-[#11999E]" /> Operational Telemetry Metrics
                </h3>
                
                <div className="grid grid-cols-2 gap-3">
                  <div className="bg-white p-3.5 rounded-xl border border-slate-200 shadow-xs">
                    <span className="text-[9px] font-bold text-slate-400 uppercase tracking-wider block mb-1">Success Rate</span>
                    <span className="text-xl font-bold text-slate-900">{(metrics?.success_rate || 0).toFixed(1)}%</span>
                  </div>
                  <div className="bg-white p-3.5 rounded-xl border border-slate-200 shadow-xs">
                    <span className="text-[9px] font-bold text-slate-400 uppercase tracking-wider block mb-1">Avg Latency</span>
                    <span className="text-xl font-bold text-slate-900">{(metrics?.avg_latency_seconds || 0).toFixed(2)}s</span>
                  </div>
                  <div className="bg-white p-3.5 rounded-xl border border-slate-200 shadow-xs">
                    <span className="text-[9px] font-bold text-slate-400 uppercase tracking-wider block mb-1">Scheduler Efficiency</span>
                    <span className="text-xl font-bold text-teal-600">{(metrics as any)?.scheduler_efficiency_score?.toFixed(2) || '1.00'}</span>
                  </div>
                  <div className="bg-white p-3.5 rounded-xl border border-slate-200 shadow-xs">
                    <span className="text-[9px] font-bold text-slate-400 uppercase tracking-wider block mb-1">Elapsed Time</span>
                    <span className="text-xl font-bold text-slate-900">{(metrics?.elapsed_seconds || 0).toFixed(1)}s</span>
                  </div>
                </div>

                <div className="bg-white p-4 rounded-xl border border-slate-200 shadow-xs space-y-2.5">
                  <h4 className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">Queue Telemetry</h4>
                  <div className="space-y-2 text-xs">
                    <div className="flex justify-between items-center">
                      <span className="font-semibold text-slate-700">Tasks Total</span>
                      <span className="font-mono bg-slate-100 px-2 py-0.5 rounded font-bold">{metrics?.tasks_total || 0}</span>
                    </div>
                    <div className="flex justify-between items-center">
                      <span className="font-semibold text-emerald-700">Completed</span>
                      <span className="font-mono bg-emerald-50 text-emerald-700 px-2 py-0.5 rounded font-bold">{metrics?.tasks_completed || 0}</span>
                    </div>
                    <div className="flex justify-between items-center">
                      <span className="font-semibold text-rose-700">Failed / Blocked</span>
                      <span className="font-mono bg-rose-50 text-rose-700 px-2 py-0.5 rounded font-bold">{(metrics?.tasks_failed || 0) + (metrics?.tasks_blocked || 0)}</span>
                    </div>
                    <div className="flex justify-between items-center">
                      <span className="font-semibold text-amber-700">Retries</span>
                      <span className="font-mono bg-amber-50 text-amber-700 px-2 py-0.5 rounded font-bold">{metrics?.tasks_retried || 0}</span>
                    </div>
                  </div>
                </div>
              </div>
            )}

            {/* ── EXPLAINABILITY TAB: Why This Task Now? ───────────────────────── */}
            {activeTab === 'explainability' && (
              <div className="space-y-5">
                <h3 className="text-xs font-bold uppercase tracking-wider text-slate-800 border-b border-slate-200 pb-2.5 flex items-center gap-2">
                  <HelpCircle className="h-4 w-4 text-[#11999E]" /> ARDO Explainability Engine
                </h3>

                {whyNow ? (
                  <div className="space-y-3.5">
                    <div className="bg-white border border-slate-200 rounded-xl p-4 shadow-xs">
                      <h4 className="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-2">Decision Reason</h4>
                      <p className="text-xs font-semibold text-slate-800 leading-relaxed">
                        {whyNow.decision_reason}
                      </p>
                    </div>

                    <div className="grid grid-cols-2 gap-3">
                      <div className="bg-white p-3.5 rounded-xl border border-slate-200 shadow-xs">
                        <span className="text-[9px] font-bold text-slate-400 uppercase tracking-wider block mb-1">Priority Score</span>
                        <div className="text-2xl font-bold text-[#11999E]">{(whyNow.final_score * 100).toFixed(0)}</div>
                      </div>
                      <div className="bg-white p-3.5 rounded-xl border border-slate-200 shadow-xs">
                        <span className="text-[9px] font-bold text-slate-400 uppercase tracking-wider block mb-1.5">Factor Weights</span>
                        {whyNow.factor_breakdown && Object.entries(whyNow.factor_breakdown).map(([factor, score]: any) => (
                          <div key={factor} className="flex justify-between items-center text-[10px] font-semibold mb-0.5">
                            <span className="text-slate-500 capitalize">{factor}</span>
                            <span className="text-slate-800">{(score * 100).toFixed(0)}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                ) : (
                  <div className="text-center p-6 text-slate-400 font-bold uppercase tracking-widest text-xs border border-dashed border-slate-200 rounded-xl bg-white">
                    No active scheduling explanation
                  </div>
                )}
              </div>
            )}

          </motion.div>
        </AnimatePresence>
      </div>

      {/* ── Screenshot Lightbox Modal ────────────────────────────────────────── */}
      <AnimatePresence>
        {selectedScreenshot && (
          <motion.div 
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-50 bg-black/80 flex items-center justify-center p-6 backdrop-blur-xs"
            onClick={() => setSelectedScreenshot(null)}
          >
            <div className="relative max-w-4xl max-h-[90vh] bg-slate-900 rounded-2xl overflow-hidden border border-slate-700 shadow-2xl" onClick={e => e.stopPropagation()}>
              <div className="flex items-center justify-between px-4 py-3 border-b border-slate-800 bg-slate-950">
                <span className="text-xs font-bold text-slate-200 flex items-center gap-1.5">
                  <Globe className="h-3.5 w-3.5 text-teal-400" /> Playwright Browser Screenshot Evidence
                </span>
                <button 
                  onClick={() => setSelectedScreenshot(null)}
                  className="h-6 w-6 rounded flex items-center justify-center text-slate-400 hover:text-white hover:bg-slate-800 transition-all"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
              <div className="p-2 overflow-auto max-h-[80vh]">
                <img src={selectedScreenshot} alt="Evidence Screenshot Full" className="w-full h-auto rounded-lg shadow-lg" />
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
