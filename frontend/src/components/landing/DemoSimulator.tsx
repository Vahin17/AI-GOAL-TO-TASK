'use client';

import React, { useState, useRef, useEffect } from 'react';
import Link from 'next/link';
import { cn } from '@/lib/utils';
import { 
  Terminal, 
  ChevronRight, 
  Zap, 
  RotateCcw, 
  ExternalLink, 
  Cpu, 
  Check, 
  Loader2, 
  ShieldAlert 
} from 'lucide-react';

interface PipelineStage {
  id: number;
  title: string;
  tech: string;
  status: 'pending' | 'running' | 'approval_required' | 'completed';
}

const PRESET_GOALS = [
  {
    label: '📄 Research AI Agent Papers',
    goal: 'Research the latest papers on AI agents and summarize the important findings.',
  },
  {
    label: '💻 Build Task Management Web App',
    goal: 'Develop a task management web application with user authentication and database integration.',
  },
  {
    label: '📊 Analyze Sales Dataset & Insights',
    goal: 'Analyze a customer sales dataset and generate a report containing important business insights.',
  },
];

export default function DemoSimulator() {
  const [goal, setGoal] = useState<string>(
    'Research the latest papers on AI agents and summarize the important findings.'
  );
  const [isRunning, setIsRunning] = useState<boolean>(false);
  const [statusText, setStatusText] = useState<string>('Demonstration Engine Ready');
  const [elapsed, setElapsed] = useState<string>('0.00s');
  const [stepIndex, setStepIndex] = useState<number>(0);
  const [stages, setStages] = useState<PipelineStage[]>([]);
  const [targetGoalDisplay, setTargetGoalDisplay] = useState<string>('');

  const outputRef = useRef<HTMLDivElement | null>(null);
  const timerRef = useRef<NodeJS.Timeout | null>(null);
  const startTimeRef = useRef<number>(0);
  const abortRef = useRef<boolean>(false);

  useEffect(() => {
    if (outputRef.current) {
      outputRef.current.scrollTop = outputRef.current.scrollHeight;
    }
  }, [stepIndex, stages]);

  const handleSetGoal = (selectedGoal: string) => {
    setGoal(selectedGoal);
    const simulatorSection = document.getElementById('simulator');
    if (simulatorSection) {
      simulatorSection.scrollIntoView({ behavior: 'smooth' });
    }
  };

  const resetEngine = () => {
    abortRef.current = true;
    if (timerRef.current) clearInterval(timerRef.current);
    setIsRunning(false);
    setStatusText('Demonstration Engine Ready');
    setElapsed('0.00s');
    setStepIndex(0);
    setStages([]);
    setTargetGoalDisplay('');
  };

  const executeEngine = async () => {
    const trimmedGoal = goal.trim();
    if (!trimmedGoal || isRunning) return;

    abortRef.current = false;
    setIsRunning(true);
    setTargetGoalDisplay(trimmedGoal);
    setStatusText('Initializing Frontend Demo Session...');
    startTimeRef.current = performance.now();

    // Start timer ticker
    if (timerRef.current) clearInterval(timerRef.current);
    timerRef.current = setInterval(() => {
      const now = performance.now();
      setElapsed(((now - startTimeRef.current) / 1000).toFixed(2) + 's');
    }, 50);

    // Initial stages template
    const initialStages: PipelineStage[] = [
      {
        id: 1,
        title: 'Natural Language Goal Understanding',
        tech: 'Gemini / OpenAI LLM Decomposition',
        status: 'completed',
      },
      {
        id: 2,
        title: 'RAG Context & Semantic Memory Retrieval',
        tech: 'PostgreSQL + pgvector cosine similarity',
        status: 'completed',
      },
      {
        id: 3,
        title: 'DAG Validation & Topological Sorting',
        tech: "Kahn's algorithm cycle check",
        status: 'completed',
      },
      {
        id: 4,
        title: 'Safety Risk Evaluation & Approval Gate',
        tech: 'Risk Level: MEDIUM (Approval Req.)',
        status: 'approval_required',
      },
      {
        id: 5,
        title: 'Asynchronous Execution Management',
        tech: 'FastAPI BackgroundTasks + WebSocket',
        status: 'pending',
      },
    ];

    // Step 1: Ingesting goal
    setStepIndex(1);
    await new Promise((r) => setTimeout(r, 600));
    if (abortRef.current) return;

    // Step 2: Querying pgvector
    setStepIndex(2);
    await new Promise((r) => setTimeout(r, 700));
    if (abortRef.current) return;

    // Step 3: Planning & Safety Evaluation (Approval required)
    setStatusText('Executing Planning & Safety Evaluation...');
    setStages([...initialStages]);
    setStepIndex(3);
    await new Promise((r) => setTimeout(r, 1000));
    if (abortRef.current) return;

    // Step 4: Approved & Running Stage 5
    const updatedStagesStep4 = initialStages.map((s) => {
      if (s.id === 4) return { ...s, status: 'completed' as const };
      if (s.id === 5) return { ...s, status: 'running' as const };
      return s;
    });
    setStages(updatedStagesStep4);
    await new Promise((r) => setTimeout(r, 900));
    if (abortRef.current) return;

    // Step 5: Completed Stage 5
    const updatedStagesStep5 = updatedStagesStep4.map((s) => {
      if (s.id === 5) return { ...s, status: 'completed' as const };
      return s;
    });
    setStages(updatedStagesStep5);

    if (timerRef.current) clearInterval(timerRef.current);
    const finalElapsed = ((performance.now() - startTimeRef.current) / 1000).toFixed(2);
    setElapsed(`${finalElapsed}s`);
    setStatusText('Frontend Demonstration Completed Successfully!');
    setIsRunning(false);
  };

  return (
    <section id="simulator" className="py-24 relative z-10">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="text-center space-y-4 mb-16">
          <div className="inline-flex items-center space-x-2 px-3.5 py-1.5 rounded-full ardo-panel border border-emerald-600/30 text-emerald-700 dark:text-emerald-400 text-xs font-bold uppercase tracking-wider">
            <Terminal className="w-3.5 h-3.5" />
            <span>Frontend Demonstration Mode</span>
          </div>
          <h2 className="text-3xl sm:text-5xl font-bold tracking-tight text-slate-900 dark:text-white">
            Experience Goal-to-Task Planning
          </h2>
          <p className="text-slate-700 dark:text-slate-300 max-w-2xl mx-auto text-lg leading-relaxed">
            Test the frontend demonstration simulation below, or click{' '}
            <strong className="text-emerald-700 dark:text-emerald-400 font-semibold">
              &quot;Launch ARDO Engine&quot;
            </strong>{' '}
            to open the main application.
          </p>
        </div>

        {/* Playground Card */}
        <div className="ardo-panel rounded-3xl p-6 sm:p-8 lg:p-10 shadow-2xl relative overflow-hidden">
          <div className="absolute -right-20 -bottom-20 w-96 h-96 bg-emerald-500/10 rounded-full blur-3xl pointer-events-none" />

          <div className="grid grid-cols-1 lg:grid-cols-12 gap-8 items-start">
            {/* Left Controls */}
            <div className="lg:col-span-6 min-w-0 space-y-6">
              <div>
                <label className="block text-sm font-semibold text-slate-800 dark:text-slate-200 mb-2">
                  Select Preset Goal or Enter Custom
                </label>
                <div className="grid grid-cols-1 gap-2.5 mb-4">
                  {PRESET_GOALS.map((preset, idx) => (
                    <button
                      key={idx}
                      type="button"
                      onClick={() => handleSetGoal(preset.goal)}
                      className="preset-btn text-left p-3 rounded-xl ardo-panel text-xs text-slate-800 dark:text-slate-200 font-semibold hover:text-emerald-700 dark:hover:text-emerald-400 hover:border-emerald-500/50 transition-all flex items-center justify-between group"
                    >
                      <span>{preset.label}</span>
                      <ChevronRight className="w-4 h-4 text-slate-500 dark:text-slate-400 group-hover:text-emerald-600 dark:group-hover:text-emerald-400 group-hover:translate-x-1 transition-all" />
                    </button>
                  ))}
                </div>
              </div>

              <div>
                <label
                  htmlFor="customGoalInput"
                  className="block text-sm font-semibold text-slate-800 dark:text-slate-200 mb-2"
                >
                  Custom Natural Language Goal
                </label>
                <textarea
                  id="customGoalInput"
                  rows={3}
                  value={goal}
                  onChange={(e) => setGoal(e.target.value)}
                  className="w-full rounded-xl bg-slate-950 text-slate-100 dark:bg-black/90 border border-emerald-500/30 p-4 text-sm code-font focus:outline-none focus:border-emerald-500 transition-colors placeholder:text-slate-500"
                  placeholder="Type any complex goal here..."
                />
              </div>

              <div className="flex flex-row items-center gap-2 sm:gap-2.5 pt-2 w-full min-w-0">
                <button
                  type="button"
                  onClick={executeEngine}
                  disabled={isRunning}
                  className={cn(
                    "flex-1 min-w-0 h-[56px] px-2.5 sm:px-4 rounded-xl",
                    "bg-gradient-to-r from-emerald-600 via-teal-600 to-emerald-600 hover:from-emerald-500 hover:via-teal-500 hover:to-emerald-500",
                    "text-white font-semibold text-xs sm:text-sm",
                    "flex items-center justify-center gap-1.5 sm:gap-2 text-center",
                    "shadow-lg shadow-emerald-600/20 transition-all duration-200",
                    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400 focus-visible:ring-offset-2",
                    "active:scale-[0.98]",
                    isRunning ? "opacity-60 cursor-not-allowed" : "hover:scale-[1.01]"
                  )}
                >
                  <Zap className="w-4 h-4 sm:w-5 sm:h-5 shrink-0 text-emerald-200" />
                  <span className="whitespace-nowrap">
                    {isRunning ? 'Simulating...' : (
                      <>
                        <span className="hidden sm:inline">Simulate Demo Pipeline</span>
                        <span className="sm:hidden">Simulate</span>
                      </>
                    )}
                  </span>
                </button>

                <Link
                  href="/app"
                  className={cn(
                    "flex-1 min-w-0 h-[56px] px-2.5 sm:px-4 rounded-xl",
                    "bg-teal-700 hover:bg-teal-600 text-white font-semibold text-xs sm:text-sm",
                    "flex items-center justify-center gap-1.5 sm:gap-2 text-center",
                    "shadow-lg shadow-teal-700/20 transition-all duration-200",
                    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-400 focus-visible:ring-offset-2",
                    "active:scale-[0.98] hover:scale-[1.01]"
                  )}
                >
                  <ExternalLink className="w-4 h-4 sm:w-5 sm:h-5 shrink-0 text-teal-200" />
                  <span className="whitespace-nowrap">
                    <span className="hidden sm:inline">Launch ARDO Engine</span>
                    <span className="sm:hidden">Launch</span>
                  </span>
                </Link>

                <button
                  type="button"
                  onClick={resetEngine}
                  className={cn(
                    "w-[56px] h-[56px] shrink-0 rounded-xl",
                    "ardo-panel text-slate-700 dark:text-slate-300 hover:text-slate-950 dark:hover:text-white hover:border-emerald-500/50",
                    "flex items-center justify-center transition-all duration-200",
                    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400 focus-visible:ring-offset-2",
                    "active:scale-[0.96]"
                  )}
                  title="Reset Simulation"
                  aria-label="Reset Simulation"
                >
                  <RotateCcw className="w-5 h-5 shrink-0" />
                </button>
              </div>
            </div>

            {/* Right Execution Log */}
            <div className="lg:col-span-6 min-w-0 space-y-4">
              <div className="flex items-center justify-between">
                <div className="flex items-center space-x-2">
                  <div
                    className={`w-2.5 h-2.5 rounded-full ${
                      isRunning ? 'bg-emerald-400 animate-ping' : 'bg-emerald-500'
                    }`}
                  />
                  <span id="statusText" className="text-sm font-semibold text-slate-900 dark:text-white">
                    {statusText}
                  </span>
                </div>
                <span className="text-xs text-slate-600 dark:text-slate-400 code-font font-semibold">{elapsed} elapsed</span>
              </div>

              {/* Output Container */}
              <div
                id="simOutput"
                ref={outputRef}
                className="rounded-2xl bg-slate-900 dark:bg-black/90 border border-emerald-500/20 p-6 min-h-[380px] max-h-[460px] overflow-y-auto space-y-4"
              >
                {stepIndex === 0 && (
                  <div className="text-center py-20 text-slate-400 space-y-3">
                    <Cpu className="w-12 h-12 mx-auto opacity-30 animate-pulse text-emerald-400" />
                    <p className="text-sm">
                      Click{' '}
                      <strong className="text-emerald-400">
                        &quot;Simulate Demo Pipeline&quot;
                      </strong>{' '}
                      to execute frontend workflow demonstration.
                    </p>
                  </div>
                )}

                {stepIndex > 0 && (
                  <div className="space-y-3 text-xs code-font">
                    <div className="p-3 rounded-xl bg-emerald-500/10 border border-emerald-500/20 text-emerald-300">
                      <strong>Target Objective:</strong> &quot;{targetGoalDisplay}&quot;
                    </div>

                    <div className="flex items-center space-x-2 text-emerald-400">
                      <Loader2 className="w-4 h-4 animate-spin shrink-0" />
                      <span>
                        [Demo Simulation] Ingesting goal objective &amp; evaluating local vector cache...
                      </span>
                    </div>

                    {stepIndex >= 2 && (
                      <div className="flex items-center space-x-2 text-teal-400">
                        <Loader2 className="w-4 h-4 animate-spin shrink-0" />
                        <span>
                          [RAG Service] Querying pgvector similarity index with 1500-token context budget...
                        </span>
                      </div>
                    )}

                    {stepIndex >= 3 && stages.length > 0 && (
                      <div className="space-y-2.5 pt-2">
                        <div className="text-[10px] uppercase tracking-wider text-slate-400 font-semibold">
                          Active ARDO Demonstration Pipeline
                        </div>

                        {stages.map((s) => {
                          let badgeClass = 'bg-slate-800 text-slate-400';
                          let badgeText = 'Pending';
                          let iconNode = (
                            <div className="w-6 h-6 rounded-lg bg-slate-800 text-slate-400 flex items-center justify-center text-xs font-bold shrink-0">
                              {s.id}
                            </div>
                          );

                          if (s.status === 'completed') {
                            badgeClass =
                              'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30';
                            badgeText = 'Completed';
                            iconNode = (
                              <div className="w-6 h-6 rounded-lg bg-emerald-500/20 text-emerald-400 flex items-center justify-center text-xs font-bold shrink-0">
                                <Check className="w-3.5 h-3.5" />
                              </div>
                            );
                          } else if (s.status === 'running') {
                            badgeClass =
                              'bg-teal-500/20 text-teal-300 border border-teal-500/30 animate-pulse';
                            badgeText = 'Running...';
                            iconNode = (
                              <div className="w-6 h-6 rounded-lg bg-teal-500/20 text-teal-400 flex items-center justify-center text-xs font-bold shrink-0">
                                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                              </div>
                            );
                          } else if (s.status === 'approval_required') {
                            badgeClass =
                              'bg-amber-500/20 text-amber-300 border border-amber-500/30';
                            badgeText = 'Approval Req.';
                            iconNode = (
                              <div className="w-6 h-6 rounded-lg bg-amber-500/20 text-amber-400 flex items-center justify-center text-xs font-bold shrink-0">
                                <ShieldAlert className="w-3.5 h-3.5" />
                              </div>
                            );
                          }

                          return (
                            <div
                              key={s.id}
                              className="p-3.5 rounded-xl bg-white/[0.03] border border-emerald-500/10 flex items-center justify-between transition-all"
                            >
                              <div className="flex items-center space-x-3">
                                {iconNode}
                                <div>
                                  <div className="text-sm font-medium text-white">{s.title}</div>
                                  <div className="text-xs text-slate-400 code-font">{s.tech}</div>
                                </div>
                              </div>
                              <span className={`px-2.5 py-1 rounded-md text-xs font-semibold ${badgeClass}`}>
                                {badgeText}
                              </span>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
