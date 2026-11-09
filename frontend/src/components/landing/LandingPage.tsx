'use client';

import React, { useState, useEffect } from 'react';
import Link from 'next/link';
import { 
  Cpu, 
  Sparkles, 
  Sun, 
  Moon, 
  ExternalLink, 
  ArrowRight, 
  PlayCircle, 
  Layers, 
  ArrowDown, 
  Database, 
  Server, 
  Code, 
  Brain, 
  Search, 
  ShieldCheck, 
  BookOpen, 
  GitMerge, 
  CheckCircle, 
  ShieldAlert, 
  Activity, 
  Globe, 
  Repeat, 
  Check,
  Zap 
} from 'lucide-react';

const GithubIcon = ({ className = 'w-4 h-4' }: { className?: string }) => (
  <svg className={className} fill="currentColor" viewBox="0 0 24 24" aria-hidden="true">
    <path
      fillRule="evenodd"
      d="M12 2C6.477 2 2 6.484 2 12.017c0 4.425 2.865 8.18 6.839 9.504.5.092.682-.217.682-.483 0-.237-.008-.868-.013-1.703-2.782.605-3.369-1.343-3.369-1.343-.454-1.158-1.11-1.466-1.11-1.466-.908-.62.069-.608.069-.608 1.003.07 1.53 1.032 1.53 1.032.892 1.53 2.341 1.088 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.113-4.555-4.951 0-1.093.39-1.988 1.029-2.688-.103-.253-.446-1.272.098-2.65 0 0 .84-.27 2.75 1.026A9.564 9.564 0 0112 6.844c.85.004 1.705.115 2.504.337 1.909-1.296 2.747-1.027 2.747-1.027.546 1.379.202 2.398.1 2.651.64.7 1.028 1.595 1.028 2.688 0 3.848-2.339 4.695-4.566 4.943.359.309.678.92.678 1.855 0 1.338-.012 2.419-.012 2.747 0 .268.18.58.688.482A10.019 10.019 0 0022 12.017C22 6.484 17.522 2 12 2z"
      clipRule="evenodd"
    />
  </svg>
);

import BackgroundCanvas from './BackgroundCanvas';
import DemoSimulator from './DemoSimulator';
import { useAuthStore } from '@/store/useAuthStore';
import './landing.css';

export default function LandingPage() {
  const [isDark, setIsDark] = useState<boolean>(false);
  const [mounted, setMounted] = useState<boolean>(false);
  const { user, isAuthenticated, logout, checkAuth } = useAuthStore();

  useEffect(() => {
    checkAuth();
  }, [checkAuth]);

  useEffect(() => {
    setMounted(true);
    const savedTheme = localStorage.getItem('ardo_theme') || 'light';
    if (savedTheme === 'dark') {
      setIsDark(true);
      document.documentElement.classList.add('dark');
      document.documentElement.classList.remove('light');
    } else {
      setIsDark(false);
      document.documentElement.classList.remove('dark');
      document.documentElement.classList.add('light');
    }
  }, []);

  const toggleTheme = () => {
    const nextDark = !isDark;
    setIsDark(nextDark);
    if (nextDark) {
      document.documentElement.classList.add('dark');
      document.documentElement.classList.remove('light');
      localStorage.setItem('ardo_theme', 'dark');
    } else {
      document.documentElement.classList.remove('dark');
      document.documentElement.classList.add('light');
      localStorage.setItem('ardo_theme', 'light');
    }
  };

  return (
    <div className={`landing-root ${isDark ? 'dark' : ''} relative selection:bg-emerald-500 selection:text-white`}>
      {/* Background Particle Animation */}
      <BackgroundCanvas />

      {/* Navigation Bar */}
      <header className="fixed top-0 left-0 right-0 z-50 ardo-panel border-b border-emerald-500/10 transition-all duration-300">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-20 flex items-center justify-between">
          <div 
            className="flex items-center space-x-3 cursor-pointer"
            onClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}
          >
            <div className="w-10 h-10 rounded-xl bg-gradient-to-tr from-emerald-600 via-teal-600 to-cyan-600 flex items-center justify-center shadow-lg shadow-emerald-500/20">
              <Cpu className="w-6 h-6 text-white animate-spin" style={{ animationDuration: '12s' }} />
            </div>
            <div>
              <span className="text-xl font-bold tracking-tight text-slate-900 dark:text-white">ARDO</span>
              <span className="text-xs block text-emerald-700 dark:text-emerald-400 font-semibold code-font">
                AI Goal-to-Task Planning Engine
              </span>
            </div>
          </div>

          <nav className="hidden md:flex items-center space-x-8 text-sm font-semibold text-slate-700 dark:text-slate-300">
            <a href="#simulator" className="hover:text-emerald-700 dark:hover:text-emerald-400 transition-colors">
              Live Simulator
            </a>
            <a href="#architecture" className="hover:text-emerald-700 dark:hover:text-emerald-400 transition-colors">
              Architecture
            </a>
            <a href="#techstack" className="hover:text-emerald-700 dark:hover:text-emerald-400 transition-colors">
              Tech Stack
            </a>
            <a href="#features" className="hover:text-emerald-700 dark:hover:text-emerald-400 transition-colors">
              Features
            </a>
          </nav>

          <div className="flex items-center space-x-3">
            {/* Theme Toggle */}
            <button
              type="button"
              onClick={toggleTheme}
              className="p-2.5 rounded-xl ardo-panel hover:border-emerald-500 text-slate-800 dark:text-slate-200 transition-all"
              title="Toggle Light/Dark Theme"
            >
              {mounted && isDark ? (
                <Sun className="w-4 h-4 text-amber-400" />
              ) : (
                <Moon className="w-4 h-4 text-slate-700" />
              )}
            </button>

            {/* Authentication / Launch CTA */}
            {mounted && isAuthenticated ? (
              <div className="flex items-center space-x-2">
                <Link
                  href="/app"
                  className="inline-flex items-center space-x-2 px-3.5 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-white font-medium text-sm transition-all shadow-lg shadow-emerald-600/25 hover:scale-105"
                >
                  <ExternalLink className="w-4 h-4" />
                  <span>Launch Engine</span>
                </Link>
                <button
                  type="button"
                  onClick={() => logout()}
                  className="hidden sm:inline-flex items-center px-3 py-2 rounded-xl ardo-panel font-semibold text-slate-800 dark:text-slate-200 text-xs hover:text-rose-500 transition-colors cursor-pointer"
                >
                  Log Out
                </button>
              </div>
            ) : (
              <div className="flex items-center space-x-2">
                <Link
                  href="/login"
                  className="px-3 py-2 rounded-xl ardo-panel font-semibold text-slate-800 dark:text-slate-200 text-xs sm:text-sm transition-all hover:text-emerald-700 dark:hover:text-emerald-400"
                >
                  Log In
                </Link>
                <Link
                  href="/signup"
                  className="inline-flex items-center space-x-1.5 px-3 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-white font-medium text-xs sm:text-sm transition-all shadow-lg shadow-emerald-600/25 hover:scale-105"
                >
                  <span>Sign Up</span>
                </Link>
              </div>
            )}

            <a
              href="https://github.com"
              target="_blank"
              rel="noreferrer"
              className="hidden lg:flex items-center space-x-2 px-3.5 py-2 rounded-xl ardo-panel font-semibold text-slate-800 dark:text-slate-200 text-sm transition-all hover:text-emerald-700 dark:hover:text-emerald-400"
            >
              <GithubIcon className="w-4 h-4" />
              <span>GitHub</span>
            </a>
          </div>
        </div>
      </header>

      {/* SECTION 1: HERO / LANDING SECTION */}
      <section className="relative pt-32 pb-20 md:pt-44 md:pb-32 overflow-hidden z-10">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="grid grid-cols-1 lg:grid-cols-12 gap-12 items-center">
            {/* Hero Text Left */}
            <div className="lg:col-span-7 space-y-8 text-center lg:text-left">
              <div className="inline-flex items-center space-x-2 px-3.5 py-1.5 rounded-full ardo-panel border border-emerald-600/30 text-emerald-700 dark:text-emerald-400 text-xs font-bold tracking-wide animate-bounce">
                <Sparkles className="w-3.5 h-3.5" />
                <span>Your Vision, Our Execution</span>
              </div>

              <h1 className="text-4xl sm:text-6xl lg:text-7xl font-extrabold tracking-tight leading-tight text-slate-900 dark:text-white">
                Turn Intent Into <span className="gradient-text">Action.</span>
              </h1>

              <p className="text-lg sm:text-xl text-slate-700 dark:text-slate-300 max-w-2xl mx-auto lg:mx-0 font-normal leading-relaxed">
                An intelligent AI orchestration engine that transforms natural-language goals into structured tasks, coordinates browser automation, and verifies execution.
              </p>

              <div className="flex flex-col sm:flex-row items-center justify-center lg:justify-start space-y-4 sm:space-y-0 sm:space-x-4">
                <a
                  href="#simulator"
                  className="w-full sm:w-auto px-8 py-4 rounded-xl bg-gradient-to-r from-emerald-600 via-teal-600 to-emerald-600 hover:from-emerald-500 hover:to-teal-500 text-white font-semibold transition-all duration-300 shadow-xl shadow-emerald-500/25 flex items-center justify-center space-x-3 group"
                >
                  <span>Explore Live Engine</span>
                  <ArrowRight className="w-5 h-5 group-hover:translate-x-1 transition-transform" />
                </a>

                <Link
                  href={isAuthenticated ? "/app" : "/login?next=/app"}
                  className="w-full sm:w-auto px-8 py-4 rounded-xl ardo-panel font-semibold text-slate-800 dark:text-white transition-all flex items-center justify-center space-x-2 hover:border-emerald-500 hover:text-emerald-700 dark:hover:text-emerald-400"
                >
                  <PlayCircle className="w-5 h-5 text-emerald-600 dark:text-emerald-400" />
                  <span>Launch Application</span>
                </Link>
              </div>

              {/* Verified project indicators */}
              <div className="grid grid-cols-3 gap-4 pt-6 border-t border-emerald-500/15 max-w-lg mx-auto lg:mx-0">
                <div>
                  <div className="text-2xl font-bold text-slate-900 dark:text-white code-font">27</div>
                  <div className="text-xs text-slate-600 dark:text-slate-400 font-medium">Automated Tests Passed</div>
                </div>
                <div>
                  <div className="text-2xl font-bold text-emerald-700 dark:text-emerald-400 code-font">4 Levels</div>
                  <div className="text-xs text-slate-600 dark:text-slate-400 font-medium">Risk Classifications</div>
                </div>
                <div>
                  <div className="text-2xl font-bold text-teal-700 dark:text-teal-400 code-font">1500</div>
                  <div className="text-xs text-slate-600 dark:text-slate-400 font-medium">Token Context Budget</div>
                </div>
              </div>
            </div>

            {/* Hero Visual Right: Conceptual ARDO Workflow Preview */}
            <div className="lg:col-span-5 relative">
              <div className="absolute -inset-1 rounded-2xl bg-gradient-to-r from-emerald-500 to-teal-500 opacity-20 blur-xl" />
              <div className="relative ardo-panel rounded-2xl p-6 shadow-2xl space-y-4">
                {/* Header of preview card */}
                <div className="flex items-center justify-between pb-3 border-b border-emerald-500/15">
                  <div className="flex items-center space-x-3">
                    <div className="w-3 h-3 rounded-full bg-rose-500/80" />
                    <div className="w-3 h-3 rounded-full bg-amber-500/80" />
                    <div className="w-3 h-3 rounded-full bg-emerald-500/80" />
                    <span className="text-xs code-font text-slate-700 dark:text-slate-400 ml-2 font-medium">ardo-orchestrator.py</span>
                  </div>
                  <span className="px-2.5 py-1 rounded-md bg-emerald-500/15 text-emerald-800 dark:text-emerald-300 text-xs font-bold">
                    DAG PIPELINE
                  </span>
                </div>

                {/* Goal prompt display */}
                <div className="space-y-1.5">
                  <div className="text-xs text-slate-700 dark:text-slate-300 uppercase tracking-wider font-bold">
                    User Goal Objective
                  </div>
                  <div className="p-3 rounded-xl bg-slate-950 text-emerald-300 dark:bg-black/90 border border-emerald-500/30 text-xs code-font font-medium">
                    &quot;Research the latest papers on AI agents and summarize the important findings.&quot;
                  </div>
                </div>

                {/* Generated Tasks DAG workflow preview */}
                <div className="space-y-2.5">
                  <div className="text-xs text-slate-700 dark:text-slate-300 uppercase tracking-wider font-bold flex justify-between">
                    <span>Conceptual Workflow Nodes</span>
                    <span className="text-emerald-700 dark:text-emerald-400 font-bold">6 Stages</span>
                  </div>

                  {/* Task Node 1 */}
                  <div className="p-2.5 rounded-xl ardo-panel flex items-center justify-between">
                    <div className="flex items-center space-x-2.5">
                      <div className="w-6 h-6 rounded-lg bg-emerald-500/20 text-emerald-700 dark:text-emerald-400 flex items-center justify-center text-xs font-bold">1</div>
                      <span className="text-xs font-semibold text-slate-900 dark:text-white">Understand Research Objective</span>
                    </div>
                    <span className="px-2 py-0.5 rounded bg-emerald-500/15 text-emerald-800 dark:text-emerald-300 text-[10px] font-semibold">Completed</span>
                  </div>

                  {/* Task Node 2 */}
                  <div className="p-2.5 rounded-xl ardo-panel flex items-center justify-between">
                    <div className="flex items-center space-x-2.5">
                      <div className="w-6 h-6 rounded-lg bg-emerald-500/20 text-emerald-700 dark:text-emerald-400 flex items-center justify-center text-xs font-bold">2</div>
                      <span className="text-xs font-semibold text-slate-900 dark:text-white">Retrieve Relevant Knowledge (RAG)</span>
                    </div>
                    <span className="px-2 py-0.5 rounded bg-emerald-500/15 text-emerald-800 dark:text-emerald-300 text-[10px] font-semibold">Completed</span>
                  </div>

                  {/* Task Node 3 */}
                  <div className="p-2.5 rounded-xl ardo-panel flex items-center justify-between">
                    <div className="flex items-center space-x-2.5">
                      <div className="w-6 h-6 rounded-lg bg-emerald-500/20 text-emerald-700 dark:text-emerald-400 flex items-center justify-center text-xs font-bold">3</div>
                      <span className="text-xs font-semibold text-slate-900 dark:text-white">Generate Research Task DAG</span>
                    </div>
                    <span className="px-2 py-0.5 rounded bg-emerald-500/15 text-emerald-800 dark:text-emerald-300 text-[10px] font-semibold">Completed</span>
                  </div>

                  {/* Task Node 4 */}
                  <div className="p-2.5 rounded-xl ardo-panel flex items-center justify-between">
                    <div className="flex items-center space-x-2.5">
                      <div className="w-6 h-6 rounded-lg bg-teal-500/20 text-teal-700 dark:text-teal-400 flex items-center justify-center text-xs font-bold">4</div>
                      <span className="text-xs font-semibold text-slate-900 dark:text-white">Validate Task Dependencies</span>
                    </div>
                    <span className="px-2 py-0.5 rounded bg-teal-500/15 text-teal-800 dark:text-teal-300 text-[10px] font-semibold animate-pulse">Running</span>
                  </div>

                  {/* Task Node 5 */}
                  <div className="p-2.5 rounded-xl ardo-panel flex items-center justify-between">
                    <div className="flex items-center space-x-2.5">
                      <div className="w-6 h-6 rounded-lg bg-amber-500/20 text-amber-700 dark:text-amber-400 flex items-center justify-center text-xs font-bold">5</div>
                      <span className="text-xs font-semibold text-slate-800 dark:text-slate-200">Execute Approved Tasks</span>
                    </div>
                    <span className="px-2 py-0.5 rounded bg-amber-500/15 text-amber-800 dark:text-amber-300 text-[10px] font-semibold">Approval Req.</span>
                  </div>

                  {/* Task Node 6 */}
                  <div className="p-2.5 rounded-xl ardo-panel flex items-center justify-between">
                    <div className="flex items-center space-x-2.5">
                      <div className="w-6 h-6 rounded-lg bg-slate-500/20 text-slate-700 dark:text-slate-400 flex items-center justify-center text-xs font-bold">6</div>
                      <span className="text-xs font-semibold text-slate-700 dark:text-slate-300">Store Execution Experience</span>
                    </div>
                    <span className="px-2 py-0.5 rounded bg-slate-500/15 text-slate-700 dark:text-slate-300 text-[10px] font-semibold">Pending</span>
                  </div>
                </div>

                {/* Footer indicator */}
                <div className="pt-1 flex items-center justify-between text-xs text-slate-700 dark:text-slate-400 border-t border-emerald-500/15 font-medium">
                  <span>Status: Interactive Demo Preview</span>
                  <span className="text-emerald-700 dark:text-emerald-400 font-bold">ARDO v2026 Engine</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* SECTION 2: LIVE SIMULATOR */}
      <DemoSimulator />

      {/* SECTION 3: ARCHITECTURE (7 Connected Stages + Feedback Loop + Supporting Infrastructure) */}
      <section id="architecture" className="py-24 relative z-10 border-t border-emerald-500/10 bg-emerald-500/[0.02]">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="text-center space-y-4 mb-16">
            <div className="inline-flex items-center space-x-2 px-3.5 py-1.5 rounded-full ardo-panel border border-emerald-600/30 text-emerald-700 dark:text-emerald-400 text-xs font-bold uppercase tracking-wider">
              <Layers className="w-3.5 h-3.5" />
              <span>Pipeline Architecture</span>
            </div>
            <h2 className="text-3xl sm:text-5xl font-bold tracking-tight text-slate-900 dark:text-white">Inside the ARDO Intelligence Pipeline</h2>
            <p className="text-slate-700 dark:text-slate-300 max-w-2xl mx-auto text-lg leading-relaxed">
              A context-aware planning and orchestration architecture combining RAG, semantic memory, DAG validation, and safety-aware execution.
            </p>
          </div>

          {/* 7 Connected Stages Workflow */}
          <div className="space-y-4 mb-16">
            {/* Stage 01 */}
            <div className="ardo-panel rounded-2xl p-6 relative flex flex-col md:flex-row items-start md:items-center justify-between gap-4">
              <div className="flex items-center space-x-4">
                <div className="w-12 h-12 rounded-xl bg-emerald-500/20 text-emerald-700 dark:text-emerald-400 flex items-center justify-center font-bold text-lg code-font shrink-0">01</div>
                <div>
                  <h3 className="text-lg font-bold text-slate-900 dark:text-white">Natural Language Goal Input</h3>
                  <p className="text-sm text-slate-700 dark:text-slate-300 leading-relaxed">Users submit high-level objectives through the frontend dashboard and FastAPI REST endpoints.</p>
                </div>
              </div>
              <span className="px-3 py-1 rounded-full bg-emerald-500/15 text-emerald-800 dark:text-emerald-300 text-xs font-semibold code-font">Frontend / API</span>
            </div>

            <div className="flex justify-center text-emerald-600 dark:text-emerald-400">
              <ArrowDown className="w-6 h-6 animate-bounce" />
            </div>

            {/* Stage 02 */}
            <div className="ardo-panel rounded-2xl p-6 relative flex flex-col md:flex-row items-start md:items-center justify-between gap-4">
              <div className="flex items-center space-x-4">
                <div className="w-12 h-12 rounded-xl bg-teal-500/20 text-teal-700 dark:text-teal-400 flex items-center justify-center font-bold text-lg code-font shrink-0">02</div>
                <div>
                  <h3 className="text-lg font-bold text-slate-900 dark:text-white">RAG Knowledge Retrieval</h3>
                  <p className="text-sm text-slate-700 dark:text-slate-300 leading-relaxed">The system retrieves relevant information from ingested documents and previously stored semantic execution memories.</p>
                </div>
              </div>
              <span className="px-3 py-1 rounded-full bg-teal-500/15 text-teal-800 dark:text-teal-300 text-xs font-semibold code-font">PostgreSQL + pgvector</span>
            </div>

            <div className="flex justify-center text-emerald-600 dark:text-emerald-400">
              <ArrowDown className="w-6 h-6 animate-bounce" />
            </div>

            {/* Stage 03 */}
            <div className="ardo-panel rounded-2xl p-6 relative flex flex-col md:flex-row items-start md:items-center justify-between gap-4">
              <div className="flex items-center space-x-4">
                <div className="w-12 h-12 rounded-xl bg-cyan-500/20 text-cyan-700 dark:text-cyan-400 flex items-center justify-center font-bold text-lg code-font shrink-0">03</div>
                <div>
                  <h3 className="text-lg font-bold text-slate-900 dark:text-white">LLM-Based Goal Decomposition</h3>
                  <p className="text-sm text-slate-700 dark:text-slate-300 leading-relaxed">LLM-based components use the goal and retrieved context to generate a structured task graph.</p>
                </div>
              </div>
              <span className="px-3 py-1 rounded-full bg-cyan-500/15 text-cyan-800 dark:text-cyan-300 text-xs font-semibold code-font">Gemini / OpenAI / LangChain</span>
            </div>

            <div className="flex justify-center text-emerald-600 dark:text-emerald-400">
              <ArrowDown className="w-6 h-6 animate-bounce" />
            </div>

            {/* Stage 04 */}
            <div className="ardo-panel rounded-2xl p-6 relative flex flex-col md:flex-row items-start md:items-center justify-between gap-4">
              <div className="flex items-center space-x-4">
                <div className="w-12 h-12 rounded-xl bg-emerald-500/20 text-emerald-700 dark:text-emerald-400 flex items-center justify-center font-bold text-lg code-font shrink-0">04</div>
                <div>
                  <h3 className="text-lg font-bold text-slate-900 dark:text-white">DAG Validation & Dependency Resolution</h3>
                  <p className="text-sm text-slate-700 dark:text-slate-300 leading-relaxed">The generated task graph is validated for structural correctness, dependency relationships, and acyclic execution order.</p>
                </div>
              </div>
              <span className="px-3 py-1 rounded-full bg-emerald-500/15 text-emerald-800 dark:text-emerald-300 text-xs font-semibold code-font">Topological Sorting</span>
            </div>

            <div className="flex justify-center text-emerald-600 dark:text-emerald-400">
              <ArrowDown className="w-6 h-6 animate-bounce" />
            </div>

            {/* Stage 05 */}
            <div className="ardo-panel rounded-2xl p-6 relative flex flex-col md:flex-row items-start md:items-center justify-between gap-4">
              <div className="flex items-center space-x-4">
                <div className="w-12 h-12 rounded-xl bg-amber-500/20 text-amber-700 dark:text-amber-400 flex items-center justify-center font-bold text-lg code-font shrink-0">05</div>
                <div>
                  <h3 className="text-lg font-bold text-slate-900 dark:text-white">Safety Evaluation & Human Approval</h3>
                  <p className="text-sm text-slate-700 dark:text-slate-300 leading-relaxed">Tasks undergo risk evaluation. High-risk operations require human authorization through the dashboard approval mechanism.</p>
                </div>
              </div>
              <span className="px-3 py-1 rounded-full bg-amber-500/15 text-amber-800 dark:text-amber-300 text-xs font-semibold code-font">Risk Gates (LOW–CRITICAL)</span>
            </div>

            <div className="flex justify-center text-emerald-600 dark:text-emerald-400">
              <ArrowDown className="w-6 h-6 animate-bounce" />
            </div>

            {/* Stage 06 */}
            <div className="ardo-panel rounded-2xl p-6 relative flex flex-col md:flex-row items-start md:items-center justify-between gap-4">
              <div className="flex items-center space-x-4">
                <div className="w-12 h-12 rounded-xl bg-teal-500/20 text-teal-700 dark:text-teal-400 flex items-center justify-center font-bold text-lg code-font shrink-0">06</div>
                <div>
                  <h3 className="text-lg font-bold text-slate-900 dark:text-white">Task Execution & Real-Time Monitoring</h3>
                  <p className="text-sm text-slate-700 dark:text-slate-300 leading-relaxed">The execution engine manages task transitions and communicates execution progress, logs, and status updates through WebSockets.</p>
                </div>
              </div>
              <span className="px-3 py-1 rounded-full bg-teal-500/15 text-teal-800 dark:text-teal-300 text-xs font-semibold code-font">FastAPI + WebSockets</span>
            </div>

            <div className="flex justify-center text-emerald-600 dark:text-emerald-400">
              <ArrowDown className="w-6 h-6 animate-bounce" />
            </div>

            {/* Stage 07 */}
            <div className="ardo-panel rounded-2xl p-6 relative flex flex-col md:flex-row items-start md:items-center justify-between gap-4 border-2 border-emerald-500/40">
              <div className="flex items-center space-x-4">
                <div className="w-12 h-12 rounded-xl bg-emerald-600 text-white flex items-center justify-center font-bold text-lg code-font shrink-0">07</div>
                <div>
                  <h3 className="text-lg font-bold text-slate-900 dark:text-white">Feedback & Semantic Memory</h3>
                  <p className="text-sm text-slate-700 dark:text-slate-300 leading-relaxed">Execution outcomes, success/failure information, and experiences are stored in semantic memory to inform future planning.</p>
                </div>
              </div>
              <span className="px-3 py-1 rounded-full bg-emerald-500/15 text-emerald-800 dark:text-emerald-300 text-xs font-semibold code-font">Semantic Execution Memory</span>
            </div>
          </div>

          {/* Feedback Loop Visual Indicator */}
          <div className="max-w-4xl mx-auto mb-16 p-4 rounded-2xl ardo-panel border border-emerald-500/30 text-center flex items-center justify-center space-x-3 text-sm text-emerald-800 dark:text-emerald-300 font-medium">
            <Repeat className="w-5 h-5 animate-spin" style={{ animationDuration: '8s' }} />
            <span>
              <strong>Feedback Loop Active:</strong> Stage 07 continuously feeds historical execution experiences back into Stage 02 &amp; 03 for self-improving planning.
            </span>
          </div>

          {/* Supporting Infrastructure & Data Persistence */}
          <div className="max-w-5xl mx-auto space-y-6">
            <div className="text-center space-y-2">
              <h3 className="text-2xl font-bold text-slate-900 dark:text-white">Supporting Infrastructure & Data Persistence</h3>
              <p className="text-sm text-slate-700 dark:text-slate-300">Dedicated storage layers powering knowledge retrieval and execution state.</p>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              {/* PostgreSQL + pgvector */}
              <div className="ardo-panel rounded-2xl p-6 space-y-3">
                <div className="flex items-center space-x-3">
                  <div className="w-10 h-10 rounded-xl bg-blue-500/20 text-blue-700 dark:text-blue-400 flex items-center justify-center">
                    <Database className="w-5 h-5" />
                  </div>
                  <h4 className="text-lg font-bold text-slate-900 dark:text-white">PostgreSQL + pgvector</h4>
                </div>
                <ul className="space-y-2 text-xs text-slate-700 dark:text-slate-200 code-font font-medium">
                  <li className="flex items-center space-x-2">
                    <Check className="w-3.5 h-3.5 text-emerald-600 dark:text-emerald-400 shrink-0 font-bold" />
                    <span>Knowledge document chunks & SHA-256 deduplication</span>
                  </li>
                  <li className="flex items-center space-x-2">
                    <Check className="w-3.5 h-3.5 text-emerald-600 dark:text-emerald-400 shrink-0 font-bold" />
                    <span>Embedding storage & HNSW indexing</span>
                  </li>
                  <li className="flex items-center space-x-2">
                    <Check className="w-3.5 h-3.5 text-emerald-600 dark:text-emerald-400 shrink-0 font-bold" />
                    <span>Semantic vector retrieval & cosine similarity search</span>
                  </li>
                </ul>
              </div>

              {/* MongoDB */}
              <div className="ardo-panel rounded-2xl p-6 space-y-3">
                <div className="flex items-center space-x-3">
                  <div className="w-10 h-10 rounded-xl bg-emerald-500/20 text-emerald-700 dark:text-emerald-400 flex items-center justify-center">
                    <Server className="w-5 h-5" />
                  </div>
                  <h4 className="text-lg font-bold text-slate-900 dark:text-white">MongoDB</h4>
                </div>
                <ul className="space-y-2 text-xs text-slate-700 dark:text-slate-200 code-font font-medium">
                  <li className="flex items-center space-x-2">
                    <Check className="w-3.5 h-3.5 text-emerald-600 dark:text-emerald-400 shrink-0 font-bold" />
                    <span>Execution state persistence & session tracking</span>
                  </li>
                  <li className="flex items-center space-x-2">
                    <Check className="w-3.5 h-3.5 text-emerald-600 dark:text-emerald-400 shrink-0 font-bold" />
                    <span>Task information & operational telemetry logs</span>
                  </li>
                  <li className="flex items-center space-x-2">
                    <Check className="w-3.5 h-3.5 text-emerald-600 dark:text-emerald-400 shrink-0 font-bold" />
                    <span>Real-time WebSocket event synchronization</span>
                  </li>
                </ul>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* SECTION 4: TECH STACK */}
      <section id="techstack" className="py-24 relative z-10">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="text-center space-y-4 mb-16">
            <div className="inline-flex items-center space-x-2 px-3.5 py-1.5 rounded-full ardo-panel border border-emerald-600/30 text-emerald-700 dark:text-emerald-400 text-xs font-bold uppercase tracking-wider">
              <Cpu className="w-3.5 h-3.5" />
              <span>Engineering Stack</span>
            </div>
            <h2 className="text-3xl sm:text-5xl font-bold tracking-tight text-slate-900 dark:text-white">Built With Production-Grade Tech</h2>
            <p className="text-slate-700 dark:text-slate-300 max-w-2xl mx-auto text-lg leading-relaxed">
              Engineered for extreme performance, low latency, and robust type safety across the entire stack.
            </p>
          </div>

          {/* Stack Categories Grid */}
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            {/* Frontend */}
            <div className="ardo-panel rounded-2xl p-6 space-y-3">
              <div className="w-12 h-12 rounded-xl bg-cyan-500/20 text-cyan-700 dark:text-cyan-400 flex items-center justify-center">
                <Code className="w-6 h-6" />
              </div>
              <h3 className="text-lg font-bold text-slate-900 dark:text-white">Frontend</h3>
              <div className="flex flex-wrap gap-1.5">
                <span className="px-2 py-0.5 rounded bg-cyan-500/15 text-cyan-800 dark:text-cyan-300 text-xs code-font font-semibold">Next.js 16</span>
                <span className="px-2 py-0.5 rounded bg-cyan-500/15 text-cyan-800 dark:text-cyan-300 text-xs code-font font-semibold">React 19</span>
                <span className="px-2 py-0.5 rounded bg-cyan-500/15 text-cyan-800 dark:text-cyan-300 text-xs code-font font-semibold">TypeScript</span>
                <span className="px-2 py-0.5 rounded bg-cyan-500/15 text-cyan-800 dark:text-cyan-300 text-xs code-font font-semibold">Tailwind CSS</span>
                <span className="px-2 py-0.5 rounded bg-cyan-500/15 text-cyan-800 dark:text-cyan-300 text-xs code-font font-semibold">Zustand</span>
                <span className="px-2 py-0.5 rounded bg-cyan-500/15 text-cyan-800 dark:text-cyan-300 text-xs code-font font-semibold">React Flow</span>
              </div>
              <p className="text-sm text-slate-700 dark:text-slate-300 leading-relaxed pt-2">
                Interactive dashboard for DAG visualization, task monitoring, execution logs, and human approval controls.
              </p>
            </div>

            {/* Backend */}
            <div className="ardo-panel rounded-2xl p-6 space-y-3">
              <div className="w-12 h-12 rounded-xl bg-emerald-500/20 text-emerald-700 dark:text-emerald-400 flex items-center justify-center">
                <Zap className="w-6 h-6" />
              </div>
              <h3 className="text-lg font-bold text-slate-900 dark:text-white">Backend</h3>
              <div className="flex flex-wrap gap-1.5">
                <span className="px-2 py-0.5 rounded bg-emerald-500/15 text-emerald-800 dark:text-emerald-300 text-xs code-font font-semibold">Python 3.11</span>
                <span className="px-2 py-0.5 rounded bg-emerald-500/15 text-emerald-800 dark:text-emerald-300 text-xs code-font font-semibold">FastAPI</span>
                <span className="px-2 py-0.5 rounded bg-emerald-500/15 text-emerald-800 dark:text-emerald-300 text-xs code-font font-semibold">Uvicorn</span>
                <span className="px-2 py-0.5 rounded bg-emerald-500/15 text-emerald-800 dark:text-emerald-300 text-xs code-font font-semibold">Pydantic V2</span>
                <span className="px-2 py-0.5 rounded bg-emerald-500/15 text-emerald-800 dark:text-emerald-300 text-xs code-font font-semibold">REST APIs</span>
                <span className="px-2 py-0.5 rounded bg-emerald-500/15 text-emerald-800 dark:text-emerald-300 text-xs code-font font-semibold">WebSockets</span>
              </div>
              <p className="text-sm text-slate-700 dark:text-slate-300 leading-relaxed pt-2">
                Asynchronous backend infrastructure responsible for goal processing, planning, orchestration, and real-time communication.
              </p>
            </div>

            {/* AI & LLM Integration */}
            <div className="ardo-panel rounded-2xl p-6 space-y-3">
              <div className="w-12 h-12 rounded-xl bg-teal-500/20 text-teal-700 dark:text-teal-400 flex items-center justify-center">
                <Brain className="w-6 h-6" />
              </div>
              <h3 className="text-lg font-bold text-slate-900 dark:text-white">AI & LLM Integration</h3>
              <div className="flex flex-wrap gap-1.5">
                <span className="px-2 py-0.5 rounded bg-teal-500/15 text-teal-800 dark:text-teal-300 text-xs code-font font-semibold">Google Gemini</span>
                <span className="px-2 py-0.5 rounded bg-teal-500/15 text-teal-800 dark:text-teal-300 text-xs code-font font-semibold">OpenAI</span>
                <span className="px-2 py-0.5 rounded bg-teal-500/15 text-teal-800 dark:text-teal-300 text-xs code-font font-semibold">LangChain</span>
                <span className="px-2 py-0.5 rounded bg-teal-500/15 text-teal-800 dark:text-teal-300 text-xs code-font font-semibold">Goal Decomposition</span>
                <span className="px-2 py-0.5 rounded bg-teal-500/15 text-teal-800 dark:text-teal-300 text-xs code-font font-semibold">Fallbacks</span>
              </div>
              <p className="text-sm text-slate-700 dark:text-slate-300 leading-relaxed pt-2">
                LLM-powered reasoning and structured task generation with provider fallback mechanisms.
              </p>
            </div>

            {/* RAG & Vector Search */}
            <div className="ardo-panel rounded-2xl p-6 space-y-3">
              <div className="w-12 h-12 rounded-xl bg-blue-500/20 text-blue-700 dark:text-blue-400 flex items-center justify-center">
                <Search className="w-6 h-6" />
              </div>
              <h3 className="text-lg font-bold text-slate-900 dark:text-white">RAG & Vector Search</h3>
              <div className="flex flex-wrap gap-1.5">
                <span className="px-2 py-0.5 rounded bg-blue-500/15 text-blue-800 dark:text-blue-300 text-xs code-font font-semibold">RAG Pipeline</span>
                <span className="px-2 py-0.5 rounded bg-blue-500/15 text-blue-800 dark:text-blue-300 text-xs code-font font-semibold">Embeddings</span>
                <span className="px-2 py-0.5 rounded bg-blue-500/15 text-blue-800 dark:text-blue-300 text-xs code-font font-semibold">pgvector</span>
                <span className="px-2 py-0.5 rounded bg-blue-500/15 text-blue-800 dark:text-blue-300 text-xs code-font font-semibold">HNSW Indexing</span>
                <span className="px-2 py-0.5 rounded bg-blue-500/15 text-blue-800 dark:text-blue-300 text-xs code-font font-semibold">Cosine Similarity</span>
                <span className="px-2 py-0.5 rounded bg-blue-500/15 text-blue-800 dark:text-blue-300 text-xs code-font font-semibold">Semantic Memory</span>
              </div>
              <p className="text-sm text-slate-700 dark:text-slate-300 leading-relaxed pt-2">
                Context-aware knowledge retrieval using vector embeddings, semantic similarity, and historical execution experiences.
              </p>
            </div>

            {/* Database & Persistence */}
            <div className="ardo-panel rounded-2xl p-6 space-y-3">
              <div className="w-12 h-12 rounded-xl bg-green-500/20 text-green-700 dark:text-green-400 flex items-center justify-center">
                <Server className="w-6 h-6" />
              </div>
              <h3 className="text-lg font-bold text-slate-900 dark:text-white">Database & Persistence</h3>
              <div className="flex flex-wrap gap-1.5">
                <span className="px-2 py-0.5 rounded bg-green-500/15 text-green-800 dark:text-green-300 text-xs code-font font-semibold">PostgreSQL</span>
                <span className="px-2 py-0.5 rounded bg-green-500/15 text-green-800 dark:text-green-300 text-xs code-font font-semibold">pgvector</span>
                <span className="px-2 py-0.5 rounded bg-green-500/15 text-green-800 dark:text-green-300 text-xs code-font font-semibold">MongoDB Atlas</span>
                <span className="px-2 py-0.5 rounded bg-green-500/15 text-green-800 dark:text-green-300 text-xs code-font font-semibold">In-memory Fallback</span>
              </div>
              <p className="text-sm text-slate-700 dark:text-slate-300 leading-relaxed pt-2">
                PostgreSQL manages vector-based knowledge retrieval, while MongoDB handles execution persistence and operational state.
              </p>
            </div>

            {/* Automation & Testing */}
            <div className="ardo-panel rounded-2xl p-6 space-y-3">
              <div className="w-12 h-12 rounded-xl bg-amber-500/20 text-amber-700 dark:text-amber-400 flex items-center justify-center">
                <ShieldCheck className="w-6 h-6" />
              </div>
              <h3 className="text-lg font-bold text-slate-900 dark:text-white">Automation & Testing</h3>
              <div className="flex flex-wrap gap-1.5">
                <span className="px-2 py-0.5 rounded bg-amber-500/15 text-amber-800 dark:text-amber-300 text-xs code-font font-semibold">Playwright</span>
                <span className="px-2 py-0.5 rounded bg-amber-500/15 text-amber-800 dark:text-amber-300 text-xs code-font font-semibold">pytest</span>
                <span className="px-2 py-0.5 rounded bg-amber-500/15 text-amber-800 dark:text-amber-300 text-xs code-font font-semibold">pytest-asyncio</span>
                <span className="px-2 py-0.5 rounded bg-amber-500/15 text-amber-800 dark:text-amber-300 text-xs code-font font-semibold">TS Validation</span>
              </div>
              <p className="text-sm text-slate-700 dark:text-slate-300 leading-relaxed pt-2">
                Browser automation infrastructure and automated testing for the RAG pipeline, vector-store operations, and application integration.
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* SECTION 5: FEATURES / CORE ADVANTAGES */}
      <section id="features" className="py-24 relative z-10 border-t border-emerald-500/10 bg-emerald-500/[0.01]">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="text-center space-y-4 mb-16">
            <div className="inline-flex items-center space-x-2 px-3.5 py-1.5 rounded-full ardo-panel border border-emerald-600/30 text-emerald-700 dark:text-emerald-400 text-xs font-bold uppercase tracking-wider">
              <Sparkles className="w-3.5 h-3.5" />
              <span>Core Advantages</span>
            </div>
            <h2 className="text-3xl sm:text-5xl font-bold tracking-tight text-slate-900 dark:text-white">Engineered for Reliability</h2>
            <p className="text-slate-700 dark:text-slate-300 max-w-2xl mx-auto text-lg leading-relaxed">
              Designed to handle complex real-world workflows without deadlocks or unhandled task failures.
            </p>
          </div>

          {/* 8 Cards Grid */}
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
            <div className="ardo-panel rounded-2xl p-6 space-y-3">
              <div className="w-12 h-12 rounded-xl bg-emerald-500/20 text-emerald-700 dark:text-emerald-400 flex items-center justify-center">
                <Database className="w-6 h-6" />
              </div>
              <h3 className="text-lg font-bold text-slate-900 dark:text-white">1. Context-Aware RAG Retrieval</h3>
              <p className="text-sm text-slate-700 dark:text-slate-300 leading-relaxed">
                Retrieves relevant knowledge and contextual information before generating execution plans, reducing dependence on isolated LLM prompts.
              </p>
            </div>

            <div className="ardo-panel rounded-2xl p-6 space-y-3">
              <div className="w-12 h-12 rounded-xl bg-teal-500/20 text-teal-700 dark:text-teal-400 flex items-center justify-center">
                <BookOpen className="w-6 h-6" />
              </div>
              <h3 className="text-lg font-bold text-slate-900 dark:text-white">2. Semantic Execution Memory</h3>
              <p className="text-sm text-slate-700 dark:text-slate-300 leading-relaxed">
                Stores historical execution experiences, including successes and failures, to inform future planning and help avoid repeated mistakes.
              </p>
            </div>

            <div className="ardo-panel rounded-2xl p-6 space-y-3">
              <div className="w-12 h-12 rounded-xl bg-cyan-500/20 text-cyan-700 dark:text-cyan-400 flex items-center justify-center">
                <GitMerge className="w-6 h-6" />
              </div>
              <h3 className="text-lg font-bold text-slate-900 dark:text-white">3. Intelligent DAG Generation</h3>
              <p className="text-sm text-slate-700 dark:text-slate-300 leading-relaxed">
                Transforms high-level natural-language goals into structured task graphs with explicit dependencies.
              </p>
            </div>

            <div className="ardo-panel rounded-2xl p-6 space-y-3">
              <div className="w-12 h-12 rounded-xl bg-emerald-500/20 text-emerald-700 dark:text-emerald-400 flex items-center justify-center">
                <CheckCircle className="w-6 h-6" />
              </div>
              <h3 className="text-lg font-bold text-slate-900 dark:text-white">4. Dependency Validation</h3>
              <p className="text-sm text-slate-700 dark:text-slate-300 leading-relaxed">
                Validates task graphs for cycles and structural inconsistencies before execution.
              </p>
            </div>

            <div className="ardo-panel rounded-2xl p-6 space-y-3">
              <div className="w-12 h-12 rounded-xl bg-amber-500/20 text-amber-700 dark:text-amber-400 flex items-center justify-center">
                <ShieldAlert className="w-6 h-6" />
              </div>
              <h3 className="text-lg font-bold text-slate-900 dark:text-white">5. Human-in-the-Loop Safety</h3>
              <p className="text-sm text-slate-700 dark:text-slate-300 leading-relaxed">
                Uses risk classification and approval gates to ensure that high-risk operations require human authorization.
              </p>
            </div>

            <div className="ardo-panel rounded-2xl p-6 space-y-3">
              <div className="w-12 h-12 rounded-xl bg-teal-500/20 text-teal-700 dark:text-teal-400 flex items-center justify-center">
                <Cpu className="w-6 h-6" />
              </div>
              <h3 className="text-lg font-bold text-slate-900 dark:text-white">6. Resilient Retrieval Architecture</h3>
              <p className="text-sm text-slate-700 dark:text-slate-300 leading-relaxed">
                Provides in-memory cosine similarity and deterministic local embedding fallbacks when external database or embedding services are unavailable.
              </p>
            </div>

            <div className="ardo-panel rounded-2xl p-6 space-y-3">
              <div className="w-12 h-12 rounded-xl bg-cyan-500/20 text-cyan-700 dark:text-cyan-400 flex items-center justify-center">
                <Activity className="w-6 h-6" />
              </div>
              <h3 className="text-lg font-bold text-slate-900 dark:text-white">7. Real-Time Execution Monitoring</h3>
              <p className="text-sm text-slate-700 dark:text-slate-300 leading-relaxed">
                Uses WebSockets to communicate task status changes, execution logs, and approval requirements to the frontend.
              </p>
            </div>

            <div className="ardo-panel rounded-2xl p-6 space-y-3">
              <div className="w-12 h-12 rounded-xl bg-emerald-500/20 text-emerald-700 dark:text-emerald-400 flex items-center justify-center">
                <Globe className="w-6 h-6" />
              </div>
              <h3 className="text-lg font-bold text-slate-900 dark:text-white">8. Browser Automation Support</h3>
              <p className="text-sm text-slate-700 dark:text-slate-300 leading-relaxed">
                Uses Playwright-based browser automation for supported browser workflows, subject to execution policies and safety checks.
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* SECTION 6: RELIABILITY GUARANTEE & STATUS */}
      <section className="py-24 relative z-10">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8 space-y-12">
          <div className="ardo-panel rounded-3xl p-8 sm:p-12 relative overflow-hidden">
            <div className="absolute -left-20 -top-20 w-80 h-80 bg-emerald-500/10 rounded-full blur-3xl pointer-events-none" />
            <div className="space-y-6">
              <div className="inline-flex items-center space-x-2 px-3.5 py-1.5 rounded-full ardo-panel border border-emerald-600/30 text-emerald-700 dark:text-emerald-400 text-xs font-bold uppercase tracking-wider">
                <ShieldCheck className="w-3.5 h-3.5" />
                <span>Reliability Guarantee</span>
              </div>
              <h3 className="text-2xl sm:text-4xl font-bold tracking-tight text-slate-900 dark:text-white">Built for Reliable AI Orchestration</h3>

              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6 pt-4">
                <div className="p-4 rounded-xl ardo-panel space-y-2">
                  <div className="text-emerald-700 dark:text-emerald-400 font-bold text-sm">Context-Aware Planning</div>
                  <p className="text-xs text-slate-700 dark:text-slate-300 leading-relaxed">
                    Replaces isolated prompting with robust retrieved knowledge and vector-backed ground truth.
                  </p>
                </div>
                <div className="p-4 rounded-xl ardo-panel space-y-2">
                  <div className="text-teal-700 dark:text-teal-400 font-bold text-sm">Dependency-Validated Execution</div>
                  <p className="text-xs text-slate-700 dark:text-slate-300 leading-relaxed">
                    Ensures tasks execute in strict topological order rather than unstructured task lists.
                  </p>
                </div>
                <div className="p-4 rounded-xl ardo-panel space-y-2">
                  <div className="text-cyan-700 dark:text-cyan-400 font-bold text-sm">Historical Experience Retrieval</div>
                  <p className="text-xs text-slate-700 dark:text-slate-300 leading-relaxed">
                    Learns from past execution outcomes to prevent recurring operational errors.
                  </p>
                </div>
                <div className="p-4 rounded-xl ardo-panel space-y-2">
                  <div className="text-amber-800 dark:text-amber-300 font-bold text-sm">Safety Evaluation</div>
                  <p className="text-xs text-slate-700 dark:text-slate-300 leading-relaxed">
                    Applies risk levels and human approval gates instead of unrestricted autonomous actions.
                  </p>
                </div>
                <div className="p-4 rounded-xl ardo-panel space-y-2 lg:col-span-2">
                  <div className="text-emerald-700 dark:text-emerald-400 font-bold text-sm">Graceful Fallback Mechanisms</div>
                  <p className="text-xs text-slate-700 dark:text-slate-300 leading-relaxed">
                    Maintains full operational continuity through deterministic local embeddings and in-memory vector fallback when external infrastructure is idle.
                  </p>
                </div>
              </div>
            </div>
          </div>

          {/* Project Status Indicator */}
          <div className="ardo-panel rounded-2xl p-6 sm:p-8 flex flex-col sm:flex-row items-center justify-between space-y-4 sm:space-y-0">
            <div className="flex items-center space-x-4">
              <div className="w-3 h-3 rounded-full bg-emerald-500 animate-pulse shrink-0" />
              <div>
                <div className="text-sm font-bold tracking-wide text-slate-900 dark:text-white">FROM NATURAL LANGUAGE TO REAL-WORLD ACTION</div>
                <p className="text-xs text-slate-700 dark:text-slate-400 mt-0.5">
                  Transforming user intent into structured plans, coordinated browser actions, and verified execution.
                </p>
              </div>
            </div>
            <div className="px-4 py-2 rounded-xl ardo-panel text-xs text-emerald-800 dark:text-emerald-400 font-bold shrink-0 code-font">
              Your AI-Powered Path from Goal to Completion
            </div>
          </div>
        </div>
      </section>

      {/* Footer */}
      <footer className="py-12 border-t border-emerald-500/10 relative z-10 ardo-panel">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 flex flex-col md:flex-row items-center justify-between space-y-6 md:space-y-0">
          <div className="flex items-center space-x-3">
            <div className="w-8 h-8 rounded-xl bg-gradient-to-tr from-emerald-600 to-teal-600 flex items-center justify-center">
              <Cpu className="w-4 h-4 text-white" />
            </div>
            <div>
              <span className="text-base font-bold text-slate-900 dark:text-white">ARDO – Autonomous Resilient Directed Orchestrator</span>
              <span className="text-xs block text-slate-600 dark:text-slate-400 font-medium">AI Goal-to-Task Planning Engine</span>
            </div>
          </div>

          <div className="text-center text-xs text-slate-700 dark:text-slate-400 font-semibold">
            Your AI-Powered Path from Goal to Completion
          </div>

          <div className="flex items-center space-x-4">
            <a
              href="https://github.com"
              target="_blank"
              rel="noreferrer"
              className="p-2 rounded-xl ardo-panel text-slate-700 dark:text-slate-300 hover:text-emerald-700 dark:hover:text-emerald-400 transition-all"
            >
              <GithubIcon className="w-5 h-5" />
            </a>
            <Link
              href="/app"
              className="p-2 rounded-xl ardo-panel text-slate-700 dark:text-slate-300 hover:text-emerald-700 dark:hover:text-emerald-400 transition-all"
              title="Launch ARDO Engine"
            >
              <ExternalLink className="w-5 h-5" />
            </Link>
          </div>
        </div>
      </footer>
    </div>
  );
}
